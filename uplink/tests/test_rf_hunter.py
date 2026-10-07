import json
import os

import pytest

from uplink import rf_hunter
from uplink.rf_hunter import (EventStore, FamilyGrouper, RfEvent, default_store_root, event_id,
                              folder_signature, resolve_store_root)


def _event(seq=1, **kw):
    return RfEvent(
        device_uuid="device", session_id="session", sequence_number=seq,
        captured_at_utc="2026-10-06T08:01:12.431Z", monotonic_ms=seq,
        frequency_hz=433920000, modulation="OOK", pulse_timings_us=(400, 800, 400), **kw)


def _flipper_record(seq, **extra):
    data = {"schema_version": 1, "event_id": f"rf-dev-s1-{seq}", "device_uuid": "dev",
            "session_id": "s1", "sequence_number": seq, "captured_at_utc": "2026-10-06T20:00:00Z",
            "monotonic_ms": seq, "frequency_hz": 433920000, "fingerprint_id": "local-0badc0de",
            "family_id": None, "pulse_timings_us": [400, 800, 400, 800], "upload_state": "pending"}
    data.update(extra)
    return data


def test_identity_preserves_repeated_observations():
    assert event_id("device", "session", 1) == event_id("device", "session", 1)
    assert event_id("device", "session", 1) != event_id("device", "session", 2)


def test_event_ids_cannot_escape_capture_store():
    with pytest.raises(ValueError, match="unsafe RF event id"):
        _event(event_id="../outside")


def test_grouping_keeps_events_separate():
    grouper = FamilyGrouper()
    first, second = _event(), _event(2)
    assert grouper.assign(first) == grouper.assign(second)
    assert first.event_id != second.event_id
    assert len(grouper.families[first.family_id]["event_ids"]) == 2


def test_store_add_is_idempotent_and_keeps_the_first_capture(tmp_path):
    store = EventStore(tmp_path)
    event = _event()
    store.add(event, b"pulse-data")
    store.add(_event(), b"different-but-duplicate-id")
    assert len(store.events) == 1
    assert store.read_capture(event.event_id) == b"pulse-data"
    assert store.has_capture(event.event_id)
    reopened = EventStore(tmp_path)
    assert list(reopened.events) == [event.event_id]
    assert reopened.read_capture(event.event_id) == b"pulse-data"


def test_add_fsyncs_capture_and_record_before_returning(tmp_path, monkeypatch):
    synced = []
    real_fsync = os.fsync

    def recording_fsync(fd):
        synced.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(rf_hunter.os, "fsync", recording_fsync)
    store = EventStore(tmp_path)
    store.add(_event(1), b"raw-1")  # creates events.jsonl (temp file + durable rename)
    assert len(synced) >= 2  # capture blob and record
    synced.clear()
    store.add(_event(2), b"raw-2")  # appends
    assert len(synced) >= 2
    synced.clear()
    store.add(_event(3))
    assert len(synced) == 1
    assert not [name for name in os.listdir(tmp_path) if name.endswith(".tmp")]


def test_jsonl_store_appends_new_records_instead_of_rewriting(tmp_path):
    store = EventStore(tmp_path)
    for seq in range(1, 4):
        store.add(_event(seq), b"raw")
    before = (tmp_path / "events.jsonl").read_bytes()
    store.add(_event(4), b"raw")
    after = (tmp_path / "events.jsonl").read_bytes()
    assert after.startswith(before) and after.count(b"\n") == 4


def test_attach_capture_rewrites_durably_and_preserves_unparseable_lines(tmp_path):
    good = _event(1)
    (tmp_path / "events.jsonl").write_text(
        json.dumps(good.to_dict()) + "\n" + '{"broken": tru\n', encoding="utf-8")
    store = EventStore(tmp_path)
    assert store.skipped_records == 1
    store.attach_capture(good.event_id, b"late capture")
    text = (tmp_path / "events.jsonl").read_text(encoding="utf-8")
    assert '{"broken": tru' in text  # evidence is never silently dropped
    reopened = EventStore(tmp_path)
    assert reopened.read_capture(good.event_id) == b"late capture"
    assert reopened.skipped_records == 1


