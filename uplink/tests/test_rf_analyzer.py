from uplink.rf_analyzer import AnalyzerProject, EventFilter
from uplink.rf_hunter import EventStore, RfEvent
import asyncio


def _event(seq, when, family="family-1", freq=433920000, rssi=-50):
    return RfEvent(
        device_uuid=f"device-{seq % 2}", session_id="session", sequence_number=seq,
        captured_at_utc=when, monotonic_ms=seq, frequency_hz=freq, modulation="OOK",
        rssi_avg_dbm=rssi, family_id=family, pulse_timings_us=(400, 800, 400))


def test_project_merges_multiple_flippers_and_deduplicates():
    project = AnalyzerProject()
    first = _event(1, "2026-10-06T08:00:00Z")
    duplicate = RfEvent.from_dict(first.to_dict())
    second = _event(2, "2026-10-06T09:00:00Z", family="family-2")
    class Store:
        root = "one"
        events = {first.event_id: first, duplicate.event_id: duplicate}
    project.add_store(Store())
    class Other:
        root = "two"
        events = {second.event_id: second}
    project.add_store(Other())
    assert len(project.events) == 2
    summary = project.family_summary()
    assert len(summary) == 1
    assert summary[0]["observation_count"] == 2


def test_filters_and_views_produce_timeline_spectrum_waterfall():
    project = AnalyzerProject()
    rows = [_event(1, "2026-10-06T08:00:00Z", rssi=-40),
            _event(2, "2026-10-06T09:00:00Z", rssi=-80)]
    project.events = {row.event_id: row for row in rows}
    filtered = project.filtered(EventFilter(min_rssi=-60))
    assert [row.sequence_number for row in filtered] == [1]
    assert len(project.timeline(rows)) == 2
    assert len(project.waterfall(rows)) == 2
    assert any(point["count"] for point in project.spectrum(rows))

    date_filtered = project.filtered(EventFilter(start=project.timeline(rows)[0]["when"]))
    assert len(date_filtered) == 2
    freq_filtered = project.filtered(EventFilter(min_frequency_hz=434000000))
    assert not freq_filtered


def test_similarity_explains_structural_match_and_keeps_observations_separate():
    left = _event(1, "2026-10-06T08:00:00Z")
    right = _event(2, "2026-10-06T09:00:00Z")
    result = AnalyzerProject.similarity(left, right)
    assert result["percent"] >= 90
    assert "same modulation" in result["reasons"]
    assert "distinct observation IDs preserved" in result["reasons"]


def test_raw_capture_is_available_to_detail_view(tmp_path):
    store = EventStore(tmp_path)
    event = _event(3, "2026-10-06T10:00:00Z")
    store.add(event, b"raw pulse capture")
    project = AnalyzerProject(); project.add_store(store)
    assert project.capture_bytes(event) == b"raw pulse capture"


def test_authoritative_grouping_merges_structure_and_splits_distinct_shape():
    first = _event(10, "2026-10-06T08:00:00Z")
    second = _event(11, "2026-10-06T08:01:00Z")
    distinct = _event(12, "2026-10-06T08:02:00Z")
    distinct.pulse_timings_us = (100, 1500, 100)
    project = AnalyzerProject()
    project.events = {event.event_id: event for event in (first, second, distinct)}
    project.rebuild_families()
    family_map = {event.event_id: event.family_id for event in project.events.values()}
    assert family_map[first.event_id] == family_map[second.event_id]
    assert family_map[first.event_id] != family_map[distinct.event_id]
    reverse = AnalyzerProject()
    reverse.events = {event.event_id: event for event in (distinct, second, first)}
    reverse.rebuild_families()
    assert {key: event.family_id for key, event in reverse.events.items()} == family_map


def test_ble_sync_uses_durable_project_store(tmp_path):
    project = AnalyzerProject()
    target = EventStore(tmp_path)
    event = _event(42, "2026-10-06T11:00:00Z")

    class Adapter:
        async def sync_to(self, store):
            store.add(event, b"wire-event")
            return {"seen": 1, "imported": 1, "skipped": 0}

    result = asyncio.run(project.sync_ble(Adapter(), target))
    assert result["imported"] == 1
    assert event.event_id in project.events
    assert project.capture_bytes(event) == b"wire-event"


def test_project_dedup_prefers_duplicate_with_raw_capture(tmp_path):
    event = _event(50, "2026-10-06T12:00:00Z")
    first = EventStore(tmp_path / "first")
    first.add(event)
    second = EventStore(tmp_path / "second")
    second.add(RfEvent.from_dict(event.to_dict()), b"durable raw")
    project = AnalyzerProject()
    project.add_store(first)
    project.add_store(second)
    assert list(project.events) == [event.event_id]
    assert project.capture_bytes(project.events[event.event_id]) == b"durable raw"


def test_timeline_is_sorted_by_utc_investigation_time():
    project = AnalyzerProject()
    late = _event(61, "2026-10-06T12:00:00Z")
    early = _event(62, "2026-10-06T11:00:00Z")
    project.events = {late.event_id: late, early.event_id: early}
    rows = project.timeline()
    assert [row["event_id"] for row in rows] == [early.event_id, late.event_id]
