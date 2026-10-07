import json
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from uplink import rf_analyzer
from uplink.rf_analyzer import (AnalyzerProject, EventFilter, main, parse_frequency_filter,
                                parse_time_filter, sync_status_text, waterfall_index_at)
from uplink.rf_hunter import EventStore, RfEvent


def _event(seq, when, family="family-1", freq=433920000, rssi=-50):
    return RfEvent(
        device_uuid=f"device-{seq % 2}", session_id="session", sequence_number=seq,
        captured_at_utc=when, monotonic_ms=seq, frequency_hz=freq, modulation="OOK",
        rssi_avg_dbm=rssi, family_id=family, pulse_timings_us=(400, 800, 400))


def _flipper_record(seq, pulses=(400, 800, 400, 800)):
    return {"schema_version": 1, "event_id": f"rf-dev-s1-{seq}", "device_uuid": "dev",
            "session_id": "s1", "sequence_number": seq,
            "captured_at_utc": f"2026-10-06T20:00:{seq:02d}Z", "monotonic_ms": seq,
            "frequency_hz": 433920000, "fingerprint_id": f"local-{seq:08x}", "family_id": None,
            "pulse_timings_us": list(pulses), "upload_state": "pending"}


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
    project = AnalyzerProject()
    project.add_store(store)
    assert project.capture_bytes(event) == b"raw pulse capture"
    assert project.has_capture(event)


def test_authoritative_grouping_merges_structure_and_splits_distinct_shape():
    first = _event(10, "2026-10-06T08:00:00Z")
    second = _event(11, "2026-10-06T08:01:00Z")
    distinct = _event(12, "2026-10-06T08:02:00Z")
    distinct.pulse_timings_us = (100, 1500, 100)
    project = AnalyzerProject()
    project.events = {event.event_id: event for event in (first, second, distinct)}
    project.rebuild_families()
    family_map = {event.event_id: project.family_key(event) for event in project.events.values()}
    assert family_map[first.event_id] == family_map[second.event_id]
    assert family_map[first.event_id] != family_map[distinct.event_id]
    reverse = AnalyzerProject()
    reverse.events = {event.event_id: event for event in (distinct, second, first)}
    reverse.rebuild_families()
    assert {key: reverse.family_key(event) for key, event in reverse.events.items()} == family_map
    assert {event.family_id for event in (first, second, distinct)} == {"family-1"}  # evidence kept


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


def test_family_detail_and_similarity_reasons_are_investigation_ready():
    first = _event(70, "2026-10-06T08:00:00Z", rssi=-42)
    second = _event(71, "2026-10-06T18:00:00Z", rssi=-66)
    second.fingerprint_id = "variant-b"
    second.upload_state = "imported"
    project = AnalyzerProject()
    project.events = {first.event_id: first, second.event_id: second}
    project.rebuild_families()
    family = project.family_key(first)
    detail = project.family_detail(family)
    assert detail["observation_count"] == 2
    assert detail["waveform_variants"]
    assert detail["time_of_day_hours"] == [8, 18]
    assert detail["imported_count"] == 1
    similar = project.similar_events(first)
    assert similar and similar[0]["event"].event_id == second.event_id
    assert "distinct observation IDs preserved" in similar[0]["comparison"]["reasons"]


def test_grouping_and_export_never_rewrite_flipper_evidence(tmp_path):
    journal = tmp_path / "sd" / "apps_data" / "dedsec_uplink" / "rf"
    (journal / "events").mkdir(parents=True)
    for seq in range(1, 6):
        (journal / "events" / f"rf-dev-s1-{seq}.json").write_text(json.dumps(_flipper_record(seq)),
                                                                   encoding="utf-8")
    before = {path.name: path.read_bytes() for path in (journal / "events").iterdir()}
    project = AnalyzerProject()
    project.add_root(journal / "events")  # the events folder itself is accepted
    assert len(project.events) == 5
    project.rebuild_families()
    families = {project.family_key(event) for event in project.events.values()}
    assert len(families) == 1 and "unassigned" not in families
    out = tmp_path / "export.json"
    project.export_json(out)
    rows = json.loads(out.read_text(encoding="utf-8"))["events"]
    assert all(row["fingerprint_id"].startswith("local-") for row in rows)
    assert all(row["desktop_family_id"] in families for row in rows)
    assert all(event.fingerprint_id.startswith("local-") and event.family_id is None
               for event in project.events.values())
    after = {path.name: path.read_bytes() for path in (journal / "events").iterdir()}
    assert after == before
    with pytest.raises(PermissionError):
        project.sources[str(journal)].add(_event(1, "2026-10-06T08:00:00Z"))


