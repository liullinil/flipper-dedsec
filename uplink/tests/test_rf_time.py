from datetime import datetime, timezone

import pytest

from uplink.rf_hunter import RfEvent


def _event(**changes):
    fields = dict(device_uuid="device", session_id="run", sequence_number=1,
                  captured_at_utc="2026-10-06T08:01:12Z", monotonic_ms=100,
                  timezone_offset_minutes=180)
    fields.update(changes)
    return RfEvent.from_dict(fields)


def test_flipper_rtc_offset_and_epoch_roundtrip_without_changing_identity():
    instant = datetime(2026, 10, 6, 8, 1, 12, tzinfo=timezone.utc).timestamp()
    event = _event(captured_at_unix=instant, rtc_local_unix=int(instant + 180 * 60))
    exported = event.to_dict()
    imported = RfEvent.from_dict(exported)
    assert imported.event_id == event.event_id
    assert imported.captured_at_unix == instant
    assert imported.rtc_local_unix == instant + 180 * 60
    assert imported.timezone_offset_minutes == 180
    assert imported.captured_at_utc == "2026-10-06T08:01:12.000Z"


def test_naive_local_calendar_uses_device_offset_and_explicit_offset_is_normalized():
    local = _event(captured_at_utc="2026-10-06T11:01:12")
    zoned = _event(captured_at_utc="2026-10-06T11:01:12+03:00")
    assert local.captured_at_utc == zoned.captured_at_utc == "2026-10-06T08:01:12.000Z"
    assert local.event_id == zoned.event_id


def test_epoch_only_record_imports_and_clock_adjustment_keeps_monotonic_evidence():
    first = _event(captured_at_utc=None, captured_at_unix=1791273672, monotonic_ms=100)
    second = _event(sequence_number=2, captured_at_utc="2026-10-06T07:00:00Z", monotonic_ms=200)
    assert second.captured_at_unix < first.captured_at_unix
    assert second.monotonic_ms > first.monotonic_ms
    assert second.event_id != first.event_id


def test_inconsistent_epoch_and_calendar_are_rejected_before_ack():
    with pytest.raises(ValueError, match="disagree"):
        _event(captured_at_unix=1)
    with pytest.raises(ValueError, match="timezone"):
        _event(timezone_offset_minutes=841)
