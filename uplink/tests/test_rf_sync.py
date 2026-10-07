"""RfSync against a Python model of the Flipper RF engine (contract v1, section 2).

The fake mirrors the C side: ``RL|cursor`` answers the pending record at that
directory index, ``RR`` returns at most 120 bytes as base64, ``RA`` validates
size + CRC-32 and removes the record from ``events/`` (``RK``), and failures are
``RX|code|text``.  Nothing here touches a radio or a real BLE link.
"""
import base64
import binascii
import json
import os
import queue
import threading
import time
from datetime import datetime, timezone

from uplink import rf_sync
from uplink.rf_hunter import EventStore, RfEvent
from uplink.rf_sync import RfSync

DEVICE = "0123456789abcdef"


def crc(data: bytes) -> int:
    return binascii.crc32(data) & 0xFFFFFFFF


def c_record(seq, pulses=(400, 800, 400, 800), session="s1", **extra):
    """A record shaped like the one the C engine writes (newline included)."""
    event_id = f"rf-{DEVICE}-{session}-{seq}"
    when = 1791316800 + seq
    data = {
        "schema_version": 1, "event_id": event_id, "device_uuid": DEVICE, "session_id": session,
        "sequence_number": seq,
        "captured_at_utc": datetime.fromtimestamp(when, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "captured_at_unix": when, "timezone_offset_minutes": 0, "rtc_local_unix": when,
        "monotonic_ms": seq * 100, "source_type": "subghz", "mode": "Scout", "frequency_hz": 433920000,
        "modulation": "unknown", "bandwidth_hz": 0, "duration_us": 5000, "repeat_count": 1,
        "battery_pct": 80, "fingerprint_id": "local-1234abcd", "family_id": None,
        "classification": "unknown", "classification_confidence": 0.0, "follow_profile_id": "",
        "follow_similarity": 0.0, "rssi_min_dbm": -70.0, "rssi_avg_dbm": -60.0, "rssi_max_dbm": -50.0,
        "pulse_count": len(pulses), "last_duration_us": pulses[-1] if pulses else 0,
        "pulse_timings_us": list(pulses), "upload_state": "pending",
    }
    data.update(extra)
    return event_id, (json.dumps(data, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeFlipper:
    """Python model of the RF engine's sync side."""

    def __init__(self, records, chunk=120, keep_uploaded=False):
        self.events = dict(records)  # insertion order == directory order
        self.uploaded = {}
        self.chunk = chunk
        self.keep_uploaded = keep_uploaded
        self.requests = []
        self.acked = []
        self.off = False
        self.reject_ack = set()
        self.fail_reads = {}
        self.vanish_on_read = set()
        self.wrong_crc = set()

    def status_line(self):
        return f"R|{len(self.events)}|{len(self.events) + len(self.uploaded)}|2048|1|0"

    def manifest_crc(self, event_id, payload):
        return crc(payload) ^ 1 if event_id in self.wrong_crc else crc(payload)

    def handle(self, line):
        self.requests.append(line)
        parts = line.split("|")
        tag = parts[0]
        if tag == "Z":
            return []
        if self.off and tag in ("RL", "RR", "RA"):
            return ["RX|off|RF sync disabled"]
        if tag == "RL":
            cursor = int(parts[1])
            ids = list(self.events)
            if cursor >= len(ids):
                return ["RE"]
            event_id = ids[cursor]
            payload = self.events[event_id]
            return [f"RI|{cursor + 1}|{event_id}|{len(payload)}|{self.manifest_crc(event_id, payload)}"]
        if tag == "RR":
            event_id, offset = parts[1], int(parts[2])
            if event_id in self.vanish_on_read:
                self.vanish_on_read.discard(event_id)
                self.events.pop(event_id, None)
            if self.fail_reads.get(event_id):
                self.fail_reads[event_id] -= 1
                return ["RX|io|SD read failed"]
            payload = self.events.get(event_id)
            if payload is None:
                return ["RX|nf|not found"]
            if offset >= len(payload):
                return ["RX|bad|offset out of range"]
            data = payload[offset:offset + self.chunk]
            return [f"RD|{event_id}|{offset}|{base64.b64encode(data).decode('ascii')}"]
        if tag == "RA":
            event_id, size, checksum = parts[1], int(parts[2]), int(parts[3])
            payload = self.events.get(event_id)
            if payload is None:
                return ["RX|nf|not found"]
            if event_id in self.reject_ack or size != len(payload) or checksum != crc(payload):
                return ["RX|bad|ack mismatch"]
            self.acked.append(event_id)
            del self.events[event_id]
            if self.keep_uploaded:
                self.uploaded[event_id] = payload
            return [f"RK|{event_id}"]
        return ["RX|bad|unknown request"]

    def read_requests(self, event_id):
        return [line for line in self.requests if line.startswith(f"RR|{event_id}|")]


def make_sync(tmp_path, clock=None, **kwargs):
    clock = clock or Clock()
    sync = RfSync(str(tmp_path / "store"), clock=clock, wall_clock=lambda: 1791316800.0 + clock.now,
                  **kwargs)
    return sync, clock


def deliver(sync, line):
    assert sync.handle_line(line.split("|")) is True


def pump(sync, flipper, clock, steps=4000, transform=None, step=0.05):
    """Play the link loop: poll urgent_lines, deliver requests, feed replies back."""
    for _ in range(steps):
        lines = sync.urgent_lines()
        replies = []
        for line in lines:
            replies.extend(flipper.handle(line))
        if transform is not None:
            replies = transform(replies)
        for reply in replies:
            deliver(sync, reply)
        clock.advance(step)
        if not lines and not replies and not sync.status()["syncing"]:
            return
    raise AssertionError("RF sync did not settle")


def start(sync, flipper):
    sync.on_link(True)
    deliver(sync, flipper.status_line())


# --------------------------------------------------------------------------- happy path
class DurabilityCheckingFlipper(FakeFlipper):
    """Fails the test if an RA arrives before the record is durable on disk."""

    def __init__(self, records, store_root, **kwargs):
        super().__init__(records, **kwargs)
        self.store_root = store_root
        self.checked = []

    def handle(self, line):
        if line.startswith("RA|"):
            event_id = line.split("|")[1]
            fresh = EventStore(self.store_root, read_only=True)  # what a crash would leave
            assert event_id in fresh.events, "RA sent before the record was committed"
            assert fresh.read_capture(event_id) == self.events[event_id]
            self.checked.append(event_id)
        return super().handle(line)


def test_imports_all_records_commits_before_ack_and_keeps_cursor(tmp_path):
    big_pulses = tuple(300 + (index % 7) * 50 for index in range(400))  # record > 768 bytes
    records = [c_record(1), c_record(2, pulses=big_pulses), c_record(3, classification="☃")]
    assert len(records[1][1]) > 2000
    sync, clock = make_sync(tmp_path)
    flipper = DurabilityCheckingFlipper(records, sync.store_root)
    start(sync, flipper)
    assert sync.status()["syncing"]
    pump(sync, flipper, clock)

    assert flipper.events == {}
    assert flipper.acked == [event_id for event_id, _ in records]
    assert flipper.checked == flipper.acked
    # RK removes the record from events/, so the cursor never moves.
    assert {line for line in flipper.requests if line.startswith("RL|")} == {"RL|0"}
    store = EventStore(sync.store_root, read_only=True)
    for event_id, payload in records:
        assert store.read_capture(event_id) == payload
        assert store.events[event_id].upload_state == "imported"
        assert store.events[event_id].fingerprint_id == "local-1234abcd"  # Flipper evidence kept
    assert store.events[records[2][0]].classification == "☃"
    status = sync.status()
    assert status["imported"] == 3 and status["failed"] == 0 and status["skipped"] == 0
    assert status["last_error"] == "" and status["syncing"] is False
    assert status["last_sync"] is not None
    assert (status["pending"], status["stored"], status["free_kb"], status["state"]) == (3, 3, 2048, 1)


def test_reads_are_pipelined_four_deep_and_matched_by_offset(tmp_path):
    event_id, payload = c_record(1, pulses=tuple(range(300, 700)))
    sync, clock = make_sync(tmp_path)
    flipper = FakeFlipper([(event_id, payload)])
    start(sync, flipper)
    held = []
    outstanding = 0
    peak = 0
    for _ in range(2000):
        lines = sync.urgent_lines()
        outstanding += sum(line.startswith("RR|") for line in lines)
        peak = max(peak, outstanding)
        replies = held
        held = []
        for line in lines:
            held.extend(flipper.handle(line))  # replies arrive one poll later
        for reply in replies:
            outstanding -= reply.startswith("RD|")
            deliver(sync, reply)
        clock.advance(0.05)
        if not flipper.events and not held:
            break
    assert flipper.acked == [event_id]
    assert peak == rf_sync.MAX_INFLIGHT_READS
    offsets = [int(line.split("|")[2]) for line in flipper.read_requests(event_id)]
    assert offsets == list(range(0, len(payload), rf_sync.READ_CHUNK))  # no duplicate reads


def test_reordered_and_dropped_replies_are_recovered(tmp_path):
    event_id, payload = c_record(1, pulses=tuple(range(300, 600)))
    sync, clock = make_sync(tmp_path)
    flipper = FakeFlipper([(event_id, payload)])
    dropped = []

    def chaos(replies):
        result = []
        for reply in replies:
            if reply.startswith(f"RD|{event_id}|120|") and not dropped:
                dropped.append(reply)  # lost notification
                continue
            result.append(reply)
        return list(reversed(result))  # out of order

    start(sync, flipper)
    started = clock.now
    pump(sync, flipper, clock, transform=chaos)
    assert dropped and flipper.acked == [event_id]
    assert EventStore(sync.store_root).read_capture(event_id) == payload
    reads_at_120 = [line for line in flipper.read_requests(event_id) if line.endswith("|120")]
    assert len(reads_at_120) == 2  # re-requested once after the timeout
    assert clock.now - started >= rf_sync.REQUEST_TIMEOUT


def test_short_replies_leave_gaps_that_are_requested_again(tmp_path):
    records = [c_record(1, pulses=tuple(range(300, 500))), c_record(2)]
    sync, clock = make_sync(tmp_path)
    flipper = FakeFlipper(records, chunk=50)
    start(sync, flipper)
    pump(sync, flipper, clock)
    assert flipper.events == {}
    store = EventStore(sync.store_root)
    for event_id, payload in records:
        assert store.read_capture(event_id) == payload


# --------------------------------------------------------------------------- per-item failures
def test_unanswered_record_is_given_up_and_the_next_one_imported(tmp_path):
    stuck, stuck_payload = c_record(1)
    good, good_payload = c_record(2)
    sync, clock = make_sync(tmp_path)
    flipper = FakeFlipper([(stuck, stuck_payload), (good, good_payload)])

    def swallow(replies):
        return [reply for reply in replies if not reply.startswith(f"RD|{stuck}|")]

    start(sync, flipper)
    started = clock.now
    pump(sync, flipper, clock, transform=swallow)
    status = sync.status()
    assert stuck in flipper.events and flipper.acked == [good]
    assert status["failed"] == 1 and status["imported"] == 1
    assert "timed out reading" in status["last_error"]
    assert clock.now - started >= rf_sync.RECORD_TIMEOUT
    assert ["RL|0", "RL|1", "RL|1"] == [line for line in flipper.requests if line.startswith("RL|")][:3]


def test_malformed_record_is_skipped_and_retried_only_by_sync_now(tmp_path):
    bad_id = f"rf-{DEVICE}-s1-7"
    bad_payload = b'{"event_id":"' + bad_id.encode() + b'","pulse_timings_us":[Infinity]}\n'
    good, good_payload = c_record(8)
    sync, clock = make_sync(tmp_path)
    flipper = FakeFlipper([(bad_id, bad_payload), (good, good_payload)])
    start(sync, flipper)
    pump(sync, flipper, clock)
    status = sync.status()
    assert flipper.acked == [good] and bad_id in flipper.events
    assert status["failed"] == 1 and "malformed record" in status["last_error"]
    reads = len(flipper.read_requests(bad_id))
    assert reads >= 1

    # Automatic rounds skip the unchanged bad record without downloading it again.
    deliver(sync, flipper.status_line())
    pump(sync, flipper, clock)
    assert len(flipper.read_requests(bad_id)) == reads
    assert sync.status()["failed"] == 1 and sync.status()["last_error"]

    # An explicit sync retries it.
    sync.sync_now()
    pump(sync, flipper, clock)
    assert len(flipper.read_requests(bad_id)) > reads
    assert sync.status()["failed"] == 2


def test_checksum_mismatch_downloads_once_more_then_rejects(tmp_path):
    event_id, payload = c_record(1)
    other, other_payload = c_record(2)
    sync, clock = make_sync(tmp_path)
    flipper = FakeFlipper([(event_id, payload), (other, other_payload)])
    flipper.wrong_crc.add(event_id)
    start(sync, flipper)
    pump(sync, flipper, clock)
    chunks = -(-len(payload) // rf_sync.READ_CHUNK)
    assert len(flipper.read_requests(event_id)) == 2 * chunks
    assert event_id in flipper.events and flipper.acked == [other]
    assert "checksum mismatch" in sync.status()["last_error"]
    assert event_id not in EventStore(sync.store_root).events


def test_invalid_list_entries_are_skipped_with_cursor_next(tmp_path):
    good, good_payload = c_record(2)

    class BadManifest(FakeFlipper):
        def handle(self, line):
            if line == "RL|0":
                self.requests.append(line)
                return ["RI|1|../escape|10|5"]
            if line == "RL|1":
                self.requests.append(line)
                return [f"RI|2|{DEVICE}-big|{rf_sync.MAX_RECORD_BYTES + 1}|5"]
            if line.startswith("RL|"):
                self.requests.append(line)
                cursor = int(line.split("|")[1]) - 2
                ids = list(self.events)
                if cursor >= len(ids):
                    return ["RE"]
                payload = self.events[ids[cursor]]
                return [f"RI|{cursor + 3}|{ids[cursor]}|{len(payload)}|{crc(payload)}"]
            return super().handle(line)

    sync, clock = make_sync(tmp_path)
    flipper = BadManifest([(good, good_payload)])
    start(sync, flipper)
    pump(sync, flipper, clock)
    status = sync.status()
    assert status["failed"] == 2 and status["imported"] == 1
    assert flipper.acked == [good]


def test_cursor_that_does_not_advance_aborts_the_round(tmp_path):
    class Looping(FakeFlipper):
        def handle(self, line):
            if line.startswith("RL|"):
                self.requests.append(line)
                return ["RI|0|loop|10|5"]
            return super().handle(line)

    sync, clock = make_sync(tmp_path)
    flipper = Looping([c_record(1)])
    start(sync, flipper)
    pump(sync, flipper, clock)
    assert "did not advance" in sync.status()["last_error"]
    assert not sync.status()["syncing"]


# --------------------------------------------------------------------------- existing events
def test_stored_copy_that_matches_is_acknowledged_without_download(tmp_path):
    event_id, payload = c_record(1)
    root = tmp_path / "store"
    EventStore(root).add(RfEvent.from_dict(json.loads(payload)), capture=payload)
    sync, clock = make_sync(tmp_path)
    flipper = FakeFlipper([(event_id, payload)])
    start(sync, flipper)
    pump(sync, flipper, clock)
    assert flipper.acked == [event_id]
    assert flipper.read_requests(event_id) == []
    assert sync.status()["skipped"] == 1 and sync.status()["imported"] == 0


def test_missing_capture_is_downloaded_and_attached_before_ack(tmp_path):
    event_id, payload = c_record(1)
    root = tmp_path / "store"
    # For example a store that was opened from a copied SD journal (no blob).
    EventStore(root).add(RfEvent.from_dict(json.loads(payload)))
    sync, clock = make_sync(tmp_path)
    flipper = DurabilityCheckingFlipper([(event_id, payload)], str(root))
    start(sync, flipper)
    pump(sync, flipper, clock)
    assert flipper.acked == [event_id] and flipper.checked == [event_id]
    assert flipper.read_requests(event_id)
    store = EventStore(root)
    assert store.read_capture(event_id) == payload
    assert len(store.events) == 1
    assert sync.status()["imported"] == 1


def test_conflicting_stored_copy_is_reported_and_never_acknowledged(tmp_path):
    event_id, payload = c_record(1)
    other, other_payload = c_record(2)
    root = tmp_path / "store"
    EventStore(root).add(RfEvent.from_dict(json.loads(payload)), capture=b"different payload")
    sync, clock = make_sync(tmp_path)
    flipper = FakeFlipper([(event_id, payload), (other, other_payload)])
    start(sync, flipper)
    pump(sync, flipper, clock)
    status = sync.status()
    assert event_id in flipper.events and flipper.acked == [other]
    assert not [line for line in flipper.requests if line.startswith(f"RA|{event_id}|")]
    assert status["conflicts"] == 1 and status["failed"] == 1
    assert "conflict" in status["last_error"]
    assert EventStore(root).read_capture(event_id) == b"different payload"


def test_conflicting_identity_without_capture_is_not_acknowledged(tmp_path):
    event_id, payload = c_record(1)
    root = tmp_path / "store"
    stored = json.loads(payload)
    stored["sequence_number"] = 99  # same id, different observation
    EventStore(root).add(RfEvent.from_dict(stored))
    sync, clock = make_sync(tmp_path)
    flipper = FakeFlipper([(event_id, payload)])
    start(sync, flipper)
    pump(sync, flipper, clock)
    assert event_id in flipper.events and not flipper.acked
    assert sync.status()["conflicts"] == 1
    assert not EventStore(root).read_capture(event_id)


def test_rejected_ack_keeps_record_and_next_round_acknowledges_stored_copy(tmp_path):
    event_id, payload = c_record(1)
    sync, clock = make_sync(tmp_path)
    flipper = FakeFlipper([(event_id, payload)])
    flipper.reject_ack.add(event_id)
    start(sync, flipper)
    pump(sync, flipper, clock)
    assert event_id in flipper.events and sync.status()["failed"] == 1
    assert event_id in EventStore(sync.store_root).events  # committed before the RA
    reads = len(flipper.read_requests(event_id))
    flipper.reject_ack.clear()
    sync.sync_now()
    pump(sync, flipper, clock)
    assert flipper.acked == [event_id]
    assert len(flipper.read_requests(event_id)) == reads  # no second download
    assert sync.status()["skipped"] == 1


def test_lost_rk_followed_by_not_found_counts_as_acknowledged(tmp_path):
    event_id, payload = c_record(1)
    sync, clock = make_sync(tmp_path)
    flipper = FakeFlipper([(event_id, payload)])
    lost = []

    def drop_rk(replies):
        result = []
        for reply in replies:
            if reply.startswith("RK|") and not lost:
                lost.append(reply)
                continue
            result.append(reply)
        return result

    start(sync, flipper)
    pump(sync, flipper, clock, transform=drop_rk)
    assert lost and flipper.acked == [event_id]
    assert sync.status()["imported"] == 1 and sync.status()["failed"] == 0


def test_late_error_for_an_old_read_is_not_taken_as_the_ack_reply(tmp_path):
    """RX has no request id: a late RX for a duplicate RR must not fail the RA."""
    event_id, payload = c_record(1, pulses=tuple(range(300, 380)))
    sync, clock = make_sync(tmp_path)
    flipper = FakeFlipper([(event_id, payload)])
    start(sync, flipper)
    held = []
    late = []
    for _ in range(2000):
        lines = sync.urgent_lines()
        replies, held = held, []
        for line in lines:
            answer = flipper.handle(line)
            if line == f"RR|{event_id}|120" and not late:
                late.append("RX|io|SD read failed")  # answered much later
                continue
            if line.startswith("RA|") and late:
                answer = [late.pop()] + answer  # FIFO: the old read's reply comes first
                late.append("done")
            held.extend(answer)
        for reply in replies:
            deliver(sync, reply)
        clock.advance(0.05)
        if not flipper.events and not held:
            break
    assert flipper.acked == [event_id]
    assert len([line for line in flipper.requests if line.startswith("RA|")]) == 1
    status = sync.status()
    assert status["imported"] == 1 and status["failed"] == 0 and status["last_error"] == ""


def test_record_deleted_during_read_relists_the_same_cursor(tmp_path):
    gone, gone_payload = c_record(1)
    good, good_payload = c_record(2)
    sync, clock = make_sync(tmp_path)
    flipper = FakeFlipper([(gone, gone_payload), (good, good_payload)])
    flipper.vanish_on_read.add(gone)
    start(sync, flipper)
    pump(sync, flipper, clock)
    assert flipper.acked == [good] and flipper.events == {}
    assert sync.status()["failed"] == 0


def test_sd_read_errors_are_retried(tmp_path):
    event_id, payload = c_record(1)
    sync, clock = make_sync(tmp_path)
    flipper = FakeFlipper([(event_id, payload)])
    flipper.fail_reads[event_id] = 2
    start(sync, flipper)
    pump(sync, flipper, clock)
    assert flipper.acked == [event_id]


def test_store_write_failure_aborts_without_ack(tmp_path, monkeypatch):
    event_id, payload = c_record(1)
    sync, clock = make_sync(tmp_path)
    flipper = FakeFlipper([(event_id, payload)])

    def disk_full(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(sync.store, "add", disk_full)
    start(sync, flipper)
    pump(sync, flipper, clock)
    status = sync.status()
    assert not flipper.acked and event_id in flipper.events
    assert "cannot write" in status["last_error"] and not status["syncing"]
    # A failed write pauses automatic rounds instead of re-downloading at once.
    deliver(sync, flipper.status_line())
    assert not sync.status()["syncing"]


# --------------------------------------------------------------------------- link handling
def test_link_drop_mid_record_aborts_cleanly_and_next_link_resumes(tmp_path):
    event_id, payload = c_record(1, pulses=tuple(range(300, 600)))
    sync, clock = make_sync(tmp_path)
    flipper = FakeFlipper([(event_id, payload)])
    start(sync, flipper)
    seen = []

    def cut(replies):
        for reply in replies:
            if reply.startswith("RD|"):
                seen.append(reply)
        if seen:
            sync.on_link(False)
            return []
        return replies

    pump(sync, flipper, clock, transform=cut)
    status = sync.status()
    assert not status["syncing"] and "link lost" in status["last_error"]
    assert not status["link_up"]
    assert event_id in flipper.events and not flipper.acked
    assert not [line for line in flipper.requests if line.startswith("RA|")]
    assert sync.urgent_lines() == []
    assert event_id not in EventStore(sync.store_root).events

    requests_before = len(flipper.requests)
    sync.on_link(True)
    deliver(sync, flipper.status_line())
    lines_after = sync.urgent_lines()
    assert lines_after[0].startswith("Z|")  # PC clock is sent again on every link
    for line in lines_after:
        for reply in flipper.handle(line):
            deliver(sync, reply)
    pump(sync, flipper, clock)
    assert flipper.acked == [event_id] and len(flipper.requests) > requests_before
    assert sync.status()["last_error"] == ""


def test_sync_now_while_link_down_starts_when_link_returns(tmp_path):
    event_id, payload = c_record(1)
    sync, clock = make_sync(tmp_path)
    flipper = FakeFlipper([(event_id, payload)])
    sync.on_link(False)
    sync.sync_now()
    assert not sync.status()["syncing"]
    sync.on_link(True)
    pump(sync, flipper, clock)
    assert flipper.acked == [event_id]


def test_sync_disabled_on_flipper_backs_off_until_sync_now(tmp_path):
    event_id, payload = c_record(1)
    sync, clock = make_sync(tmp_path)
    flipper = FakeFlipper([(event_id, payload)])
    flipper.off = True
    start(sync, flipper)
    pump(sync, flipper, clock)
    assert "disabled on the Flipper" in sync.status()["last_error"]
    deliver(sync, flipper.status_line())
    assert not sync.status()["syncing"]  # automatic rounds pause
    flipper.off = False
    sync.sync_now()  # the user asks explicitly
    pump(sync, flipper, clock)
    assert flipper.acked == [event_id]


def test_automatic_round_resumes_after_backoff(tmp_path):
    event_id, payload = c_record(1)
    sync, clock = make_sync(tmp_path)
    flipper = FakeFlipper([(event_id, payload)])
    flipper.off = True
    start(sync, flipper)
    pump(sync, flipper, clock)
    flipper.off = False
    clock.advance(rf_sync.AUTO_RETRY_BACKOFF + 1)
    deliver(sync, flipper.status_line())
    assert sync.status()["syncing"]
    pump(sync, flipper, clock)
    assert flipper.acked == [event_id]


def test_clock_line_reports_utc_and_local_offset(tmp_path):
    sync, _clock = make_sync(tmp_path)
    sync.on_link(True)
    lines = sync.urgent_lines()
    assert len(lines) == 1 and lines[0].startswith("Z|")
    _tag, utc, offset = lines[0].split("|")
    assert int(utc) > 1_700_000_000
    assert -14 * 60 <= int(offset) <= 14 * 60
    assert sync.urgent_lines() == []  # once per link, not every poll
    quiet, _ = make_sync(tmp_path / "quiet", send_clock=False)
    quiet.on_link(True)
    assert quiet.urgent_lines() == []


def test_no_round_without_pending_records(tmp_path):
    sync, _clock = make_sync(tmp_path)
    sync.on_link(True)
    sync.urgent_lines()
    deliver(sync, "R|0|5|2048|1|0")
    assert sync.urgent_lines() == [] and not sync.status()["syncing"]
    assert sync.status()["stored"] == 5


# --------------------------------------------------------------------------- robustness
def test_malformed_and_foreign_lines_are_harmless(tmp_path):
    event_id, payload = c_record(1)
    sync, clock = make_sync(tmp_path)
    flipper = FakeFlipper([(event_id, payload)])
    assert sync.handle_line(["H", "host"]) is False
    assert sync.handle_line([]) is False
    assert sync.handle_line(["C", "1", "dir"]) is False
    garbage = [["R"], ["R", "a", "b", "c", "d", "e"], ["R", "1", "2"], ["RI"], ["RI", "x"],
               ["RD"], ["RD", event_id, "nope", "@@@"], ["RD", event_id, "0", "!!!"],
               ["RD", event_id, "-5", "AAAA"], ["RK"], ["RK", "unknown"],
               ["RD", event_id, "999999", "AAAA"], ["RD", event_id, "0", ""]]
    idle_only = [["RE", "extra"], ["RX"], ["RX", "zz"], ["RX", "off"]]
    for parts in garbage + idle_only:
        assert sync.handle_line(parts) is True  # RF tag: consumed, never raised
    assert not sync.status()["syncing"]
    start(sync, flipper)
    for _ in range(40):  # garbage interleaved with every phase of the round
        for parts in garbage:
            sync.handle_line(parts)
        lines = sync.urgent_lines()
        for line in lines:
            for reply in flipper.handle(line):
                deliver(sync, reply)
        clock.advance(0.05)
    pump(sync, flipper, clock)
    assert flipper.acked == [event_id]
    assert EventStore(sync.store_root).read_capture(event_id) == payload


def test_status_keys_follow_the_contract(tmp_path):
    sync, _clock = make_sync(tmp_path)
    status = sync.status()
    for key in ("pending", "stored", "free_kb", "state", "errors", "imported", "failed",
                "last_error", "syncing", "last_sync"):
        assert key in status
    assert isinstance(sync.store, EventStore)
    assert os.path.isdir(sync.store_root)


def test_threads_ble_link_and_ui_concurrently(tmp_path, monkeypatch):
    """handle_line on a 'BLE' thread, urgent_lines on a 'link' thread, status/sync_now from 'UI'."""
    monkeypatch.setattr(rf_sync, "REQUEST_TIMEOUT", 0.5)
    records = [c_record(index, pulses=tuple(range(300, 300 + index * 40))) for index in range(1, 9)]
    flipper = FakeFlipper(records)
    sync = RfSync(str(tmp_path / "store"))
    to_flipper = queue.Queue()
    errors = []
    stop = threading.Event()

    def link_loop():
        try:
            while not stop.is_set():
                for line in sync.urgent_lines():
                    to_flipper.put(line)
                time.sleep(0.005)
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    def ble_loop():
        try:
            while not stop.is_set():
                try:
                    line = to_flipper.get(timeout=0.05)
                except queue.Empty:
                    continue
                for reply in flipper.handle(line):
                    sync.handle_line(reply.split("|"))
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=link_loop), threading.Thread(target=ble_loop)]
    for thread in threads:
        thread.start()
    sync.on_link(True)
    sync.handle_line(flipper.status_line().split("|"))
    deadline = time.monotonic() + 20
    try:
        while flipper.events and time.monotonic() < deadline:
            sync.status()
            sync.sync_now()
            time.sleep(0.01)
    finally:
        stop.set()
        for thread in threads:
            thread.join(5)
    assert not errors
    assert flipper.events == {}
    store = EventStore(sync.store_root)
    for event_id, payload in records:
        assert store.read_capture(event_id) == payload