def test_families_are_rebuilt_only_when_the_event_set_changes(tmp_path):
    store = EventStore(tmp_path)
    for seq in range(1, 4):
        store.add(RfEvent.from_dict(_flipper_record(seq)), b"raw")
    project = AnalyzerProject()
    project.add_root(tmp_path)
    project.filtered()
    project.filtered(EventFilter(text="rf-dev"))
    project.family_summary()
    first = next(iter(project.events.values()))
    project.family_detail(project.family_key(first))
    project.similar_events(first)
    assert project.rebuild_count == 1
    store.add(RfEvent.from_dict(_flipper_record(9)), b"raw")
    project.reload_root(str(tmp_path))
    project.filtered()
    assert project.rebuild_count == 2 and len(project.events) == 4
    project.reload_root(str(tmp_path))  # same records, new objects
    project.filtered()
    assert project.rebuild_count == 3


def test_frequency_filter_accepts_tolerance_and_ranges():
    assert parse_frequency_filter("") is None
    assert parse_frequency_filter("433.92") == (433_720_000, 434_120_000)
    assert parse_frequency_filter("433,92 MHz") == (433_720_000, 434_120_000)
    assert parse_frequency_filter("433-434") == (433_000_000, 434_000_000)
    assert parse_frequency_filter("434..433.5") == (433_500_000, 434_000_000)
    assert parse_frequency_filter("433.92+-0.05") == (433_870_000, 433_970_000)
    assert parse_frequency_filter("433.92 ± 0.1") == (433_820_000, 434_020_000)
    for bad in ("abc", "433-", "-433", "433.92 MHz extra"):
        with pytest.raises(ValueError):
            parse_frequency_filter(bad)
    project = AnalyzerProject()
    event = _event(1, "2026-10-06T08:00:00Z", freq=433_920_000)
    project.events = {event.event_id: event}
    low, high = parse_frequency_filter("433.9")
    assert project.filtered(EventFilter(min_frequency_hz=low, max_frequency_hz=high)) == [event]


def test_time_filter_rejects_garbage_instead_of_1970():
    assert parse_time_filter("") is None
    assert parse_time_filter("2026-10-06") == datetime(2026, 10, 6, tzinfo=timezone.utc)
    end = parse_time_filter("2026-10-06", end=True)
    assert end == datetime(2026, 10, 6, 23, 59, 59, 999999, tzinfo=timezone.utc)
    assert parse_time_filter("2026-10-06 08:00") == datetime(2026, 10, 6, 8, tzinfo=timezone.utc)
    assert parse_time_filter("2026-10-06T08:00:00Z") == datetime(2026, 10, 6, 8, tzinfo=timezone.utc)
    assert parse_time_filter("2026-10-06T11:00+03:00") == datetime(2026, 10, 6, 8, tzinfo=timezone.utc)
    for bad in ("yesterday", "06.10.2026", "2026-13-01"):
        with pytest.raises(ValueError):
            parse_time_filter(bad)


def test_waterfall_click_maps_to_the_drawn_row():
    # Three rows on a 330 px canvas: oldest at the bottom, newest at the top.
    assert waterfall_index_at(320, 330, 3) == 0
    assert waterfall_index_at(165, 330, 3) == 1
    assert waterfall_index_at(10, 330, 3) == 2
    # A full canvas (27 rows of 12 px): the top pixel is the newest drawn row.
    assert waterfall_index_at(0, 330, 27) == 26
    assert waterfall_index_at(329, 330, 27) == 0
    assert waterfall_index_at(10, 330, 0) is None


def test_load_problems_are_reported(tmp_path):
    good = RfEvent.from_dict(_flipper_record(1))
    (tmp_path / "events.jsonl").write_text(json.dumps(good.to_dict()) + "\n{bad\n", encoding="utf-8")
    project = AnalyzerProject()
    project.add_root(tmp_path)
    count, messages = project.load_problems()
    assert count == 1 and messages and len(project.events) == 1


