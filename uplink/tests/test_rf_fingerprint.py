from types import SimpleNamespace

from uplink.rf_fingerprint import StructuralGrouper, classify, compare_events, fingerprint


def _event(seq=1, pulses=(400, 800, 400, 1200), payload=b"\x12\x34", **overrides):
    values = {
        "device_uuid": "device",
        "session_id": "session",
        "sequence_number": seq,
        "event_id": f"event-{seq}",
        "captured_at_utc": f"2026-10-06T08:01:{seq:02d}.000Z",
        "source_type": "subghz",
        "frequency_hz": 433_920_000,
        "modulation": "OOK",
        "bandwidth_hz": 12_000,
        "repeat_count": 2,
        "pulse_timings_us": pulses,
        "payload": payload,
        "fingerprint_id": "",
        "family_id": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_same_carrier_different_structure_stays_separate():
    first = _event(1, pulses=(400, 800, 400, 1200))
    second = _event(2, pulses=(500, 500, 1000, 1000))
    grouper = StructuralGrouper()
    assert fingerprint(first) != fingerprint(second)
    assert grouper.assign(first) != grouper.assign(second)
    comparison = compare_events(first, second)
    assert comparison["relationship"] in {"weak_similarity", "unknown"}
    assert comparison["percent"] < 80


def test_rolling_payload_changes_keep_one_structural_family():
    first = _event(1, payload=b"\x01\x02\x03")
    second = _event(2, payload=b"\xa5\x5a\xc3")
    assert fingerprint(first) == fingerprint(second)
    comparison = compare_events(first, second)
    assert comparison["relationship"] == "variable_payload"
    assert "payload differs with stable frame length" in comparison["reasons"]
    grouper = StructuralGrouper()
    assert grouper.assign(first) == grouper.assign(second)


def test_clock_jitter_is_tolerated_in_absolute_and_relative_timings():
    first = _event(1, pulses=(400, 800, 400, 1200))
    second = _event(2, pulses=(425, 775, 425, 1225))
    assert fingerprint(first) == fingerprint(second)
    assert compare_events(first, second)["score"] >= 0.9


def test_missing_structure_stays_provisional_and_unmerged():
    first = _event(1, pulses=(), payload=b"")
    second = _event(2, pulses=(), payload=b"")
    grouper = StructuralGrouper()
    first_family = grouper.assign(first)
    second_family = grouper.assign(second)
    assert first_family != second_family
    assert grouper.families[first_family]["provisional"]
    assert grouper.families[first_family]["confidence"] == 0.0
    assert compare_events(first, second)["relationship"] == "unknown"


def test_repeated_observations_keep_event_identity_but_group_together():
    grouper = StructuralGrouper()
    events = [_event(index) for index in range(1, 4)]
    families = [grouper.assign(event) for event in events]
    assert len(set(families)) == 1
    family = grouper.families[families[0]]
    assert family["event_ids"] == ["event-1", "event-2", "event-3"]
    assert len(family["fingerprints"]) == 1


def test_passive_nfc_field_events_group_by_carrier_metadata():
    first = _event(1, pulses=(), source_type="nfc", frequency_hz=13_560_000,
                   modulation="NFC", nfc_technology="external-field",
                   nfc_protocol="carrier-presence")
    second = _event(2, pulses=(), source_type="nfc", frequency_hz=13_560_000,
                    modulation="NFC", nfc_technology="external-field",
                    nfc_protocol="carrier-presence")
    grouper = StructuralGrouper()
    assert grouper.assign(first) == grouper.assign(second)
    assert not grouper.families[first.family_id]["provisional"]
    result = compare_events(first, second)
    assert result["relationship"] == "same_structure"
    assert "same NFC carrier" in result["reasons"]


def test_rebuild_is_deterministic_and_splits_wrong_old_family():
    first = _event(1, pulses=(400, 800, 400, 1200), family_id="old-wrong-family")
    second = _event(2, pulses=(400, 800, 400, 1200), family_id="old-wrong-family")
    third = _event(3, pulses=(500, 500, 1000, 1000), family_id="old-wrong-family")
    one = StructuralGrouper()
    one.rebuild([third, first, second])
    two = StructuralGrouper()
    two.rebuild([second, third, first])
    assert sorted(one.families) == sorted(two.families)
    assert len(one.families) == 2
    assert first.family_id != "old-wrong-family"
    assert second.family_id == first.family_id
    assert third.family_id != first.family_id


def test_classification_is_a_conservative_hypothesis():
    result = classify(_event(1, repeat_count=3))
    assert result["classification"] == "remote-like"
    assert result["confidence"] < 1.0
    assert classify(_event(2, pulses=(), payload=b""))["classification"] == "unknown"
