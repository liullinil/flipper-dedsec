"""Flipper app updates: automatic push, pacing for old apps, giving up on a dead transfer."""
import base64

import pytest

from uplink import updater as up


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(up.time, "time", c)
    return c


def make(version, data=b"x" * 2000):
    u = up.Updater()
    u.latest = {"tag": "v1.2.1", "url": "https://example.invalid/dedsec_uplink.fap", "size": len(data)}
    u.set_flipper_version(version)
    return u, data


def chunks(lines):
    return [line for line in lines if line.startswith("UD|")]


def test_old_apps_get_one_chunk_at_a_time(clock):
    u, data = make("1.1.0")
    u._start(u.latest, data)
    assert u.urgent()[0].startswith("UB|v1.2.1|2000|")
    u.on_ack(0)
    sent = chunks(u.urgent())
    assert len(sent) == 1                       # the 1.1 app blocks its BLE thread otherwise
    assert len(base64.b64decode(sent[0].split("|")[2])) == up.CHUNK
    assert chunks(u.urgent()) == []             # nothing more until the ack
    u.on_ack(up.CHUNK)
    assert len(chunks(u.urgent())) == 1


def test_new_apps_get_a_window(clock):
    u, data = make("1.2.0")
    u._start(u.latest, data)
    u.urgent()
    u.on_ack(0)
    assert len(chunks(u.urgent())) == up.WINDOW


def test_lines_fit_the_flipper_buffer(clock):
    u, data = make("1.2.0")
    u._start(u.latest, data)
    u.urgent()
    u.on_ack(0)
    for line in u.urgent():
        assert len(line) + 1 < 600                # the Flipper's LINE_MAX


def test_retransmissions_do_not_keep_a_dead_transfer_alive(clock):
    u, data = make("1.2.0")
    u._start(u.latest, data)
    u.urgent()
    u.on_ack(0)
    u.urgent()
    for _ in range(40):                          # the Flipper stops answering
        clock.now += 1.0
        u.urgent()
        if not u.busy():
            break
    assert not u.busy()
    assert clock.now - 1000.0 <= up.GIVE_UP_AFTER + 2


def test_unanswered_start_gives_up(clock):
    u, data = make("1.1.0")
    u._start(u.latest, data)
    starts = 0
    for _ in range(60):
        starts += sum(line.startswith("UB|") for line in u.urgent())
        clock.now += 1.0
        if not u.busy():
            break
    assert not u.busy() and starts >= 2


def test_finished_transfer_ends_with_ue(clock):
    u, data = make("1.2.0", b"y" * 500)
    u._start(u.latest, data)
    u.urgent()
    u.on_ack(0)
    u.urgent()
    u.on_ack(500)
    assert u.urgent()[-1] == "UE|v1.2.1"
    assert not u.busy()


def test_auto_push_waits_after_an_attempt(clock, monkeypatch):
    started = []
    monkeypatch.setattr(up.Updater, "request", lambda self, tag: started.append(tag))
    monkeypatch.setattr(up.threading, "Thread",
                        lambda target, args, **_kw: type("T", (), {"start": lambda s: target(*args)})())
    u, _ = make("1.2.0")
    assert u.auto_flipper() is True and started == ["v1.2.1"]
    assert u.auto_flipper() is False                  # not again right away
    clock.now += up.AUTO_RETRY + 1
    assert u.auto_flipper() is True
    u.set_flipper_version("1.2.1")
    clock.now += up.AUTO_RETRY + 1
    assert u.auto_flipper() is False                  # up to date


def test_one_download_at_a_time(clock, monkeypatch):
    u, data = make("1.2.0")
    calls = []

    class Response:
        def __enter__(self):
            calls.append(1)
            u.request("v1.2.1")                      # a second request while downloading
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return data

    monkeypatch.setattr(up.urllib.request, "urlopen", lambda *a, **k: Response())
    u.request("v1.2.1")
    assert calls == [1] and u.busy()