def test_sync_status_text_summarizes_rfsync_status():
    summary, error = sync_status_text({"link_up": True, "pending": 3, "free_kb": 2048, "state": 1,
                                       "errors": 0, "imported": 5, "failed": 1, "last_error": "boom",
                                       "syncing": True, "current_progress": "240/700",
                                       "last_sync": 1791316800.0})
    assert "3 pending" in summary and "Sub-GHz RX" in summary and "imported 5" in summary
    assert "240/700" in summary and error == "Last error: boom"
    assert "not connected" in sync_status_text({"link_up": False})[0]
    assert "Viewer only" in sync_status_text(None)[0]


def test_main_exports_and_survives_windowed_python(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    store = EventStore(tmp_path / "store")
    store.add(RfEvent.from_dict(_flipper_record(1)), b"raw")
    out = tmp_path / "out.json"
    csv_out = tmp_path / "out.csv"
    assert main([str(tmp_path / "store"), "--export-json", str(out), "--export-csv", str(csv_out)]) == 0
    assert len(json.loads(out.read_text(encoding="utf-8"))["events"]) == 1
    assert "desktop_family_id" in csv_out.read_text(encoding="utf-8").splitlines()[0]

    shown = []
    monkeypatch.setattr(rf_analyzer, "_show_message", lambda title, text, error=False: shown.append(text))
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    assert main(["--help"]) == 0
    assert shown and "usage" in shown[-1].lower()
    assert main(["--no-such-option"]) == 2
    assert "unrecognized arguments" in shown[-1]


class FakeSync:
    def __init__(self):
        self.calls = 0

    def status(self):
        return {"link_up": True, "pending": 2, "stored": 4, "free_kb": 1024, "state": 1, "errors": 0,
                "imported": self.calls, "failed": 0, "last_error": "", "syncing": False,
                "last_sync": None}

    def sync_now(self):
        self.calls += 1


def test_analyzer_window_smoke(tmp_path):
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError as exc:  # pragma: no cover - headless CI
        pytest.skip(f"no display: {exc}")
    root.withdraw()
    try:
        store_root = tmp_path / "store"
        writer = EventStore(store_root)
        for seq in range(1, 4):
            writer.add(RfEvent.from_dict(_flipper_record(seq)), b"raw")
        journal = tmp_path / "copied" / "events"
        journal.mkdir(parents=True)
        (journal / "rf-dev-s1-40.json").write_text(json.dumps(_flipper_record(40)), encoding="utf-8")
        closed = []
        sync = FakeSync()
        window = rf_analyzer.AnalyzerWindow(root, str(store_root), sync=sync, extra_roots=[str(journal)],
                                            on_close=lambda: closed.append(True))
        window.window.geometry("1200x800")
        root.update()
        window.refresh()
        root.update()
        assert len(window.project.events) == 4
        assert "2 pending" in window.sync_label.cget("text")
        assert "read-only" in window.store_label.cget("text")
        window.sync_now()
        assert sync.calls == 1

        # A record committed by the companion shows up after the folder poll.
        writer.add(RfEvent.from_dict(_flipper_record(9)), b"raw")
        window._poll_folders()
        root.update()
        assert len(window.project.events) == 5

        # Clicking the top row of the waterfall selects the newest event.
        assert window._waterfall_rows
        window.canvas_event(SimpleNamespace(x=10, y=1))
        assert window._selected_event == window._waterfall_rows[-1]["event_id"]
        newest = max(window.project.events.values(), key=lambda event: event.captured_at_utc)
        assert window._selected_event == newest.event_id

        # Filters: invalid input is reported and ignored, not turned into 1970.
        window.start_time.set("not a date")
        window.refresh()
        assert "From" in window.filter_error.cget("text")
        assert len(window._events) == 5
        window.start_time.set("")
        window.frequency.set("433.9")
        window.refresh()
        assert len(window._events) == 5

        # A second window can share the same Tk root (single companion UI thread).
        other = rf_analyzer.AnalyzerWindow(root, str(store_root))
        root.update()
        other.close()
        assert window.alive and not other.alive
        window.close()
        window.close()  # idempotent
        assert closed == [True]
        root.update()
    finally:
        root.destroy()
