import hashlib
import random
import time
from types import SimpleNamespace

from uplink.rf_fingerprint import (MERGE_SCORE, FeatureCache, StructuralGrouper, classify, compare_events,
                                   extract_features, fingerprint)
from uplink.rf_hunter import RfEvent


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


def synthetic_events(count, seed=7, devices=25):
    """Realistic mix: jittered remotes on three bands, noise bursts, NFC fields, pulse-less events."""
    rng = random.Random(seed)
    bands = (315_000_000, 433_920_000, 868_350_000)
    patterns = []
    for _ in range(devices):
        short = rng.randint(250, 500)
        bits = [rng.choice((short, short * rng.choice((2, 3)))) for _ in range(rng.randint(20, 80))]
        patterns.append((rng.choice(bands), bits))
    events = []
    for seq in range(count):
        roll = rng.random()
        common = dict(device_uuid="bench", session_id=f"s{seq // 500}", sequence_number=seq,
                      captured_at_utc=f"2026-10-06T{(seq // 3600) % 24:02d}:{(seq // 60) % 60:02d}:{seq % 60:02d}Z",
                      monotonic_ms=seq * 10, rssi_avg_dbm=-40 - rng.random() * 50)
        if roll < 0.60:
            freq, bits = patterns[rng.randrange(len(patterns))]
            pulses = tuple(max(1, pulse + rng.randint(-30, 30)) for pulse in bits)
            events.append(RfEvent(frequency_hz=freq + rng.randint(-20_000, 20_000), modulation="OOK",
                                  pulse_timings_us=pulses, **common))
        elif roll < 0.85:
            pulses = tuple(rng.randint(50, 5000) for _ in range(rng.randint(3, 120)))
            events.append(RfEvent(frequency_hz=rng.choice(bands), pulse_timings_us=pulses, **common))
        elif roll < 0.95:
            events.append(RfEvent(source_type="nfc", frequency_hz=13_560_000, modulation="NFC",
                                  nfc_technology="external-field", nfc_protocol="carrier-presence", **common))
        else:
            events.append(RfEvent(frequency_hz=rng.choice(bands), **common))
    return events


def reference_families(events, merge_score=MERGE_SCORE):
    """Naive complete-link grouping as the original implementation did it (no pruning)."""
    cache = FeatureCache()  # only avoids recomputing features; every pair is compared
    families, members = {}, {}
    ordered = sorted(events, key=lambda event: (fingerprint(event), event.event_id))
    result = {}
    for event in ordered:
        features = extract_features(event)
        nfc = features["source_type"] == "nfc"
        best = None
        if features["has_pulses"] or nfc:
            for family_id in sorted(families):
                group = members[family_id]
                if nfc and not all(cache.features(m)["source_type"] == "nfc" for m in group):
                    continue
                if not nfc and not all(cache.features(m)["has_pulses"] for m in group):
                    continue
                results = [compare_events(event, member, cache=cache) for member in group]
                if any(r["relationship"] not in ("same_structure", "variable_payload")
                       or r["score"] < merge_score for r in results):
                    continue
                confidence = min(r["score"] for r in results)
                if best is None or confidence > best[1]:
                    best = (family_id, confidence)
        if best:
            family_id = best[0]
        else:
            provided = event.fingerprint_id
            fp = fingerprint(event) if not provided or provided.startswith("local-") else provided
            base = "family-" + hashlib.sha256(fp.encode()).hexdigest()[:12]
            if not features["has_pulses"] and not nfc:
                base += "-" + hashlib.sha256(event.event_id.encode()).hexdigest()[:6]
            family_id, suffix = base, 2
            while family_id in families:
                family_id = f"{base}-{suffix}"
                suffix += 1
            families[family_id] = True
            members[family_id] = []
        members[family_id].append(event)
        result[event.event_id] = family_id
    return result


def test_same_carrier_different_structure_stays_separate():
    first = _event(1, pulses=(400, 800, 400, 1200))
    second = _event(2, pulses=(500, 500, 1000, 1000))
    grouper = StructuralGrouper()
    assert fingerprint(first) != fingerprint(second)
    assert grouper.assign(first) != grouper.assign(second)
    comparison = compare_events(first, second)
    assert comparison["relationship"] in {"weak_similarity", "unknown"}
    assert comparison["percent"] < 80


