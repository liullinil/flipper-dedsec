"""Replacing files where every rename is refused (encrypted AppData on some PCs)."""
import ctypes
import os
import shutil

import pytest

from uplink import files

pytestmark = pytest.mark.skipif(os.name != "nt", reason="MoveFileEx is Windows-only")


def refusing_renames(calls):
    """MoveFileExW that fails like an encrypted AppData folder unless copying is allowed."""
    def move(src, dst, flags):
        calls.append(flags)
        if not flags & files.MOVEFILE_COPY_ALLOWED:
            ctypes.set_last_error(files.ERROR_NOT_SAME_DEVICE)
            return False
        shutil.copyfile(src, dst)
        os.remove(src)
        return True
    return move


def test_replace_copies_where_renames_are_refused(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(files, "_MoveFileExW", refusing_renames(calls))
    src, dst = tmp_path / "new.tmp", tmp_path / "data.json"
    dst.write_bytes(b"old")
    src.write_bytes(b"new")
    files.replace(str(src), str(dst))
    assert dst.read_bytes() == b"new" and not src.exists()
    assert calls[0] & files.MOVEFILE_WRITE_THROUGH and not calls[0] & files.MOVEFILE_COPY_ALLOWED
    assert calls[1] & files.MOVEFILE_COPY_ALLOWED and calls[1] & files.MOVEFILE_WRITE_THROUGH


def test_rf_store_and_session_acks_survive_refused_renames(tmp_path, monkeypatch):
    import json

    from uplink.rf_hunter import EventStore, RfEvent
    from uplink.session_views import SessionViews

    monkeypatch.setattr(files, "_MoveFileExW", refusing_renames([]))
    record = {"event_id": "rf-a-s1-1", "device_uuid": "a", "session_id": "s1", "sequence_number": 1,
              "captured_at_utc": "2026-10-07T10:00:00Z", "monotonic_ms": 5, "source_type": "subghz",
              "frequency_hz": 433920000}
    payload = (json.dumps(record) + "\n").encode()
    store = EventStore(tmp_path / "store")
    store.add(RfEvent.from_dict(record), capture=payload)
    assert EventStore(tmp_path / "store", read_only=True).read_capture("rf-a-s1-1") == payload
    views = SessionViews(str(tmp_path / "acks.json"))
    views._acks.add(("X", "k1", "r1"))
    views._save()
    assert json.loads((tmp_path / "acks.json").read_text())["acks"] == [
        {"kind": "X", "key": "k1", "revision": "r1"}]


def test_a_sharing_violation_is_retried(tmp_path, monkeypatch):
    attempts = []

    def busy_once(src, dst, flags):
        attempts.append(flags)
        if len(attempts) == 1:
            ctypes.set_last_error(32)
            return False
        os.replace(src, dst)
        return True

    monkeypatch.setattr(files, "_MoveFileExW", busy_once)
    monkeypatch.setattr(files.time, "sleep", lambda _s: None)
    src, dst = tmp_path / "a", tmp_path / "b"
    src.write_bytes(b"x")
    files.replace(str(src), str(dst))
    assert dst.read_bytes() == b"x" and len(attempts) == 2
