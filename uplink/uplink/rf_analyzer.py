"""Desktop RF Signal Hunter analyzer.

The engine is headless and works with any number of Flipper event stores.  The
optional Tkinter front end renders sampled RF observations as a waterfall,
spectrum, timeline and family comparison view.  It never interprets the data
as continuous IQ: the inputs are discrete bursts, RSSI values and pulse timing.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import os
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence

from .rf_fingerprint import StructuralGrouper, compare_events
from .rf_hunter import EventStore, RfEvent


def parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass
class EventFilter:
    source_type: str = ""
    family_id: str = ""
    text: str = ""
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    min_rssi: Optional[float] = None
    max_rssi: Optional[float] = None
    min_frequency_hz: Optional[int] = None
    max_frequency_hz: Optional[int] = None


class AnalyzerProject:
    """Merged view of one or more Flipper stores, deduplicated by event ID."""

    def __init__(self):
        self.sources: dict[str, EventStore] = {}
        self.events: dict[str, RfEvent] = {}
        self.grouper = StructuralGrouper()
        self.notes: dict[str, dict] = {}
        self.settings: dict = {"selected_family": "", "timezone": "UTC"}

    def add_root(self, root) -> int:
        root = os.fspath(root)
        store = EventStore(root)
        self.sources[root] = store
        self._merge_store(store)
        self.rebuild_families()
        return len(store.events)

    def add_store(self, store: EventStore) -> int:
        self.sources[store.root] = store
        self._merge_store(store)
        self.rebuild_families()
        return len(store.events)

    def _merge_store(self, store):
        """Merge a journal without letting a transport duplicate replace data.

        The same event ID may be present in two Flipper exports (or in a
        copied per-event journal and its JSONL mirror).  Keep one observation,
        preferring the representation whose capture blob is actually readable.
        Metadata from a later source is only used when the existing record has
        no raw capture at all.
        """
        for event_id, event in store.events.items():
            current = self.events.get(event_id)
            if current is None:
                self.events[event_id] = event
                continue
            # ``store`` is already registered by add_root/add_store. Exclude
            # it while checking the existing event, otherwise a duplicate's
            # new capture would make the old metadata look complete.
            existing_sources = [candidate for root, candidate in self.sources.items()
                                if root != store.root]
            current_capture = self._capture_bytes_from(current, existing_sources)
            incoming_capture = b""
            try:
                if hasattr(store, "read_capture"):
                    incoming_capture = store.read_capture(event)
                elif event.capture_blob:
                    with open(os.path.join(store.root, event.capture_blob), "rb") as fh:
                        incoming_capture = fh.read()
            except (OSError, ValueError):
                incoming_capture = b""
            if incoming_capture and not current_capture:
                self.events[event_id] = event
            elif current.upload_state != "uploaded" and event.upload_state == "uploaded":
                current.upload_state = "uploaded"

    def capture_bytes(self, event: RfEvent) -> bytes:
        """Read the optional raw capture blob from whichever source owns it."""
        return self._capture_bytes_from(event, self.sources.values())

    @staticmethod
    def _capture_bytes_from(event: RfEvent, stores) -> bytes:
        for store in stores:
            if store.events.get(event.event_id) is not event and event.event_id not in store.events:
                continue
            if hasattr(store, "read_capture"):
                capture = store.read_capture(event)
                # A duplicate ID may exist in an earlier journal without its
                # raw payload (for example after ACK reclamation).  Continue
                # searching later sources for a durable copy.
                if capture:
                    return capture
                continue
            if not event.capture_blob:
                continue
            try:
                with open(os.path.join(store.root, event.capture_blob), "rb") as fh:
                    return fh.read()
            except (OSError, ValueError):
                continue
        return b""

    def clear(self):
        self.sources.clear()
        self.events.clear()
        self.grouper = StructuralGrouper()
        self.notes.clear()
        self.settings = {"selected_family": "", "timezone": "UTC"}

    def rebuild_families(self):
        """Recompute authoritative structural families deterministically."""
        return self.grouper.rebuild(self.events.values())

    def add_note(self, key, text, location=""):
        self.notes[str(key)] = {"text": str(text), "location": str(location)}

    def note(self, key):
        return self.notes.get(str(key), {"text": "", "location": ""})

    def save_project(self, path):
        payload = {"version": 1, "roots": list(self.sources), "settings": self.settings,
                   "notes": self.notes}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)

    def load_project(self, path):
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        self.clear()
        for root in payload.get("roots", []):
            self.add_root(root)
        self.settings.update(payload.get("settings") or {})
        self.notes.update(payload.get("notes") or {})

    async def sync_ble(self, adapter, frames=None):
        """Import the Flipper journal through the current BLE adapter.

        ``BleakRfAdapter`` exposes the pull API as ``sync_to(store)``.  Older
        callers passed an iterable of prepared frames to an adapter with an
        ``import_events`` method, so retain that fallback while making the
        real C profile path use this project's durable :class:`EventStore`.
        ``frames`` may be an EventStore or a project-root path; when omitted,
        the first already-open project source is used.
        """
        if hasattr(adapter, "sync_to"):
            target = frames if isinstance(frames, EventStore) else None
            if target is None and isinstance(frames, (str, os.PathLike)):
                target = EventStore(frames)
            if target is None and self.sources:
                target = next(iter(self.sources.values()))
            if target is None:
                raise ValueError("an EventStore or project root is required for RF BLE sync")
            result = await adapter.sync_to(target)
            self.add_store(target)
            return result
        if hasattr(adapter, "import_events"):
            return await adapter.import_events(frames or [])
        raise TypeError("adapter does not implement RF BLE sync")

    def sync_ble_blocking(self, adapter, frames=None):
        return asyncio.run(self.sync_ble(adapter, frames))

    def follow_profile(self, family_id):
        rows = [event for event in self.events.values() if self.family_key(event) == family_id]
        if not rows:
            return None
        latest = max(rows, key=lambda event: parse_time(event.captured_at_utc))
        return {"version": 1, "profile_id": family_id,
                "frequency_hz": latest.frequency_hz, "modulation": latest.modulation,
                "fingerprint_id": latest.fingerprint_id, "pulse_timings_us": list(latest.pulse_timings_us),
                "tolerance_us": 500, "passive_only": True}

    def export_follow(self, family_id, path):
        profile = self.follow_profile(family_id)
        if profile is None:
            raise KeyError(family_id)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(profile, fh, ensure_ascii=False, indent=2)

    def filtered(self, spec: Optional[EventFilter] = None) -> list[RfEvent]:
        spec = spec or EventFilter()
        text = spec.text.casefold()
        rows = []
        for event in self.events.values():
            when = parse_time(event.captured_at_utc)
            family = event.family_id or event.fingerprint_id or "unassigned"
            haystack = " ".join((event.event_id, event.device_uuid, event.modulation,
                                  event.classification, family)).casefold()
            if spec.source_type and event.source_type != spec.source_type:
                continue
            if spec.family_id and family != spec.family_id:
                continue
            if text and text not in haystack:
                continue
            if spec.start and when < spec.start:
                continue
            if spec.end and when > spec.end:
                continue
            if spec.min_rssi is not None and event.rssi_avg_dbm < spec.min_rssi:
                continue
            if spec.max_rssi is not None and event.rssi_avg_dbm > spec.max_rssi:
                continue
            if spec.min_frequency_hz is not None and event.frequency_hz < spec.min_frequency_hz:
                continue
            if spec.max_frequency_hz is not None and event.frequency_hz > spec.max_frequency_hz:
                continue
            rows.append(event)
        return sorted(rows, key=lambda event: (parse_time(event.captured_at_utc), event.event_id))

    @staticmethod
    def family_key(event: RfEvent) -> str:
        return event.family_id or event.fingerprint_id or "unassigned"

    def family_summary(self, events: Optional[Iterable[RfEvent]] = None) -> list[dict]:
        self.rebuild_families()
        groups = {}
        for event in events or self.events.values():
            key = self.family_key(event)
            group = groups.setdefault(key, {"family_id": key, "events": [], "frequencies": [],
                                            "rssis": [], "modulations": set()})
            group["events"].append(event)
            if event.frequency_hz:
                group["frequencies"].append(event.frequency_hz)
            group["rssis"].append(event.rssi_avg_dbm)
            group["modulations"].add(event.modulation or "unknown")
        result = []
        for group in groups.values():
            frequencies = group["frequencies"]
            result.append({
                "family_id": group["family_id"],
                "observation_count": len(group["events"]),
                "first_seen": min((e.captured_at_utc for e in group["events"]), default=""),
                "last_seen": max((e.captured_at_utc for e in group["events"]), default=""),
                "frequency_min_hz": min(frequencies, default=0),
                "frequency_max_hz": max(frequencies, default=0),
                "frequency_hz": int(statistics.median(frequencies)) if frequencies else 0,
                "modulation": ", ".join(sorted(group["modulations"])),
                "rssi_avg_dbm": statistics.mean(group["rssis"]) if group["rssis"] else 0.0,
                "event_ids": [event.event_id for event in group["events"]],
                "confidence": self.grouper.families.get(group["family_id"], {}).get("confidence", 0.0),
                "provisional": self.grouper.families.get(group["family_id"], {}).get("provisional", True),
            })
        return sorted(result, key=lambda row: (-row["observation_count"], row["family_id"]))

    def family_detail(self, family_id: str, events: Optional[Iterable[RfEvent]] = None) -> dict:
        """Return the evidence used to explain one signal family.

        The detail payload intentionally keeps observations separate while
        exposing the aggregate values the investigation UI needs: waveform
        variants, RSSI/time-of-day distributions, source hypothesis,
        similarity confidence, Follow state and raw/import status.
        """
        family_id = str(family_id or "")
        rows = [event for event in (events if events is not None else self.events.values())
                if self.family_key(event) == family_id]
        if not rows:
            return {"family_id": family_id, "observation_count": 0, "event_ids": []}
        frequencies = [event.frequency_hz for event in rows if event.frequency_hz]
        rssis = [float(event.rssi_avg_dbm) for event in rows]
        variants = sorted({event.fingerprint_id or "unknown" for event in rows})
        classifications = {}
        for event in rows:
            label = event.classification or "unknown"
            classifications[label] = classifications.get(label, 0) + 1
        source_types = sorted({event.source_type or "unknown" for event in rows})
        hours = [parse_time(event.captured_at_utc).hour for event in rows]
        raw_count = sum(bool(self.capture_bytes(event)) for event in rows)
        uploaded = sum(event.upload_state in {"uploaded", "imported"} for event in rows)
        family = self.grouper.families.get(family_id, {})
        return {
            "family_id": family_id,
            "observation_count": len(rows),
            "event_ids": [event.event_id for event in rows],
            "first_seen": min(event.captured_at_utc for event in rows),
            "last_seen": max(event.captured_at_utc for event in rows),
            "frequency_min_hz": min(frequencies, default=0),
            "frequency_max_hz": max(frequencies, default=0),
            "modulations": sorted({event.modulation or "unknown" for event in rows}),
            "waveform_variants": variants,
            "rssi_min_dbm": min(rssis, default=0.0),
            "rssi_max_dbm": max(rssis, default=0.0),
            "rssi_avg_dbm": statistics.mean(rssis) if rssis else 0.0,
            "time_of_day_hours": sorted(set(hours)),
            "source_types": source_types,
            "source_hypothesis": max(classifications, key=classifications.get) if classifications else "unknown",
            "classification_counts": classifications,
            "classification_confidence": statistics.mean(
                [float(event.classification_confidence) for event in rows]) if rows else 0.0,
            "similarity_confidence": family.get("confidence", 0.0),
            "provisional": family.get("provisional", True),
            "follow_selected": any(bool(event.follow_profile_id) for event in rows),
            "raw_capture_count": raw_count,
            "imported_count": uploaded,
            "pending_count": len(rows) - uploaded,
        }

    def similar_events(self, selected: RfEvent, limit: int = 8,
                       candidates: Optional[Iterable[RfEvent]] = None) -> list[dict]:
        """Return nearest observations and the plain-language match reasons."""
        pool = candidates if candidates is not None else self.events.values()
        rows = []
        for event in pool:
            if event.event_id == selected.event_id:
                continue
            comparison = self.similarity(selected, event)
            rows.append({"event": event, "comparison": comparison})
        rows.sort(key=lambda row: (-float(row["comparison"].get("score", 0.0)),
                                  parse_time(row["event"].captured_at_utc), row["event"].event_id))
        return rows[:max(0, int(limit))]

    def timeline(self, events: Optional[Iterable[RfEvent]] = None) -> list[dict]:
        rows = [{"event_id": event.event_id, "when": parse_time(event.captured_at_utc),
                 "family_id": self.family_key(event), "frequency_hz": event.frequency_hz,
                 "rssi_dbm": event.rssi_avg_dbm, "source_type": event.source_type}
                for event in (events if events is not None else self.events.values())]
        return sorted(rows, key=lambda row: (row["when"], row["event_id"]))

    def spectrum(self, events: Optional[Iterable[RfEvent]] = None, bins=96) -> list[dict]:
        """Aggregate sampled observations into frequency/RSSI bins."""
        rows = list(events if events is not None else self.events.values())
        frequencies = [event.frequency_hz for event in rows if event.frequency_hz]
        if not frequencies:
            return []
        low, high = min(frequencies), max(frequencies)
        span = max(1, high - low)
        buckets = [{"frequency_hz": low + int(span * i / max(1, bins - 1)),
                    "rssi_dbm": None, "count": 0} for i in range(bins)]
        values = [[] for _ in range(bins)]
        for event in rows:
            if not event.frequency_hz:
                continue
            index = min(bins - 1, max(0, int((event.frequency_hz - low) * (bins - 1) / span)))
            values[index].append(event.rssi_avg_dbm)
        for bucket, values_at in zip(buckets, values):
            if values_at:
                bucket["rssi_dbm"] = statistics.mean(values_at)
                bucket["count"] = len(values_at)
        return buckets

    def waterfall(self, events: Optional[Iterable[RfEvent]] = None) -> list[dict]:
        """Return one sampled intensity row per event for a waterfall renderer."""
        rows = list(events if events is not None else self.events.values())
        return [{"event_id": event.event_id, "when": event.captured_at_utc,
                 "frequency_hz": event.frequency_hz, "rssi_dbm": event.rssi_avg_dbm,
                 "family_id": self.family_key(event),
                 "pulse_timings_us": list(event.pulse_timings_us)} for event in
                sorted(rows, key=lambda item: parse_time(item.captured_at_utc))]

    @staticmethod
    def similarity(left: RfEvent, right: RfEvent) -> dict:
        result = dict(compare_events(left, right))
        reasons = list(result.get("reasons", ()))
        if left.event_id != right.event_id and "distinct observation IDs preserved" not in reasons:
            reasons.append("distinct observation IDs preserved")
        result["reasons"] = reasons
        return result

    def export_json(self, path, events: Optional[Iterable[RfEvent]] = None):
        rows = [event.to_dict() for event in (events if events is not None else self.events.values())]
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"schema_version": 1, "exported_at_utc": datetime.now(timezone.utc).isoformat(),
                       "events": rows}, fh, ensure_ascii=False, indent=2)

    def export_csv(self, path, events: Optional[Iterable[RfEvent]] = None):
        rows = list(events if events is not None else self.events.values())
        fields = ["event_id", "device_uuid", "session_id", "sequence_number", "captured_at_utc",
                  "source_type", "frequency_hz", "modulation", "nfc_technology", "nfc_protocol",
                  "nfc_field_duration_ms", "rssi_avg_dbm", "duration_us",
                  "family_id", "fingerprint_id", "classification", "upload_state"]
        with open(path, "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            for event in rows:
                writer.writerow({field: getattr(event, field) for field in fields})


class RfHunterApp:
    """Small dark Tkinter investigation console using only standard library GUI APIs."""

    def __init__(self, project=None):
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
        self.tk = tk
        self.filedialog = filedialog
        self.messagebox = messagebox
        self.ttk = ttk
        self.project = project or AnalyzerProject()
        self.root = tk.Tk()
        self.root.title("RF Signal Hunter // Investigation Console")
        self.root.geometry("1280x820")
        self.root.configure(bg="#071018")
        self._selected_family = ""
        self._selected_event = ""
        self._sync_thread = None
        self._sync_target = None
        self._build()

    def _build(self):
        tk, ttk = self.tk, self.ttk
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background="#071018")
        style.configure("TLabel", background="#071018", foreground="#b6d7de")
        style.configure("TButton", background="#11303a", foreground="#d9f8ff")
        header = ttk.Frame(self.root); header.pack(fill="x", padx=10, pady=8)
        ttk.Label(header, text="RF SIGNAL HUNTER", font=("Segoe UI", 16, "bold")).pack(side="left")
        ttk.Button(header, text="Open Flipper store", command=self.open_store).pack(side="left", padx=16)
        ttk.Button(header, text="Sync BLE", command=self.sync_ble).pack(side="left", padx=5)
        ttk.Button(header, text="Export JSON", command=lambda: self.export("json")).pack(side="left")
        ttk.Button(header, text="Export CSV", command=lambda: self.export("csv")).pack(side="left", padx=5)
        ttk.Button(header, text="Project", command=self.save_project).pack(side="left", padx=5)
        ttk.Button(header, text="Load project", command=self.load_project).pack(side="left")
        self.status = ttk.Label(header, text="No observations loaded"); self.status.pack(side="right")
        filters = ttk.Frame(self.root); filters.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Label(filters, text="Search").pack(side="left")
        self.search = tk.StringVar(); ttk.Entry(filters, textvariable=self.search, width=28).pack(side="left", padx=5)
        self.source = tk.StringVar(); ttk.Label(filters, text="Source").pack(side="left", padx=(12, 0))
        ttk.Entry(filters, textvariable=self.source, width=12).pack(side="left", padx=5)
        self.frequency = tk.StringVar(); ttk.Label(filters, text="MHz").pack(side="left", padx=(8, 0))
        ttk.Entry(filters, textvariable=self.frequency, width=14).pack(side="left", padx=4)
        self.rssi = tk.StringVar(); ttk.Label(filters, text="RSSI≥").pack(side="left", padx=(8, 0))
        ttk.Entry(filters, textvariable=self.rssi, width=7).pack(side="left", padx=4)
        self.max_rssi = tk.StringVar(); ttk.Label(filters, text="≤").pack(side="left")
        ttk.Entry(filters, textvariable=self.max_rssi, width=7).pack(side="left", padx=4)
        self.start_time = tk.StringVar(); ttk.Label(filters, text="From UTC").pack(side="left", padx=(8, 0))
        ttk.Entry(filters, textvariable=self.start_time, width=19).pack(side="left", padx=4)
        self.end_time = tk.StringVar(); ttk.Label(filters, text="To").pack(side="left")
        ttk.Entry(filters, textvariable=self.end_time, width=19).pack(side="left", padx=4)
        ttk.Button(filters, text="Apply filters", command=self.refresh).pack(side="left", padx=5)
        self.body = ttk.Panedwindow(self.root, orient="horizontal"); self.body.pack(fill="both", expand=True, padx=10, pady=5)
        left = ttk.Frame(self.body, width=250); center = ttk.Frame(self.body); right = ttk.Frame(self.body, width=280)
        self.body.add(left, weight=1); self.body.add(center, weight=4); self.body.add(right, weight=1)
        ttk.Label(left, text="SIGNAL FAMILIES").pack(anchor="w")
        self.families = tk.Listbox(left, bg="#0a1b25", fg="#c9f5ff", selectbackground="#14515b", relief="flat")
        self.families.pack(fill="both", expand=True, pady=5); self.families.bind("<<ListboxSelect>>", self.family_selected)
        ttk.Button(left, text="Show all families", command=self.clear_family).pack(anchor="e", pady=(0, 4))
        self.waterfall_canvas = tk.Canvas(center, bg="#041017", highlightthickness=0, height=330); self.waterfall_canvas.pack(fill="both", expand=True)
        self.timeline_canvas = tk.Canvas(center, bg="#08151d", highlightthickness=0, height=140); self.timeline_canvas.pack(fill="both", expand=True, pady=5)
        self.spectrum_canvas = tk.Canvas(center, bg="#07141c", highlightthickness=0, height=150); self.spectrum_canvas.pack(fill="both", expand=True)
        scrub = ttk.Frame(center); scrub.pack(fill="x")
        ttk.Button(scrub, text="Play", command=self.toggle_play).pack(side="left")
        self.scrub = tk.IntVar(value=0)
        self.scrub_scale = tk.Scale(scrub, variable=self.scrub, from_=0, to=0, orient="horizontal",
                                    showvalue=False, command=self.scrub_changed, bg="#071018", fg="#b6d7de",
                                    highlightthickness=0)
        self.scrub_scale.pack(side="left", fill="x", expand=True)
        ttk.Label(right, text="SIGNAL FAMILY DETAIL").pack(anchor="w")
        self.family_details = tk.Text(right, bg="#081923", fg="#b6d7de", insertbackground="#d9f8ff",
                                      relief="flat", wrap="word", height=9)
        self.family_details.pack(fill="x", pady=(2, 6)); self.family_details.configure(state="disabled")
        ttk.Label(right, text="SELECTED OBSERVATION / SIMILARITY").pack(anchor="w")
        self.details = tk.Text(right, bg="#0a1b25", fg="#d9f8ff", insertbackground="#d9f8ff", relief="flat", wrap="word")
        self.details.pack(fill="both", expand=True, pady=5); self.details.configure(state="disabled")
        ttk.Label(right, text="NOTE / LOCATION").pack(anchor="w")
        self.note_entry = tk.Entry(right, bg="#0a1b25", fg="#d9f8ff", insertbackground="#d9f8ff", relief="flat")
        self.note_entry.pack(fill="x", pady=2)
        self.location_entry = tk.Entry(right, bg="#0a1b25", fg="#d9f8ff", insertbackground="#d9f8ff", relief="flat")
        self.location_entry.pack(fill="x", pady=2)
        ttk.Button(right, text="Save note", command=self.save_note).pack(anchor="e")
        ttk.Button(right, text="Export Follow profile", command=self.export_follow).pack(anchor="e", pady=4)
        self.waterfall_canvas.bind("<Button-1>", self.canvas_event)
        self.timeline_canvas.bind("<Button-1>", self.timeline_event)
        self.playing = False
        self.play_after = None

    def _set_family_details(self, family_id):
        detail = self.project.family_detail(family_id)
        if not detail.get("observation_count"):
            text = "No family selected"
        else:
            frequencies = "—"
            if detail["frequency_min_hz"]:
                frequencies = f"{detail['frequency_min_hz'] / 1e6:.3f}"
                if detail["frequency_max_hz"] != detail["frequency_min_hz"]:
                    frequencies += f"–{detail['frequency_max_hz'] / 1e6:.3f}"
                frequencies += " MHz"
            hours = ", ".join(f"{hour:02d}:00" for hour in detail["time_of_day_hours"]) or "—"
            text = (f"{detail['family_id']}\n"
                    f"{detail['observation_count']} observations · {frequencies}\n"
                    f"Seen {detail['first_seen']}\nLast {detail['last_seen']}\n"
                    f"Modulation: {', '.join(detail['modulations']) or 'unknown'}\n"
                    f"Waveform variants: {len(detail['waveform_variants'])}\n"
                    f"RSSI: {detail['rssi_min_dbm']:.1f}…{detail['rssi_max_dbm']:.1f} dBm\n"
                    f"Active hours: {hours}\n"
                    f"Hypothesis: {detail['source_hypothesis']} · confidence {detail['similarity_confidence']:.0%}\n"
                    f"Raw captures: {detail['raw_capture_count']} · imported: {detail['imported_count']} · pending: {detail['pending_count']}\n"
                    f"Follow: {'selected' if detail['follow_selected'] else 'not selected'}")
        self.family_details.configure(state="normal")
        self.family_details.delete("1.0", "end")
        self.family_details.insert("end", text)
        self.family_details.configure(state="disabled")

    def _spec(self):
        frequency = self.frequency.get().strip().replace(",", ".")
        try:
            freq = float(frequency) * 1_000_000 if frequency else None
            freq = int(freq) if freq is not None else None
        except ValueError:
            freq = None
        try:
            min_rssi = float(self.rssi.get().strip()) if self.rssi.get().strip() else None
        except ValueError:
            min_rssi = None
        try:
            max_rssi = float(self.max_rssi.get().strip()) if self.max_rssi.get().strip() else None
        except ValueError:
            max_rssi = None
        try:
            start = parse_time(self.start_time.get().strip()) if self.start_time.get().strip() else None
            end = parse_time(self.end_time.get().strip()) if self.end_time.get().strip() else None
        except ValueError:
            start = end = None
        return EventFilter(source_type=self.source.get().strip(), family_id=self._selected_family,
                           text=self.search.get().strip(), start=start, end=end,
                           min_rssi=min_rssi, max_rssi=max_rssi, min_frequency_hz=freq,
                           max_frequency_hz=freq)

    def open_store(self):
        path = self.filedialog.askdirectory(title="Select Flipper event store")
        if path:
            self.project.add_root(path); self.refresh()

    def sync_ble(self):
        """Start a live Flipper pull without freezing the investigation UI."""
        import threading
        if self._sync_thread is not None and self._sync_thread.is_alive():
            return
        target = next(iter(self.project.sources.values()), None)
        if target is None:
            path = self.filedialog.askdirectory(title="Select local RF project directory")
            if not path:
                return
            target = EventStore(path)
            self._sync_target = target
        else:
            self._sync_target = target
        self.status.configure(text="BLE: discovering RF Hunter…")
        self._sync_thread = threading.Thread(target=self._sync_ble_worker, daemon=True)
        self._sync_thread.start()

    def _sync_ble_worker(self):
        async def run():
            from .rf_ble import BleakRfAdapter
            adapter = BleakRfAdapter(timeout=10)
            try:
                await adapter.connect()
                hello = await adapter.hello()
                self.root.after(0, self._sync_pending, hello.get("pending"))
                progress = lambda stats: self.root.after(
                    0, self._sync_progress, stats)
                return await adapter.sync_to(self._sync_target, progress=progress)
            finally:
                await adapter.close()
        try:
            result = asyncio.run(run())
        except Exception as exc:  # report on Tk's thread, keep the app usable
            self.root.after(0, self._sync_finished, None, exc)
        else:
            self.root.after(0, self._sync_finished, result, None)

    def _sync_pending(self, pending):
        self.status.configure(text=f"BLE connected · {pending if pending is not None else '?'} pending on Flipper")

    def _sync_progress(self, stats):
        self.status.configure(
            text=(f"BLE: {stats.get('imported', 0)} imported · "
                  f"{stats.get('skipped', 0)} already present · "
                  f"{stats.get('seen', 0)} seen"))

    def _sync_finished(self, result, error):
        if error is not None:
            self.status.configure(text=f"BLE error: {error}")
            return
        if self._sync_target is not None:
            self.project.add_store(self._sync_target)
        self.refresh()
        self.status.configure(text=(f"BLE complete · {result.get('imported', 0)} imported · "
                                    f"{result.get('skipped', 0)} already present"))

    def family_selected(self, _event=None):
        selected = self.families.curselection()
        self._selected_family = self.families.get(selected[0]).split("  ", 1)[0] if selected else ""
        self._set_family_details(self._selected_family)
        self.refresh()

    def clear_family(self):
        self._selected_family = ""
        self.families.selection_clear(0, "end")
        self.refresh()

    def refresh(self):
        events = self.project.filtered(self._spec())
        # Keep the navigator complete while the center view is filtered to a
        # selected family; otherwise selecting one row makes all other
        # families disappear and forces the user to reload the store.
        all_spec = self._spec()
        all_spec.family_id = ""
        summaries = self.project.family_summary(self.project.filtered(all_spec))
        self.families.delete(0, "end")
        for summary in summaries:
            self.families.insert("end", f"{summary['family_id']}  {summary['observation_count']} obs")
        self.status.configure(text=f"{len(events)} observations · {len(self.project.sources)} Flipper(s)")
        self._set_family_details(self._selected_family)
        self.scrub_scale.configure(to=max(0, len(events) - 1))
        self.scrub.set(min(self.scrub.get(), max(0, len(events) - 1)))
        self.draw(events)

    def draw(self, events):
        self.draw_waterfall(events); self.draw_timeline(events); self.draw_spectrum(events)
        if events:
            self.show_event(events[min(self.scrub.get(), len(events) - 1)])

    def draw_waterfall(self, events):
        canvas = self.waterfall_canvas; canvas.delete("all")
        width = max(1, canvas.winfo_width()); height = max(1, canvas.winfo_height())
        rows = self.project.waterfall(events)[-max(1, height // 12):]
        frequencies = [row["frequency_hz"] for row in rows if row["frequency_hz"]]
        if not rows or not frequencies:
            canvas.create_text(width // 2, height // 2, text="Sampled RF waterfall — no events", fill="#6297a3")
            return
        low, high = min(frequencies), max(frequencies); span = max(1, high - low)
        row_h = max(4, height / max(1, len(rows)))
        for index, row in enumerate(rows):
            x = 12 + (row["frequency_hz"] - low) * (width - 24) / span
            intensity = max(0, min(255, int((row["rssi_dbm"] + 110) * 4)))
            color = f"#{20:02x}{min(255, 60 + intensity):02x}{min(255, 100 + intensity):02x}"
            y = height - (index + 1) * row_h
            canvas.create_rectangle(max(3, x - 3), y, min(width - 3, x + 3), y + row_h - 1, fill=color, outline="")
            if row["family_id"] == self._selected_family:
                canvas.create_oval(x - 6, y, x + 6, y + row_h, outline="#ffb347", width=2)
        canvas.create_text(10, 8, anchor="w", text=f"{low/1e6:.3f}–{high/1e6:.3f} MHz · sampled RSSI", fill="#8dd9e6")

    def draw_timeline(self, events):
        canvas = self.timeline_canvas; canvas.delete("all"); width = max(1, canvas.winfo_width()); height = max(1, canvas.winfo_height())
        rows = self.project.timeline(events)
        if not rows:
            return
        times = [row["when"].timestamp() for row in rows]; lo, hi = min(times), max(times); span = max(1, hi - lo)
        y = height // 2; canvas.create_line(12, y, width - 12, y, fill="#2d6574")
        for row, timestamp in zip(rows, times):
            x = 12 + (timestamp - lo) * (width - 24) / span
            color = "#ffb347" if row["family_id"] == self._selected_family else "#55d6be"
            canvas.create_oval(x - 4, y - 4, x + 4, y + 4, fill=color, outline="")
        canvas.create_text(12, 8, anchor="w", text=f"{datetime.fromtimestamp(lo, timezone.utc).isoformat(timespec='seconds')}Z", fill="#79aeb8")

    def timeline_event(self, event):
        events = self.project.filtered(self._spec())
        if not events:
            return
        times = [parse_time(item.captured_at_utc).timestamp() for item in events]
        lo, hi = min(times), max(times)
        ratio = max(0.0, min(1.0, (event.x - 12) / max(1, self.timeline_canvas.winfo_width() - 24)))
        target = lo + ratio * max(1, hi - lo)
        index = min(range(len(times)), key=lambda i: abs(times[i] - target))
        self.scrub.set(index); self.show_event(events[index])

    def scrub_changed(self, _value=None):
        events = self.project.filtered(self._spec())
        if events:
            self.show_event(events[min(self.scrub.get(), len(events) - 1)])
            self.draw(events)

    def toggle_play(self):
        self.playing = not self.playing
        if self.playing:
            self._play_step()

    def _play_step(self):
        if not self.playing:
            return
        events = self.project.filtered(self._spec())
        if not events:
            self.playing = False
            return
        self.scrub.set((self.scrub.get() + 1) % len(events))
        self.draw(events)
        self.play_after = self.root.after(350, self._play_step)

    def draw_spectrum(self, events):
        canvas = self.spectrum_canvas; canvas.delete("all"); width = max(1, canvas.winfo_width()); height = max(1, canvas.winfo_height())
        rows = self.project.spectrum(events)
        if not rows:
            return
        points = []
        for index, row in enumerate(rows):
            if row["rssi_dbm"] is None:
                continue
            x = 10 + index * (width - 20) / max(1, len(rows) - 1)
            y = height - 10 - max(0, min(1, (row["rssi_dbm"] + 110) / 80)) * (height - 25)
            points.extend((x, y))
        if len(points) >= 4:
            canvas.create_line(*points, fill="#ff72c6", width=2, smooth=True)
        canvas.create_text(10, 8, anchor="w", text="RSSI spectrum", fill="#d49bd0")

    def canvas_event(self, event):
        rows = self.project.filtered(self._spec())
        if rows:
            self.show_event(rows[min(len(rows) - 1, max(0, int(event.y / max(1, self.waterfall_canvas.winfo_height()) * len(rows))))])

    def show_event(self, event):
        self._selected_event = event.event_id
        self._set_family_details(self.project.family_key(event))
        nfc_details = ""
        if event.source_type == "nfc":
            nfc_details = (f"NFC {event.nfc_technology or 'unknown'} / "
                           f"{event.nfc_protocol or 'unknown'}\n"
                           f"Field interval {event.nfc_field_duration_ms} ms · "
                           f"observations {event.nfc_field_count}\n")
        data = (f"Event {event.event_id}\nDevice {event.device_uuid}\nSession {event.session_id}\n"
                f"{event.captured_at_utc}\n{event.frequency_hz / 1e6:.3f} MHz · {event.modulation}\n"
                f"{nfc_details}"
                f"RSSI {event.rssi_avg_dbm:.1f} dBm · duration {event.duration_us} us\n"
                f"Family {self.project.family_key(event)}\nPulse timings {len(event.pulse_timings_us)} samples\n"
                f"Raw capture {len(self.project.capture_bytes(event))} bytes\n"
                f"Classification {event.classification} "
                f"({event.classification_confidence:.0%})")
        similar = self.project.similar_events(event, limit=5)
        if similar:
            data += "\n\nSIMILAR OBSERVATIONS\n"
            for row in similar:
                comparison = row["comparison"]
                reasons = "; ".join(comparison.get("reasons", ())) or "no stable feature match"
                data += (f"{comparison.get('percent', 0)}% · {row['event'].event_id[:16]} · "
                         f"{comparison.get('relationship_text', 'unknown')}\n"
                         f"  {reasons}\n")
        else:
            data += "\n\nSIMILAR OBSERVATIONS\nNo other observations"
        self.details.configure(state="normal"); self.details.delete("1.0", "end"); self.details.insert("end", data); self.details.configure(state="disabled")
        note = self.project.note(event.event_id)
        self.note_entry.delete(0, "end"); self.note_entry.insert(0, note.get("text", ""))
        self.location_entry.delete(0, "end"); self.location_entry.insert(0, note.get("location", ""))

    def save_note(self):
        if self._selected_event:
            self.project.add_note(self._selected_event, self.note_entry.get(), self.location_entry.get())

    def save_project(self):
        path = self.filedialog.asksaveasfilename(defaultextension=".rfproject.json")
        if path:
            self.project.save_project(path)

    def load_project(self):
        path = self.filedialog.askopenfilename(filetypes=[("RF projects", "*.rfproject.json"), ("JSON", "*.json")])
        if path:
            self.project.load_project(path)
            self.refresh()

    def export_follow(self):
        if not self._selected_family:
            return
        path = self.filedialog.asksaveasfilename(defaultextension=".follow.json")
        if path:
            self.project.export_follow(self._selected_family, path)

    def export(self, kind):
        path = self.filedialog.asksaveasfilename(defaultextension="." + kind)
        if not path:
            return
        events = self.project.filtered(self._spec())
        (self.project.export_json if kind == "json" else self.project.export_csv)(path, events)

    def run(self):
        self.root.mainloop()


def main(argv=None):
    parser = argparse.ArgumentParser(description="RF Signal Hunter analyzer")
    parser.add_argument("roots", nargs="*", help="event store directories")
    parser.add_argument("--export-json", metavar="PATH")
    parser.add_argument("--export-csv", metavar="PATH")
    args = parser.parse_args(argv)
    project = AnalyzerProject()
    for root in args.roots:
        project.add_root(root)
    if args.export_json:
        project.export_json(args.export_json)
    if args.export_csv:
        project.export_csv(args.export_csv)
    if args.export_json or args.export_csv:
        return 0
    try:
        RfHunterApp(project).run()
    except Exception as exc:
        if args.roots:
            print(f"{len(project.events)} observations from {len(project.sources)} Flipper(s)")
            return 0
        raise exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