def test_flipper_local_fingerprint_is_not_used_and_not_overwritten():
    first = _event(1, fingerprint_id="local-deadbeef")
    second = _event(2, pulses=(500, 500, 1000, 1000), fingerprint_id="local-deadbeef")
    grouper = StructuralGrouper()
    assert grouper.assign(first) != grouper.assign(second)
    assert grouper.fingerprint_for(first) == fingerprint(first)
    assert first.fingerprint_id == "local-deadbeef"  # stored evidence untouched
    assert first.family_id is None


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
    assert grouper.assign(events[0]) == families[0]  # idempotent


def test_passive_nfc_field_events_group_by_carrier_metadata():
    first = _event(1, pulses=(), source_type="nfc", frequency_hz=13_560_000,
                   modulation="NFC", nfc_technology="external-field",
                   nfc_protocol="carrier-presence")
    second = _event(2, pulses=(), source_type="nfc", frequency_hz=13_560_000,
                    modulation="NFC", nfc_technology="external-field",
                    nfc_protocol="carrier-presence")
    grouper = StructuralGrouper()
    assert grouper.assign(first) == grouper.assign(second)
    assert not grouper.families[grouper.family_for(first)]["provisional"]
    result = compare_events(first, second)
    assert result["relationship"] == "same_structure"
    assert "same NFC carrier" in result["reasons"]


def test_rebuild_is_deterministic_splits_wrong_old_family_and_keeps_evidence():
    first = _event(1, pulses=(400, 800, 400, 1200), family_id="old-wrong-family")
    second = _event(2, pulses=(400, 800, 400, 1200), family_id="old-wrong-family")
    third = _event(3, pulses=(500, 500, 1000, 1000), family_id="old-wrong-family")
    one = StructuralGrouper()
    one.rebuild([third, first, second])
    two = StructuralGrouper()
    two.rebuild([second, third, first])
    assert sorted(one.families) == sorted(two.families)
    assert one.family_of == two.family_of
    assert len(one.families) == 2
    assert one.family_for(first) != "old-wrong-family"
    assert one.family_for(second) == one.family_for(first)
    assert one.family_for(third) != one.family_for(first)
    assert {event.family_id for event in (first, second, third)} == {"old-wrong-family"}


def test_classification_is_a_conservative_hypothesis():
    result = classify(_event(1, repeat_count=3))
    assert result["classification"] == "remote-like"
    assert result["confidence"] < 1.0
    assert classify(_event(2, pulses=(), payload=b""))["classification"] == "unknown"


def test_feature_cache_follows_event_content():
    cache = FeatureCache()
    event = _event(1)
    assert cache.features(event) is cache.features(_event(1))  # same content, shared
    assert cache.features(event)["timing"]["pulse_count"] == 4
    event.pulse_timings_us = (400, 800, 400, 1200, 400, 800)
    assert cache.features(event)["timing"]["pulse_count"] == 6  # mutation is seen
    cache.prune([event])
    assert len(cache) == 1


def test_identical_fingerprint_but_incompatible_events_get_distinct_family_ids():
    # Same preamble and pulse multiset (same canonical structure) but the tail
    # in another order: the positional ratio comparison keeps them apart, so
    # the two families must not share (and overwrite) one ID.
    preamble = (400, 800, 400, 1200, 400, 800, 400, 1200)
    first = _event(1, pulses=preamble + (400,) * 8 + (800,) * 8 + (1200,) * 8)
    second = _event(2, pulses=preamble + (800,) * 8 + (1200,) * 8 + (400,) * 8)
    assert fingerprint(first) == fingerprint(second)
    grouper = StructuralGrouper()
    grouper.rebuild([first, second])
    assert grouper.family_for(first) != grouper.family_for(second)
    assert len(grouper.families) == 2
    assert all(family["event_ids"] for family in grouper.families.values())


def test_pruned_grouping_matches_naive_complete_link():
    events = synthetic_events(220, seed=3)
    grouper = StructuralGrouper()
    grouper.rebuild(events)
    assert grouper.family_of == reference_families(events)
    assert all(event.family_id is None and event.fingerprint_id == "" for event in events)


def test_rebuild_performance_regression():
    events = synthetic_events(300, seed=11)
    started = time.perf_counter()
    StructuralGrouper().rebuild(events)
    assert time.perf_counter() - started < 3.0