def test_malformed_records_are_skipped_and_counted(tmp_path):
    lines = [
        json.dumps(_event(1).to_dict()),
        json.dumps(_event(2).to_dict()).replace("[400, 800, 400]", "[Infinity]"),
        json.dumps(dict(_event(3).to_dict(), captured_at_unix=1e300)),
        json.dumps(dict(_event(4).to_dict(), captured_at_utc=None, captured_at_unix=1e20)),
        json.dumps(dict(_event(5).to_dict(), rssi_avg_dbm=float("nan"))),
        json.dumps(dict(_event(6).to_dict(), frequency_hz="not a number")),
        "[1, 2, 3]",
        "{not json",
        json.dumps(dict(_event(7).to_dict(), modulation=None, classification=None)),
    ]
    (tmp_path / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    (events_dir / "rf-dev-s1-1.json").write_text('{"pulse_timings_us":[Infinity]}', encoding="utf-8")
    (events_dir / "rf-dev-s1-2.json").write_text(json.dumps(_flipper_record(2)), encoding="utf-8")
    store = EventStore(tmp_path)
    assert store.skipped_records == 8
    assert len(store.load_errors) == 8
    assert set(store.events) == {_event(1).event_id, _event(7).event_id, "rf-dev-s1-2"}
    assert store.events[_event(7).event_id].modulation == "unknown"


def test_torn_tail_is_ignored_and_the_next_append_terminates_it(tmp_path):
    good = _event(1)
    (tmp_path / "events.jsonl").write_text(json.dumps(good.to_dict()) + "\n" + '{"event_id": "half',
                                           encoding="utf-8")
    store = EventStore(tmp_path)
    assert list(store.events) == [good.event_id] and store.skipped_records == 0
    store.add(_event(2))
    reopened = EventStore(tmp_path)
    assert set(reopened.events) == {good.event_id, _event(2).event_id}
    assert reopened.skipped_records == 1  # the torn fragment is now a counted bad line


def test_failed_append_is_rolled_back_and_never_glued_to_the_next_record(tmp_path, monkeypatch):
    store = EventStore(tmp_path)
    store.add(_event(1))
    real_fsync = os.fsync

    def failing_fsync(fd):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(rf_hunter.os, "fsync", failing_fsync)
    with pytest.raises(OSError):
        store.add(_event(2))
    assert _event(2).event_id not in store.events  # not durable -> not imported
    monkeypatch.setattr(rf_hunter.os, "fsync", real_fsync)
    store.add(_event(3))
    reopened = EventStore(tmp_path)
    assert {_event(1).event_id, _event(3).event_id} <= set(reopened.events)
    assert reopened.skipped_records == 0


def test_read_only_store_refuses_writes(tmp_path):
    EventStore(tmp_path).add(_event(1), b"raw")
    store = EventStore(tmp_path, read_only=True)
    with pytest.raises(PermissionError):
        store.add(_event(2))
    with pytest.raises(PermissionError):
        store.attach_capture(_event(1).event_id, b"x")
    assert len(EventStore(tmp_path).events) == 1


def test_per_event_layout_writes_only_dirty_records(tmp_path, monkeypatch):
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    for seq in range(1, 31):
        (events_dir / f"rf-dev-s1-{seq}.json").write_text(json.dumps(_flipper_record(seq)), encoding="utf-8")
    originals = {path.name: path.read_bytes() for path in events_dir.iterdir()}
    written = []
    real_write = rf_hunter._write_durable

    def counting_write(path, data):
        written.append(os.path.basename(path))
        return real_write(path, data)

    monkeypatch.setattr(rf_hunter, "_write_durable", counting_write)
    store = EventStore(tmp_path)
    store._flush()
    assert written == []
    new = RfEvent.from_dict(_flipper_record(99))
    store.add(new, b"raw")
    assert sorted(written) == sorted([f"{new.event_id}.bin", f"{new.event_id}.json"])
    for name, data in originals.items():
        assert (events_dir / name).read_bytes() == data  # untouched Flipper evidence


def test_flipper_compact_record_is_normalized():
    event = RfEvent.from_dict({
        "event_id": "rf-device-session-2",
        "device_id": "device",
        "session_id": "session",
        "sequence_number": 2,
        "captured_at": "2026-01-01T00:00:00Z",
        "monotonic_ms": 12,
        "frequency_hz": 433920000,
        "rssi_dbm": -52,
        "last_duration_us": 900,
        "pulse_timings_us": [400, 800],
    })
    assert event.event_id == "rf-device-session-2"
    assert event.device_uuid == "device"
    assert event.rssi_min_dbm == event.rssi_avg_dbm == event.rssi_max_dbm == -52
    assert event.duration_us == 900


def test_flipper_follow_and_local_fingerprint_fields_roundtrip():
    event = RfEvent.from_dict({
        "event_id": "rf-device-session-3",
        "device_id": "device",
        "session_id": "session",
        "sequence_number": 3,
        "captured_at_utc": "2026-01-01T00:00:00Z",
        "monotonic_ms": 12,
        "frequency_hz": 433920000,
        "fingerprint_id": "local-1234abcd",
        "family_id": None,
        "classification": "unknown",
        "follow_profile_id": "duration-900",
        "follow_similarity": 0.96,
    })
    assert event.fingerprint_id == "local-1234abcd"
    assert event.follow_profile_id == "duration-900"
    assert event.follow_similarity == 0.96
    assert RfEvent.from_dict(event.to_dict()).follow_profile_id == "duration-900"


def test_passive_nfc_field_record_preserves_observation_metadata():
    event = RfEvent.from_dict({
        "event_id": "rf-device-session-nfc-1",
        "device_id": "device",
        "session_id": "session",
        "sequence_number": 3,
        "captured_at_utc": "2026-10-06T08:02:00Z",
        "monotonic_ms": 1200,
        "source_type": "nfc",
        "frequency_hz": 13560000,
        "modulation": "NFC",
        "nfc_technology": "external-field",
        "nfc_protocol": "carrier-presence",
        "field_duration_ms": 84,
        "nfc_field_count": 1,
    })
    assert event.source_type == "nfc"
    assert event.nfc_technology == "external-field"
    assert event.nfc_protocol == "carrier-presence"
    assert event.nfc_field_duration_ms == 84


def test_store_loads_per_event_journal_uploaded_records_and_receipts(tmp_path):
    event = _event(8)
    events_dir = tmp_path / "events"
    receipts_dir = tmp_path / "receipts"
    uploaded_dir = tmp_path / "uploaded"
    for directory in (events_dir, receipts_dir, uploaded_dir):
        directory.mkdir()
    (events_dir / f"{event.event_id}.json").write_text(json.dumps(event.to_dict()), encoding="utf-8")
    receipt = _event(9)
    receipt.upload_state = "uploaded"
    (receipts_dir / f"{receipt.event_id}.ack").write_text(json.dumps(receipt.to_dict()), encoding="utf-8")
    kept = RfEvent.from_dict(_flipper_record(10))
    (uploaded_dir / f"{kept.event_id}.json").write_text(json.dumps(_flipper_record(10)), encoding="utf-8")
    loaded = EventStore(tmp_path)
    assert set(loaded.events) == {event.event_id, receipt.event_id, kept.event_id}
    assert loaded.events[event.event_id].upload_state == "pending"
    assert loaded.events[receipt.event_id].upload_state == "uploaded"
    assert loaded.events[kept.event_id].upload_state == "uploaded"
    assert loaded.events[kept.event_id].pulse_timings_us == (400, 800, 400, 800)


def test_receipt_only_event_is_not_resurrected_on_per_event_flush(tmp_path):
    events_dir = tmp_path / "events"
    receipts_dir = tmp_path / "receipts"
    events_dir.mkdir()
    receipts_dir.mkdir()
    uploaded = _event(11)
    uploaded.upload_state = "uploaded"
    receipt = receipts_dir / f"{uploaded.event_id}.ack"
    receipt.write_text(json.dumps(uploaded.to_dict()), encoding="utf-8")
    loaded = EventStore(tmp_path)
    loaded._flush()
    assert not (events_dir / f"{uploaded.event_id}.json").exists()
    assert receipt.exists()


def test_store_rejects_capture_path_traversal(tmp_path):
    event = _event(10, capture_blob="../outside.bin")
    with pytest.raises(ValueError, match="unsafe RF capture path"):
        EventStore(tmp_path).add(event, b"secret")


def test_resolve_store_root_accepts_journal_app_and_sd_folders(tmp_path):
    sd = tmp_path / "sd"
    journal = sd / "apps_data" / "dedsec_uplink" / "rf"
    (journal / "events").mkdir(parents=True)
    (journal / "uploaded").mkdir()
    for path in (journal, journal / "events", journal / "uploaded", sd / "apps_data" / "dedsec_uplink",
                 sd / "apps_data", sd):
        assert resolve_store_root(path) == str(journal)
    legacy = tmp_path / "old" / "apps_data" / "rf_signal_hunter" / "rf_signal_hunter"
    (legacy / "receipts").mkdir(parents=True)
    assert resolve_store_root(tmp_path / "old") == str(legacy)
    assert resolve_store_root(legacy.parent) == str(legacy)
    empty = tmp_path / "empty"
    assert resolve_store_root(empty) == str(empty)


def test_default_store_root_is_per_user(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    root = default_store_root(create=True)
    assert root == os.path.join(str(tmp_path), "DedSecUplink", "rf_hunter")
    assert os.path.isdir(root)


def test_folder_signature_changes_when_records_are_added(tmp_path):
    store = EventStore(tmp_path)
    before = folder_signature(tmp_path)
    store.add(_event(1), b"raw")
    assert folder_signature(tmp_path) != before
