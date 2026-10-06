from uplink.rf_analyzer import AnalyzerProject, EventFilter
from uplink.rf_hunter import RfEvent


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
    assert len(project.family_summary()) == 2


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


def test_similarity_explains_structural_match_and_keeps_observations_separate():
    left = _event(1, "2026-10-06T08:00:00Z")
    right = _event(2, "2026-10-06T09:00:00Z")
    result = AnalyzerProject.similarity(left, right)
    assert result["percent"] >= 90
    assert "same modulation" in result["reasons"]
    assert "payload/observation remains separate" in result["reasons"]

