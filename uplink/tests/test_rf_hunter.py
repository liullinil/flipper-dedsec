from uplink.rf_hunter import EventStore, FamilyGrouper, RfEvent, UploadReceiver, event_id, fingerprint
import pytest


def _event(seq=1, **kw):
    return RfEvent(
        device_uuid="device", session_id="session", sequence_number=seq,
        captured_at_utc="2026-10-06T08:01:12.431Z", monotonic_ms=seq,
        frequency_hz=433920000, modulation="OOK", pulse_timings_us=(400, 800, 400), **kw)


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


def test_store_retries_are_idempotent_and_ack_reclaims_capture(tmp_path):
    store = EventStore(tmp_path)
    event = _event()
    store.add(event, b"pulse-data")
    store.add(_event(), b"different-but-duplicate-id")
    assert len(store.events) == 1
    assert store.manifest()[0]["event_id"] == event.event_id
    assert b"pulse-data" == b"".join(chunk for _, chunk in store.chunks(event.event_id))
    assert store.acknowledge(event.event_id)


def test_upload_receiver_resumes_chunks_and_validates_checksum(tmp_path):
    store = EventStore(tmp_path)
    receiver = UploadReceiver(store)
    event = _event()
    receiver.begin(event.to_dict())
    assert receiver.receive_chunk(event.event_id, 0, b"ab") == 2
    assert receiver.receive_chunk(event.event_id, 2, b"cd") == 4
    import hashlib
    receiver.commit(event.event_id, hashlib.sha256(b"abcd").hexdigest())
    assert store.events[event.event_id].upload_state == "pending"
    assert store.pending()
    assert store.acknowledge(event.event_id)
    assert not store.pending()


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
