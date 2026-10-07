"""Desktop RF Signal Hunter analyzer (investigation window of the DedSec Uplink companion).

:class:`AnalyzerProject` is headless: it merges any number of event stores
(opened read-only), deduplicates them by event ID and keeps desktop family
grouping in memory.  :class:`AnalyzerWindow` is a ``tk.Toplevel`` that the
companion opens on its single Tk UI thread; it reloads the companion's store
when the folder changes, shows :meth:`RfSync.status` and offers "Sync now".
It never opens a BLE connection - imports are done by
:class:`uplink.rf_sync.RfSync` over the companion's link.

The views render sampled RF observations (discrete bursts, RSSI values and
pulse timing) as a waterfall, spectrum, timeline and family comparison; they
are not continuous IQ.  ``main(argv)`` keeps the CLI: exports and a standalone
viewer with its own Tk root.
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import dataclasses
import io
import json
import logging
import logging.handlers
import os
import re
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Optional, Sequence

from .rf_fingerprint import FeatureCache, StructuralGrouper, compare_events
from .rf_hunter import (EventStore, RfEvent, app_data_dir, default_store_root, folder_signature,
                        resolve_store_root)
from . import dedsec_ui as ui

log = logging.getLogger("uplink.rf_analyzer")

POLL_MS = 3000            # folder change check
STATUS_MS = 1000          # RfSync status refresh
PLAY_MS = 350
LOG_NAME = "rf_hunter.log"
FREQUENCY_TOLERANCE_MHZ = 0.2
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
STATE_TEXT = {0: "RF off", 1: "Sub-GHz RX", 2: "NFC detect"}

BG = "#071018"
PANEL = "#0a1b25"
TEXT = "#d9f8ff"
MUTED = "#7fa9b3"
ACCENT = "#27e0e8"
WARN = "#ffb347"
ERROR = "#ff6b6b"

__all__ = ["AnalyzerProject", "AnalyzerWindow", "EventFilter", "default_store_root", "main",
           "open_analyzer", "parse_frequency_filter", "parse_time", "parse_time_filter",
           "waterfall_index_at"]


# =========================================================================== parsing
def parse_time(value: str) -> datetime:
    """Lenient parser for stored event timestamps (invalid -> 1970-01-01 UTC)."""
    try:
        parsed = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return EPOCH
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}")
_NUMBER = r"[0-9]+(?:\.[0-9]+)?"


def parse_time_filter(text: str, end: bool = False) -> Optional[datetime]:
    """Parse a From/To filter; ``''`` means no filter.

    Accepts ``YYYY-MM-DD``, ``YYYY-MM-DD HH:MM[:SS]`` (or with ``T``) and an
    optional ``Z``/``+HH:MM`` offset; times without an offset are UTC.  A bare
    date used as the end of a range covers that whole day.  Anything else
    raises ``ValueError`` - an unreadable filter must never silently become 1970.
    """
    text = (text or "").strip()
    if not text:
        return None
    candidate = re.sub(r"[zZ]$", "+00:00", text)
    if "T" not in candidate and " " in candidate:
        candidate = candidate.replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        raise ValueError(f"cannot read time '{text}' (use YYYY-MM-DD or YYYY-MM-DD HH:MM, UTC)") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    if end and _DATE_ONLY.fullmatch(text):
        parsed += timedelta(days=1) - timedelta(microseconds=1)
    return parsed


def parse_frequency_filter(text: str, tolerance_mhz: float = FREQUENCY_TOLERANCE_MHZ) -> Optional[tuple]:
    """Parse the MHz filter into ``(min_hz, max_hz)``; ``''`` means no filter.

    ``433.92`` matches 433.92 +/- 0.2 MHz, ``433.92+-0.05`` (or ``±``) sets the
    tolerance, ``433-434`` / ``433.5..434`` / ``433 to 434`` is a range.
    Raises ``ValueError`` for anything else.
    """
    raw = (text or "").strip()
    value = raw.lower().replace("mhz", "").replace(",", ".").strip()
    if not value:
        return None
    match = re.fullmatch(rf"({_NUMBER})\s*(?:-|–|—|\.\.|to)\s*({_NUMBER})", value)
    if match:
        low, high = sorted((float(match.group(1)), float(match.group(2))))
    else:
        match = re.fullmatch(rf"({_NUMBER})\s*(?:±|\+/-|\+-)\s*({_NUMBER})", value)
        if match:
            center, tolerance = float(match.group(1)), float(match.group(2))
        elif re.fullmatch(_NUMBER, value):
            center, tolerance = float(value), float(tolerance_mhz)
        else:
            raise ValueError(f"cannot read frequency '{raw}' (use 433.92, 433.92+-0.1 or 433-434)")
        low, high = center - tolerance, center + tolerance
    if not 0 < high <= 100_000:
        raise ValueError(f"frequency '{raw}' is out of range")
    return int(round(max(0.0, low) * 1_000_000)), int(round(high * 1_000_000))


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


# =========================================================================== waterfall geometry
def waterfall_capacity(height: int) -> int:
    """Rows that fit the canvas: the newest ``height // 12`` events are drawn."""
    return max(1, int(height) // 12)


def waterfall_row_height(height: int, count: int) -> float:
    return max(4.0, height / max(1, count))


def waterfall_index_at(y: float, height: int, count: int) -> Optional[int]:
    """Index into the drawn rows (oldest first) for a click at ``y``.

    Row ``i`` is drawn from ``height - (i + 1) * row_h`` to ``height - i * row_h``,
    so the newest row is at the top of the canvas.
    """
    if count <= 0 or height <= 0:
        return None
    index = int((height - y) // waterfall_row_height(height, count))
    return min(count - 1, max(0, index))


# =========================================================================== project
class AnalyzerProject:
    """Merged read-only view of one or more event stores, deduplicated by event ID.

    Desktop grouping results live in :attr:`grouper` (``family_of`` and
    ``fingerprint_of`` maps); the events themselves are never modified, so the
    Flipper's stored evidence cannot be overwritten.  Families are rebuilt
    only when the set of events changes (see :meth:`ensure_families`).
    """

    def __init__(self):
        self.sources: dict[str, EventStore] = {}
        self.events: dict[str, RfEvent] = {}
        self.cache = FeatureCache()
        self.grouper = StructuralGrouper(cache=self.cache)
        self.notes: dict[str, dict] = {}
        self.settings: dict = {"selected_family": "", "timezone": "UTC"}
        self.rebuild_count = 0
        self._built_events: tuple = ()
        self._built_ids: frozenset = frozenset()
        self._capture_known: dict = {}

    # ------------------------------------------------------------------ sources
    def add_root(self, root, read_only: bool = True) -> int:
        """Open a folder (store root, journal sub-folder or SD card) read-only."""
        store = EventStore(resolve_store_root(root), read_only=read_only)
        return self.add_store(store)

    def add_store(self, store) -> int:
        self.sources[store.root] = store
        self._merge_store(store)
        self._capture_known.clear()
        return len(store.events)

    def reload_root(self, root) -> int:
        """Re-read one folder from disk (after another process changed it)."""
        root = os.fspath(root)
        previous = self.sources.get(root)
        store = EventStore(root, read_only=getattr(previous, "read_only", True))
        self.sources[root] = store
        self._remerge()
        return len(store.events)

    def _remerge(self):
        self.events = {}
        stores = list(self.sources.values())
        for position, store in enumerate(stores):
            self._merge_store(store, stores[:position])
        self._capture_known.clear()

    def _merge_store(self, store, earlier=None):
        """Merge a journal without letting a transport duplicate replace data.

        The same event ID may be present in two Flipper exports (or in a
        copied journal and the companion store).  Keep one observation,
        preferring the representation whose capture blob actually exists.
        """
        if earlier is None:
            earlier = [candidate for root, candidate in self.sources.items() if root != store.root]
        for event_id, event in store.events.items():
            current = self.events.get(event_id)
            if current is None:
                self.events[event_id] = event
                continue
            if self._capture_in(event, (store,)) and not self._capture_in(current, earlier):
                self.events[event_id] = event
            elif current.upload_state != "uploaded" and event.upload_state == "uploaded":
                current.upload_state = "uploaded"  # in memory only: stores are read-only

    @staticmethod
    def _capture_in(event: RfEvent, stores) -> bool:
        for store in stores:
            if event.event_id not in getattr(store, "events", {}):
                continue
            if hasattr(store, "has_capture"):
                if store.has_capture(event.event_id):
                    return True
            elif event.capture_blob:
                return os.path.isfile(os.path.join(store.root, event.capture_blob))
        return False

    def capture_bytes(self, event: RfEvent) -> bytes:
        """Read the raw capture (the exact Flipper record) from whichever source has it."""
        for store in self.sources.values():
            if event.event_id not in getattr(store, "events", {}) or not hasattr(store, "read_capture"):
                continue
            data = store.read_capture(event.event_id)
            if data:
                return data
        return b""

    def has_capture(self, event: RfEvent) -> bool:
        known = self._capture_known.get(event.event_id)
        if known is None:
            known = self._capture_known[event.event_id] = self._capture_in(event, self.sources.values())
        return known

    def load_problems(self) -> tuple:
        """``(skipped record count, first messages)`` over all sources."""
        count, messages = 0, []
        for store in self.sources.values():
            count += getattr(store, "skipped_records", 0)
            messages.extend(f"{store.root}: {text}" for text in getattr(store, "load_errors", ()))
        return count, messages[:20]

    def clear(self):
        self.sources.clear()
        self.events.clear()
        self.cache.clear()
        self.grouper = StructuralGrouper(cache=self.cache)
        self._built_events, self._built_ids = (), frozenset()
        self._capture_known.clear()
        self.notes.clear()
        self.settings = {"selected_family": "", "timezone": "UTC"}

    # ------------------------------------------------------------------ families
    def ensure_families(self) -> dict:
        """Rebuild families only if the set of event objects changed."""
        if (len(self.events) != len(self._built_ids)
                or frozenset(map(id, self.events.values())) != self._built_ids):
            self.rebuild_families()
        return self.grouper.families

    def rebuild_families(self) -> dict:
        """Recompute authoritative structural families deterministically."""
        events = tuple(self.events.values())
        families = self.grouper.rebuild(events)
        self.cache.prune(events)
        # Holding the built events keeps their object ids unique, which makes
        # the cheap identity comparison in ensure_families() reliable.
        self._built_events = events
        self._built_ids = frozenset(map(id, events))
        self.rebuild_count += 1
        return families

    def family_key(self, event: RfEvent) -> str:
        return self.grouper.family_of.get(StructuralGrouper.key(event)) or "unassigned"

    def fingerprint_key(self, event: RfEvent) -> str:
        return (self.grouper.fingerprint_of.get(StructuralGrouper.key(event))
                or event.fingerprint_id or "unknown")

    # ------------------------------------------------------------------ notes / project files
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
        if not isinstance(payload, dict):
            raise ValueError("not an RF analyzer project file")
        self.clear()
        for root in payload.get("roots", []):
            self.add_root(root)
        self.settings.update(payload.get("settings") or {})
        self.notes.update(payload.get("notes") or {})

    # ------------------------------------------------------------------ views
    def follow_profile(self, family_id):
        self.ensure_families()
        rows = [event for event in self.events.values() if self.family_key(event) == family_id]
        if not rows:
            return None
        latest = max(rows, key=lambda event: parse_time(event.captured_at_utc))
        return {"version": 1, "profile_id": family_id,
                "frequency_hz": latest.frequency_hz, "modulation": latest.modulation,
                "fingerprint_id": self.fingerprint_key(latest),
                "flipper_fingerprint_id": latest.fingerprint_id,
                "pulse_timings_us": list(latest.pulse_timings_us),
                "tolerance_us": 500, "passive_only": True}

    def export_follow(self, family_id, path):
        profile = self.follow_profile(family_id)
        if profile is None:
            raise KeyError(family_id)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(profile, fh, ensure_ascii=False, indent=2)

    def filtered(self, spec: Optional[EventFilter] = None) -> list:
        spec = spec or EventFilter()
        self.ensure_families()
        text = spec.text.casefold()
        source = spec.source_type.strip().casefold()
        rows = []
        for event in self.events.values():
            when = parse_time(event.captured_at_utc)
            family = self.family_key(event)
            if source and (event.source_type or "").casefold() != source:
                continue
            if spec.family_id and family != spec.family_id:
                continue
            if text:
                haystack = " ".join((event.event_id, event.device_uuid, event.modulation,
                                     event.classification, family, event.fingerprint_id or "")).casefold()
                if text not in haystack:
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
            rows.append((when, event.event_id, event))
        rows.sort(key=lambda row: (row[0], row[1]))
        return [row[2] for row in rows]

    def family_summary(self, events: Optional[Iterable[RfEvent]] = None) -> list:
        self.ensure_families()
        groups = {}
        for event in (events if events is not None else self.events.values()):
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
            family = self.grouper.families.get(group["family_id"], {})
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
                "confidence": family.get("confidence", 0.0),
                "provisional": family.get("provisional", True),
            })
        return sorted(result, key=lambda row: (-row["observation_count"], row["family_id"]))

    def family_detail(self, family_id: str, events: Optional[Iterable[RfEvent]] = None) -> dict:
        """Return the evidence used to explain one signal family.

        The detail payload intentionally keeps observations separate while
        exposing the aggregate values the investigation UI needs: waveform
        variants, RSSI/time-of-day distributions, source hypothesis,
        similarity confidence, Follow state and raw/import status.
        """
        self.ensure_families()
        family_id = str(family_id or "")
        rows = [event for event in (events if events is not None else self.events.values())
                if self.family_key(event) == family_id]
        if not rows:
            return {"family_id": family_id, "observation_count": 0, "event_ids": []}
        frequencies = [event.frequency_hz for event in rows if event.frequency_hz]
        rssis = [float(event.rssi_avg_dbm) for event in rows]
        variants = sorted({self.fingerprint_key(event) for event in rows})
        classifications = {}
        for event in rows:
            label = event.classification or "unknown"
            classifications[label] = classifications.get(label, 0) + 1
        source_types = sorted({event.source_type or "unknown" for event in rows})
        hours = [parse_time(event.captured_at_utc).hour for event in rows]
        raw_count = sum(self.has_capture(event) for event in rows)
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
                       candidates: Optional[Iterable[RfEvent]] = None) -> list:
        """Return nearest observations and the plain-language match reasons."""
        pool = candidates if candidates is not None else self.events.values()
        rows = []
        for event in pool:
            if event.event_id == selected.event_id:
                continue
            comparison = self.similarity(selected, event, cache=self.cache)
            rows.append({"event": event, "comparison": comparison})
        rows.sort(key=lambda row: (-float(row["comparison"].get("score", 0.0)),
                                   parse_time(row["event"].captured_at_utc), row["event"].event_id))
        return rows[:max(0, int(limit))]

    def timeline(self, events: Optional[Iterable[RfEvent]] = None) -> list:
        self.ensure_families()
        rows = [{"event_id": event.event_id, "when": parse_time(event.captured_at_utc),
                 "family_id": self.family_key(event), "frequency_hz": event.frequency_hz,
                 "rssi_dbm": event.rssi_avg_dbm, "source_type": event.source_type}
                for event in (events if events is not None else self.events.values())]
        return sorted(rows, key=lambda row: (row["when"], row["event_id"]))

    def spectrum(self, events: Optional[Iterable[RfEvent]] = None, bins=96) -> list:
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

    def waterfall(self, events: Optional[Iterable[RfEvent]] = None) -> list:
        """Return one sampled intensity row per event (oldest first) for a waterfall renderer."""
        self.ensure_families()
        rows = list(events if events is not None else self.events.values())
        return [{"event_id": event.event_id, "when": event.captured_at_utc,
                 "frequency_hz": event.frequency_hz, "rssi_dbm": event.rssi_avg_dbm,
                 "family_id": self.family_key(event),
                 "pulse_timings_us": list(event.pulse_timings_us)} for event in
                sorted(rows, key=lambda item: parse_time(item.captured_at_utc))]

    @staticmethod
    def similarity(left: RfEvent, right: RfEvent, cache: Optional[FeatureCache] = None) -> dict:
        result = dict(compare_events(left, right, cache=cache))
        reasons = list(result.get("reasons", ()))
        if left.event_id != right.event_id and "distinct observation IDs preserved" not in reasons:
            reasons.append("distinct observation IDs preserved")
        result["reasons"] = reasons
        return result

    def _export_row(self, event: RfEvent) -> dict:
        row = event.to_dict()
        row["desktop_family_id"] = self.family_key(event)
        row["desktop_fingerprint_id"] = self.fingerprint_key(event)
        return row

    def export_json(self, path, events: Optional[Iterable[RfEvent]] = None):
        self.ensure_families()
        rows = [self._export_row(event) for event in (events if events is not None else self.events.values())]
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"schema_version": 1, "exported_at_utc": datetime.now(timezone.utc).isoformat(),
                       "events": rows}, fh, ensure_ascii=False, indent=2)

    def export_csv(self, path, events: Optional[Iterable[RfEvent]] = None):
        self.ensure_families()
        rows = list(events if events is not None else self.events.values())
        fields = ["event_id", "device_uuid", "session_id", "sequence_number", "captured_at_utc",
                  "source_type", "frequency_hz", "modulation", "nfc_technology", "nfc_protocol",
                  "nfc_field_duration_ms", "rssi_avg_dbm", "duration_us",
                  "family_id", "fingerprint_id", "desktop_family_id", "desktop_fingerprint_id",
                  "classification", "upload_state"]
        with open(path, "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for event in rows:
                writer.writerow(self._export_row(event))


# =========================================================================== window
def _format_local_time(epoch) -> str:
    try:
        return datetime.fromtimestamp(float(epoch)).strftime("%H:%M:%S")
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


def _format_kb(kb) -> str:
    try:
        kb = float(kb)
    except (TypeError, ValueError):
        return "?"
    if kb >= 1024 * 1024:
        return f"{kb / 1024 / 1024:.1f} GB"
    if kb >= 1024:
        return f"{kb / 1024:.0f} MB"
    return f"{kb:.0f} KB"


def sync_status_text(status: Optional[dict]) -> tuple:
    """``(summary, error)`` lines for an :meth:`RfSync.status` snapshot."""
    if status is None:
        return "Viewer only: imports run in the DedSec Uplink companion.", ""
    parts = []
    if not status.get("link_up"):
        parts.append("Flipper not connected")
    else:
        pending = status.get("pending")
        parts.append(f"Flipper: {pending if pending is not None else '?'} pending")
        if status.get("free_kb") is not None:
            parts.append(f"{_format_kb(status['free_kb'])} free")
        state = STATE_TEXT.get(status.get("state"))
        if state:
            parts.append(state)
        if status.get("errors"):
            parts.append(f"{status['errors']} journal errors")
    if status.get("syncing"):
        progress = status.get("current_progress") or ""
        parts.append(f"syncing {progress} B" if progress else "syncing…")
    parts.append(f"imported {status.get('imported', 0)} · failed {status.get('failed', 0)}")
    if status.get("last_sync"):
        parts.append(f"last sync {_format_local_time(status['last_sync'])}")
    error = status.get("last_error") or ""
    return " · ".join(parts), (f"Last error: {error}" if error else "")


class AnalyzerWindow:
    """RF investigation window: a ``tk.Toplevel`` on an existing Tk root.

    ``parent`` is the companion's Tk root (or any widget of it); all calls must
    come from that root's thread.  ``store_root`` is the companion's event
    store (polled for changes every few seconds, opened read-only), ``sync``
    an optional :class:`uplink.rf_sync.RfSync` whose status is shown and whose
    ``sync_now()`` the "Sync now" button calls.  ``extra_roots`` are further
    folders opened read-only (for example a copied SD journal) and
    ``on_close`` is called after the window was destroyed.
    """

    def __init__(self, parent, store_root, sync=None, *, extra_roots: Sequence = (),
                 on_close: Optional[Callable[[], None]] = None,
                 project: Optional[AnalyzerProject] = None):
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
        self.tk, self.ttk = tk, ttk
        self.filedialog, self.messagebox = filedialog, messagebox
        self.sync = sync
        self.on_close = on_close
        self.project = project or AnalyzerProject()
        self.store_root = resolve_store_root(store_root) if store_root else ""
        self._alive = True
        self._after_ids: dict = {}
        self._folder_signatures: dict = {}
        self._selected_family = ""
        self._selected_event = ""
        self._events: list = []
        self._family_rows: list = []
        self._waterfall_rows: list = []
        self._waterfall_height = 1
        self._last_imported = None
        self.playing = False
        self.window = tk.Toplevel(parent)
        self.window.title("DEDSEC // RF HUNTER")
        self.window.geometry("1280x820")
        self.window.minsize(900, 600)
        self.window.configure(bg=ui.BG)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self._build()
        if self.store_root:
            self._open_root(self.store_root, report=False)
        for root in extra_roots or ():
            self._open_root(root, report=True)
        self.refresh()
        self._schedule("poll", POLL_MS, self._poll_folders)
        if self.sync is not None:
            self._tick_status()

    # ------------------------------------------------------------------ lifecycle
    @property
    def alive(self) -> bool:
        return self._alive

    def show(self):
        """Bring the existing window to the front."""
        if not self._alive:
            return
        self.window.deiconify()
        self.window.lift()
        try:
            self.window.focus_force()
        except self.tk.TclError:
            pass

    def close(self):
        """Destroy the window (idempotent) and cancel its timers."""
        if not self._alive:
            return
        self._alive = False
        self.playing = False
        for after_id in list(self._after_ids.values()):
            try:
                self.window.after_cancel(after_id)
            except Exception:
                pass
        self._after_ids.clear()
        try:
            self.window.destroy()
        except Exception:
            pass
        if self.on_close is not None:
            try:
                self.on_close()
            except Exception:
                log.exception("analyzer on_close callback failed")

    def _schedule(self, name: str, delay_ms: int, callback: Callable[[], None]):
        if not self._alive:
            return

        def run():
            self._after_ids.pop(name, None)
            if self._alive:
                callback()

        previous = self._after_ids.pop(name, None)
        if previous is not None:
            try:
                self.window.after_cancel(previous)
            except Exception:
                pass
        self._after_ids[name] = self.window.after(delay_ms, run)

    def _report_error(self, title: str, error) -> None:
        """User-triggered failures: log them and show a dialog (pythonw has no console)."""
        log.error("%s: %s", title, error, exc_info=isinstance(error, BaseException))
        if self._alive:
            try:
                self.messagebox.showerror(title, str(error), parent=self.window)
            except Exception:
                log.debug("cannot show error dialog", exc_info=True)

    # ------------------------------------------------------------------ layout
    def _build(self):
        tk, ttk = self.tk, self.ttk
        window = self.window
        ui.style_scrollbars(window)
        self._images = []
        try:
            from PIL import ImageTk

            from .icon import make_icon
            hood = ImageTk.PhotoImage(make_icon(ui.MAGENTA).resize((46, 46)), master=window)
            small = ImageTk.PhotoImage(make_icon(ui.MAGENTA).resize((32, 32)), master=window)
            self._images += [hood, small]
            window.iconphoto(False, small)
        except Exception:
            hood = None

        top = tk.Frame(window, bg=ui.BG)
        top.pack(fill="x", padx=10, pady=(8, 0))
        ui.glitch_header(top, "RF HUNTER", "DEDSEC  //  SIGNAL INVESTIGATION", image=hood).pack(
            side="left", fill="x", expand=True)
        self.status = tk.Label(top, text="No observations loaded", fg=ui.DIM, bg=ui.BG,
                               font=(ui.MONO, 9), anchor="e")
        self.status.pack(side="right", anchor="n", pady=(6, 0))

        toolbar = tk.Frame(window, bg=ui.BG)
        toolbar.pack(fill="x", padx=12, pady=(4, 2))
        self.sync_button = ui.NeonButton(toolbar, "SYNC NOW", self.sync_now, style="primary").pack(
            side="left", padx=(0, 8))
        if self.sync is None:
            self.sync_button.state(["disabled"])
        for label, command in (("OPEN FOLDER", self.open_folder),
                               ("EXPORT JSON", lambda: self.export("json")),
                               ("EXPORT CSV", lambda: self.export("csv")),
                               ("SAVE PROJECT", self.save_project),
                               ("LOAD PROJECT", self.load_project)):
            ui.NeonButton(toolbar, label, command).pack(side="left", padx=(0, 6))

        info = tk.Frame(window, bg=ui.BG)
        info.pack(fill="x", padx=12, pady=(6, 2))
        line = tk.Frame(info, bg=ui.BG)
        line.pack(fill="x")
        self.sync_dot = tk.Label(line, text="●", fg=ui.MUTED, bg=ui.BG, font=(ui.MONO, 10))
        self.sync_dot.pack(side="left")
        self.sync_label = tk.Label(line, text="", fg=ui.TEXT, bg=ui.BG, font=(ui.MONO, 9, "bold"),
                                   anchor="w")
        self.sync_label.pack(side="left", padx=(4, 0))
        self.sync_error = tk.Label(info, text="", fg=ui.RED, bg=ui.BG, font=(ui.MONO, 9), anchor="w")
        self.sync_error.pack(fill="x")
        self.store_label = tk.Label(info, text="", fg=ui.MUTED, bg=ui.BG, font=(ui.MONO, 8), anchor="w")
        self.store_label.pack(fill="x")

        filters = tk.Frame(window, bg=ui.BG)
        filters.pack(fill="x", padx=12, pady=(6, 0))

        def field(label, width, padx=(10, 0)):
            tk.Label(filters, text=label, fg=ui.DIM, bg=ui.BG, font=(ui.MONO, 8, "bold")).pack(
                side="left", padx=padx)
            variable = tk.StringVar(master=window)
            widget = ui.entry(filters, variable, width)
            widget.pack(side="left", padx=4, ipady=2)
            widget.bind("<Return>", lambda _event: self.refresh())
            return variable

        self.search = field("SEARCH", 24, padx=(0, 0))
        self.source = field("SOURCE", 8)
        self.frequency = field("MHZ", 13)
        self.rssi = field("RSSI ≥", 6)
        self.max_rssi = field("≤", 6, padx=(2, 0))
        self.start_time = field("FROM UTC", 16)
        self.end_time = field("TO", 16, padx=(2, 0))
        ui.NeonButton(filters, "APPLY", self.refresh, style="accent").pack(side="left", padx=(10, 0))
        self.filter_error = tk.Label(window, text="", fg=ui.YELLOW, bg=ui.BG, font=(ui.MONO, 9), anchor="w")
        self.filter_error.pack(fill="x", padx=12)

        body = tk.PanedWindow(window, orient="horizontal", bg=ui.LINE, sashwidth=3, borderwidth=0,
                              sashrelief="flat", opaqueresize=True)
        body.pack(fill="both", expand=True, padx=12, pady=(4, 10))
        left = tk.Frame(body, bg=ui.BG)
        center = tk.Frame(body, bg=ui.BG)
        right = tk.Frame(body, bg=ui.BG)
        body.add(left, minsize=200, width=240, stretch="never")
        body.add(center, minsize=360, stretch="always")
        body.add(right, minsize=300, width=380, stretch="never")

        ui.section(left, "SIGNAL_FAMILIES", ui.MAGENTA).pack(fill="x", padx=(0, 8))
        box = tk.Frame(left, bg=ui.PANEL, highlightthickness=1, highlightbackground=ui.LINE)
        box.pack(fill="both", expand=True, pady=6, padx=(0, 8))
        self.families = tk.Listbox(box, bg=ui.PANEL, fg=ui.TEXT, selectbackground=ui.MAGENTA,
                                   selectforeground=ui.BG, relief="flat", exportselection=False,
                                   font=(ui.MONO, 9), activestyle="none", highlightthickness=0,
                                   borderwidth=0)
        family_scroll = ttk.Scrollbar(box, orient="vertical", command=self.families.yview,
                                      style="DedSec.Vertical.TScrollbar")
        self.families.configure(yscrollcommand=family_scroll.set)
        self.families.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=4)
        family_scroll.pack(side="right", fill="y")
        self.families.bind("<<ListboxSelect>>", self.family_selected)
        ui.NeonButton(left, "SHOW ALL", self.clear_family).pack(anchor="e", padx=(0, 8), pady=(0, 2))

        scrub = tk.Frame(center, bg=ui.BG)
        scrub.pack(side="bottom", fill="x", padx=8, pady=(0, 2))
        ui.section(center, "WATERFALL").pack(fill="x", padx=8)
        self.waterfall_canvas = tk.Canvas(center, bg="#020507", highlightthickness=1,
                                          highlightbackground=ui.LINE, height=220)
        self.waterfall_canvas.pack(fill="both", expand=True, padx=8, pady=(4, 6))  # takes the spare height
        ui.section(center, "TIMELINE").pack(fill="x", padx=8)
        self.timeline_canvas = tk.Canvas(center, bg="#020507", highlightthickness=1,
                                         highlightbackground=ui.LINE, height=86)
        self.timeline_canvas.pack(fill="x", padx=8, pady=(4, 6))
        ui.section(center, "RSSI_SPECTRUM").pack(fill="x", padx=8)
        self.spectrum_canvas = tk.Canvas(center, bg="#020507", highlightthickness=1,
                                         highlightbackground=ui.LINE, height=96)
        self.spectrum_canvas.pack(fill="x", padx=8, pady=(4, 6))
        self.play_button = ui.NeonButton(scrub, "PLAY", self.toggle_play, style="accent").pack(side="left")
        self.scrub = tk.IntVar(master=window, value=0)
        self.scrub_scale = tk.Scale(scrub, variable=self.scrub, from_=0, to=0, orient="horizontal",
                                    showvalue=False, command=self.scrub_changed, bg=ui.CYAN,
                                    activebackground=ui.MAGENTA, troughcolor=ui.PANEL,
                                    highlightthickness=0, borderwidth=0, sliderrelief="flat",
                                    sliderlength=22, width=10)
        self.scrub_scale.pack(side="left", fill="x", expand=True, padx=(10, 0))

        ui.section(right, "FAMILY_DETAIL", ui.MAGENTA).pack(fill="x", padx=(8, 0))
        family_box, self.family_details = ui.text_box(right, height=11)
        family_box.pack(fill="x", padx=(8, 0), pady=(4, 8))
        ui.section(right, "OBSERVATION // SIMILARITY", ui.MAGENTA).pack(fill="x", padx=(8, 0))
        details_box, self.details = ui.text_box(right)
        details_box.pack(fill="both", expand=True, padx=(8, 0), pady=(4, 8))
        ui.section(right, "NOTE // LOCATION", ui.MAGENTA).pack(fill="x", padx=(8, 0))
        self.note_entry = ui.entry(right)
        self.note_entry.pack(fill="x", padx=(8, 0), pady=(4, 2), ipady=2)
        self.location_entry = ui.entry(right)
        self.location_entry.pack(fill="x", padx=(8, 0), pady=2, ipady=2)
        buttons = tk.Frame(right, bg=ui.BG)
        buttons.pack(fill="x", padx=(8, 0), pady=(4, 0))
        ui.NeonButton(buttons, "EXPORT FOLLOW PROFILE", self.export_follow).pack(side="right")
        ui.NeonButton(buttons, "SAVE NOTE", self.save_note, style="accent").pack(side="right", padx=6)

        self.waterfall_canvas.bind("<Button-1>", self.canvas_event)
        self.timeline_canvas.bind("<Button-1>", self.timeline_event)
        for canvas in (self.waterfall_canvas, self.timeline_canvas, self.spectrum_canvas):
            canvas.bind("<Configure>", lambda _event: self._schedule("redraw", 120, self._redraw))

    # ------------------------------------------------------------------ folders
    def _open_root(self, path, report: bool = True) -> bool:
        try:
            root = resolve_store_root(path)
            self.project.add_root(root)
            self._folder_signatures[root] = folder_signature(root)
            return True
        except Exception as exc:
            if report:
                self._report_error("Cannot open RF folder", f"{path}\n\n{exc}")
            else:
                log.warning("cannot open RF store %s: %s", path, exc)
            return False

    def open_folder(self):
        """Open another folder read-only (for example a copied SD-card journal)."""
        path = self.filedialog.askdirectory(parent=self.window, title="Open RF journal folder (read-only)")
        if path and self._open_root(path):
            self.refresh()

    def _poll_folders(self):
        """Reload any folder whose journal changed on disk (stat calls only)."""
        try:
            changed = []
            for root in list(self.project.sources):
                signature = folder_signature(root)
                if signature != self._folder_signatures.get(root):
                    self._folder_signatures[root] = signature
                    changed.append(root)
            for root in changed:
                self.project.reload_root(root)
            if changed:
                self.refresh()
        except Exception:
            log.exception("RF folder reload failed")
        finally:
            self._schedule("poll", POLL_MS, self._poll_folders)

    def _update_store_label(self):
        skipped, messages = self.project.load_problems()
        extra = len(self.project.sources) - (1 if self.store_root in self.project.sources else 0)
        text = f"Store: {self.store_root or '(none)'}"
        if extra > 0:
            text += f" · {extra} more folder(s), read-only"
        if skipped:
            text += f" · {skipped} malformed record(s) skipped"
            if messages:
                text += f" (first: {messages[0][-120:]})"
        self.store_label.configure(text=text, fg=ui.YELLOW if skipped else ui.MUTED)

    # ------------------------------------------------------------------ sync status
    def sync_now(self):
        if self.sync is None:
            return
        try:
            self.sync.sync_now()
        except Exception as exc:
            self._report_error("RF sync failed", exc)
        self._show_sync_status()

    def _show_sync_status(self):
        status = None
        if self.sync is not None:
            try:
                status = self.sync.status()
            except Exception:
                log.exception("RF sync status failed")
                status = {"last_error": "status unavailable"}
        summary, error = sync_status_text(status)
        self.sync_label.configure(text=summary)
        self.sync_error.configure(text=error)
        if status is None:
            color = ui.MUTED
        elif status.get("syncing"):
            color = ui.MAGENTA
        elif status.get("link_up"):
            color = ui.GREEN
        else:
            color = ui.YELLOW
        self.sync_dot.configure(fg=color)
        return status

    def _tick_status(self):
        status = self._show_sync_status()
        imported = (status or {}).get("imported")
        if imported != self._last_imported:
            # A record was committed: look at the folder now instead of in 3 s.
            self._last_imported = imported
            self._schedule("poll", 50, self._poll_folders)
        self._schedule("status", STATUS_MS, self._tick_status)

    # ------------------------------------------------------------------ filters / refresh
    def _spec(self) -> EventFilter:
        errors = []

        def number(variable, label):
            text = variable.get().strip().replace(",", ".")
            if not text:
                return None
            try:
                return float(text)
            except ValueError:
                errors.append(f"{label} '{text}' is not a number")
                return None

        try:
            frequency = parse_frequency_filter(self.frequency.get())
        except ValueError as exc:
            frequency = None
            errors.append(str(exc))
        try:
            start = parse_time_filter(self.start_time.get())
        except ValueError as exc:
            start = None
            errors.append(f"From: {exc}")
        try:
            end = parse_time_filter(self.end_time.get(), end=True)
        except ValueError as exc:
            end = None
            errors.append(f"To: {exc}")
        spec = EventFilter(source_type=self.source.get().strip(), family_id=self._selected_family,
                           text=self.search.get().strip(), start=start, end=end,
                           min_rssi=number(self.rssi, "RSSI ≥"), max_rssi=number(self.max_rssi, "RSSI ≤"),
                           min_frequency_hz=frequency[0] if frequency else None,
                           max_frequency_hz=frequency[1] if frequency else None)
        self.filter_error.configure(text=("Filter ignored: " + "; ".join(errors)) if errors else "")
        return spec

    def refresh(self):
        if not self._alive:
            return
        spec = self._spec()
        events = self.project.filtered(spec)
        # Keep the navigator complete while the center view is filtered to a
        # selected family; otherwise selecting one row hides all others.
        navigator = events if not spec.family_id else self.project.filtered(
            dataclasses.replace(spec, family_id=""))
        summaries = self.project.family_summary(navigator)
        self._family_rows = [summary["family_id"] for summary in summaries]
        self.families.delete(0, "end")
        for summary in summaries:
            short = str(summary["family_id"]).replace("family-", "")[:12].upper()
            self.families.insert("end", f"▮ {short:<12} {summary['observation_count']:>4}")
        if self._selected_family in self._family_rows:
            index = self._family_rows.index(self._selected_family)
            self.families.selection_set(index)
            self.families.see(index)
        self.status.configure(text=f"{len(events)} SHOWN  ·  {len(self.project.events)} OBSERVATIONS  ·  "
                                   f"{len(self.project.grouper.families)} FAMILIES")
        self._set_family_details(self._selected_family)
        self._events = events
        self.scrub_scale.configure(to=max(0, len(events) - 1))
        self.scrub.set(min(self.scrub.get(), max(0, len(events) - 1)))
        self.draw(events)
        self._update_store_label()

    def _redraw(self):
        if self._alive:
            self.draw(self._events)

    def family_selected(self, _event=None):
        selected = self.families.curselection()
        self._selected_family = self._family_rows[selected[0]] if selected and selected[0] < len(
            self._family_rows) else ""
        self.refresh()

    def clear_family(self):
        self._selected_family = ""
        self.families.selection_clear(0, "end")
        self.refresh()

    @staticmethod
    def _write(widget, segments):
        """Replace a read-only text box with (text, tag) segments."""
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        for text, tag in segments:
            widget.insert("end", text, tag) if tag else widget.insert("end", text)
        widget.configure(state="disabled")

    @staticmethod
    def _pairs(rows):
        segments = []
        for key, value in rows:
            segments += [(f"{key:<13}", "muted"), (f"{value}\n", None)]
        return segments

    def _set_family_details(self, family_id):
        detail = self.project.family_detail(family_id) if family_id else {}
        if not detail.get("observation_count"):
            self._write(self.family_details, [("NO FAMILY SELECTED\n", "title"),
                                              ("pick one on the left, or click the waterfall", "muted")])
            return
        frequencies = "—"
        if detail["frequency_min_hz"]:
            frequencies = f"{detail['frequency_min_hz'] / 1e6:.3f}"
            if detail["frequency_max_hz"] != detail["frequency_min_hz"]:
                frequencies += f" – {detail['frequency_max_hz'] / 1e6:.3f}"
            frequencies += " MHz"
        hours = ", ".join(f"{hour:02d}" for hour in detail["time_of_day_hours"]) or "—"
        segments = [(f"{str(detail['family_id']).upper()}\n", "title")]
        segments += self._pairs([
            ("OBSERVED", f"{detail['observation_count']}×  ·  {frequencies}"),
            ("FIRST SEEN", detail["first_seen"]),
            ("LAST SEEN", detail["last_seen"]),
            ("MODULATION", ", ".join(detail["modulations"]) or "unknown"),
            ("VARIANTS", len(detail["waveform_variants"])),
            ("RSSI", f"{detail['rssi_min_dbm']:.1f} … {detail['rssi_max_dbm']:.1f} dBm"),
            ("ACTIVE (UTC)", hours),
            ("HYPOTHESIS", f"{detail['source_hypothesis']}  ·  {detail['similarity_confidence']:.0%}"),
            ("CAPTURES", f"{detail['raw_capture_count']} raw · {detail['imported_count']} imported · "
                         f"{detail['pending_count']} pending"),
            ("FOLLOW", "selected" if detail["follow_selected"] else "not selected"),
        ])
        self._write(self.family_details, segments)

    # ------------------------------------------------------------------ drawing
    def draw(self, events):
        self.draw_waterfall(events)
        self.draw_timeline(events)
        self.draw_spectrum(events)
        if events:
            self.show_event(events[min(self.scrub.get(), len(events) - 1)])

    @staticmethod
    def _grid(canvas, width, height, step_x=64, step_y=24):
        for x in range(step_x, width, step_x):
            canvas.create_line(x, 0, x, height, fill="#08161d")
        for y in range(step_y, height, step_y):
            canvas.create_line(0, y, width, y, fill="#08161d")

    @staticmethod
    def _neon(rssi):
        """Weak signals deep cyan, strong ones magenta, the strongest yellow."""
        t = max(0.0, min(1.0, ((rssi if rssi is not None else -110) + 100) / 60.0))
        if t < 0.6:
            k = t / 0.6
            r, g, b = 0x10 + (0x27 - 0x10) * k, 0x50 + (0xe0 - 0x50) * k, 0x60 + (0xe8 - 0x60) * k
        elif t < 0.9:
            k = (t - 0.6) / 0.3
            r, g, b = 0x27 + (0xff - 0x27) * k, 0xe0 + (0x2b - 0xe0) * k, 0xe8 + (0xd6 - 0xe8) * k
        else:
            k = (t - 0.9) / 0.1
            r, g, b = 0xff, 0x2b + (0xe1 - 0x2b) * k, 0xd6 + (0x4d - 0xd6) * k
        return "#%02x%02x%02x" % (int(r), int(g), int(b))

    def draw_waterfall(self, events):
        canvas = self.waterfall_canvas
        canvas.delete("all")
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())
        self._grid(canvas, width, height)
        rows = self.project.waterfall(events)[-waterfall_capacity(height):]
        self._waterfall_rows = rows
        self._waterfall_height = height
        frequencies = [row["frequency_hz"] for row in rows if row["frequency_hz"]]
        if not rows or not frequencies:
            cx, cy = width // 2, height // 2
            for dx, color in ((2, ui.MAGENTA), (-2, ui.CYAN), (0, "#f4fbff")):
                canvas.create_text(cx + dx, cy - 18, text="NO SIGNALS YET", fill=color,
                                   font=("Segoe UI Black", 18))
            canvas.create_text(cx, cy + 12, text="start RF on the Flipper:  RF tab  >  OK", fill=ui.DIM,
                               font=(ui.MONO, 9))
            canvas.create_text(cx, cy + 30, text="records arrive here while the Flipper is connected",
                               fill=ui.MUTED, font=(ui.MONO, 8))
            return
        low, high = min(frequencies), max(frequencies)
        span = max(1, high - low)
        row_h = waterfall_row_height(height, len(rows))
        for index, row in enumerate(rows):
            x = 14 + (row["frequency_hz"] - low) * (width - 28) / span
            y = height - (index + 1) * row_h  # newest row at the top
            color = self._neon(row["rssi_dbm"])
            canvas.create_rectangle(max(3, x - 4), y, min(width - 3, x + 4), y + max(1, row_h - 1),
                                    fill=color, outline="")
            if row["family_id"] == self._selected_family:
                canvas.create_rectangle(x - 7, y - 1, x + 7, y + row_h, outline=ui.MAGENTA, width=1)
            if row["event_id"] == self._selected_event:
                canvas.create_rectangle(x - 9, y - 2, x + 9, y + row_h + 1, outline=ui.YELLOW, width=2)
        canvas.create_text(10, 10, anchor="w", fill=ui.CYAN, font=(ui.MONO, 9, "bold"),
                           text=f"{low / 1e6:.3f} – {high / 1e6:.3f} MHz")
        canvas.create_text(width - 10, 10, anchor="e", fill=ui.MUTED, font=(ui.MONO, 8),
                           text="sampled RSSI · newest on top")

    def draw_timeline(self, events):
        canvas = self.timeline_canvas
        canvas.delete("all")
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())
        self._grid(canvas, width, height, step_x=48, step_y=1000)
        rows = self.project.timeline(events)
        y = height // 2 + 6
        canvas.create_line(12, y, width - 12, y, fill=ui.LINE, width=2)
        if not rows:
            return
        times = [row["when"].timestamp() for row in rows]
        lo, hi = min(times), max(times)
        span = max(1, hi - lo)
        for row, timestamp in zip(rows, times):
            x = 12 + (timestamp - lo) * (width - 24) / span
            selected = row["family_id"] == self._selected_family
            canvas.create_line(x, y - (10 if selected else 6), x, y + (10 if selected else 6),
                               fill=ui.MAGENTA if selected else ui.CYAN, width=2 if selected else 1)
        if self._selected_event:
            for row, timestamp in zip(rows, times):
                if row.get("event_id") == self._selected_event:
                    x = 12 + (timestamp - lo) * (width - 24) / span
                    canvas.create_polygon(x - 5, y - 16, x + 5, y - 16, x, y - 9, fill=ui.YELLOW, outline="")
        start = (EPOCH + timedelta(seconds=lo)).strftime("%Y-%m-%d %H:%M:%S")
        end = (EPOCH + timedelta(seconds=hi)).strftime("%Y-%m-%d %H:%M:%S")
        canvas.create_text(12, 12, anchor="w", text=start + " UTC", fill=ui.DIM, font=(ui.MONO, 8))
        canvas.create_text(width - 12, 12, anchor="e", text=end + " UTC", fill=ui.DIM, font=(ui.MONO, 8))

    def draw_spectrum(self, events):
        canvas = self.spectrum_canvas
        canvas.delete("all")
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())
        self._grid(canvas, width, height, step_x=48, step_y=20)
        rows = self.project.spectrum(events)
        canvas.create_text(10, 10, anchor="w", text="peak RSSI by frequency", fill=ui.MUTED,
                           font=(ui.MONO, 8))
        if not rows:
            return
        points = []
        for index, row in enumerate(rows):
            if row["rssi_dbm"] is None:
                continue
            x = 10 + index * (width - 20) / max(1, len(rows) - 1)
            y = height - 10 - max(0, min(1, (row["rssi_dbm"] + 110) / 80)) * (height - 28)
            points.extend((x, y))
        if len(points) >= 4:
            canvas.create_line(*points, fill="#5a1050", width=6, smooth=True)    # glow
            canvas.create_line(*points, fill=ui.MAGENTA, width=2, smooth=True)

    # ------------------------------------------------------------------ selection
    def _select_index(self, index: int):
        if 0 <= index < len(self._events):
            self.scrub.set(index)
            self.draw(self._events)

    def canvas_event(self, event):
        """Select the waterfall row under the click (same mapping as the drawing)."""
        index = waterfall_index_at(event.y, self._waterfall_height, len(self._waterfall_rows))
        if index is None:
            return
        event_id = self._waterfall_rows[index]["event_id"]
        for position, item in enumerate(self._events):
            if item.event_id == event_id:
                self._select_index(position)
                return

    def timeline_event(self, event):
        events = self._events
        if not events:
            return
        times = [parse_time(item.captured_at_utc).timestamp() for item in events]
        lo, hi = min(times), max(times)
        ratio = max(0.0, min(1.0, (event.x - 12) / max(1, self.timeline_canvas.winfo_width() - 24)))
        target = lo + ratio * max(1, hi - lo)
        self._select_index(min(range(len(times)), key=lambda i: abs(times[i] - target)))

    def scrub_changed(self, _value=None):
        if self._events:
            self.draw(self._events)

    def toggle_play(self):
        self.playing = not self.playing
        self.play_button.configure(text="PAUSE" if self.playing else "PLAY")
        if self.playing:
            self._play_step()

    def _play_step(self):
        if not self.playing or not self._alive:
            return
        if not self._events:
            self.playing = False
            self.play_button.configure(text="PLAY")
            return
        self.scrub.set((self.scrub.get() + 1) % len(self._events))
        self.draw(self._events)
        self._schedule("play", PLAY_MS, self._play_step)

    def show_event(self, event):
        self._selected_event = event.event_id
        project = self.project
        family = project.family_key(event)
        self._set_family_details(family)
        hint = event.fingerprint_id or ""
        nfc_details = ""
        if event.source_type == "nfc":
            nfc_details = (f"NFC {event.nfc_technology or 'unknown'} / "
                           f"{event.nfc_protocol or 'unknown'}\n"
                           f"Field interval {event.nfc_field_duration_ms} ms · "
                           f"observations {event.nfc_field_count}\n")
        segments = [(f"{event.event_id}\n", "title")]
        segments += self._pairs([
            ("CAPTURED", event.captured_at_utc),
            ("FREQUENCY", f"{event.frequency_hz / 1e6:.3f} MHz · {event.modulation}"),
            ("RSSI", f"{event.rssi_avg_dbm:.1f} dBm · {event.duration_us} us"),
            ("FAMILY", family),
            ("FINGERPRINT", project.fingerprint_key(event)
             + (f"  (Flipper {hint})" if hint.startswith("local-") else "")),
            ("PULSES", f"{len(event.pulse_timings_us)} timings"),
            ("RAW", f"{len(project.capture_bytes(event))} bytes · {event.upload_state}"),
            ("CLASS", f"{event.classification} ({event.classification_confidence:.0%})"),
            ("DEVICE", f"{event.device_uuid} / {event.session_id}"),
        ])
        if nfc_details:
            segments.append((nfc_details, None))
        similar = project.similar_events(event, limit=5)
        segments.append(("\nSIMILAR OBSERVATIONS\n", "head"))
        if similar:
            for row in similar:
                comparison = row["comparison"]
                reasons = "; ".join(comparison.get("reasons", ())) or "no stable feature match"
                segments += [(f"{comparison.get('percent', 0):>3}%  ", "good"),
                             (f"{row['event'].event_id[:18]}  {comparison.get('relationship_text', 'unknown')}\n", None),
                             (f"      {reasons}\n", "muted")]
        else:
            segments.append(("no other observations\n", "muted"))
        self._write(self.details, segments)
        note = project.note(event.event_id)
        self.note_entry.delete(0, "end")
        self.note_entry.insert(0, note.get("text", ""))
        self.location_entry.delete(0, "end")
        self.location_entry.insert(0, note.get("location", ""))

    # ------------------------------------------------------------------ actions
    def save_note(self):
        if self._selected_event:
            self.project.add_note(self._selected_event, self.note_entry.get(), self.location_entry.get())

    def save_project(self):
        path = self.filedialog.asksaveasfilename(parent=self.window, defaultextension=".rfproject.json",
                                                 filetypes=[("RF projects", "*.rfproject.json")])
        if not path:
            return
        try:
            self.project.save_project(path)
        except Exception as exc:
            self._report_error("Cannot save project", exc)

    def load_project(self):
        path = self.filedialog.askopenfilename(parent=self.window,
                                               filetypes=[("RF projects", "*.rfproject.json"), ("JSON", "*.json")])
        if not path:
            return
        try:
            self.project.load_project(path)
        except Exception as exc:
            self._report_error("Cannot load project", exc)
        if self.store_root and self.store_root not in self.project.sources:
            self._open_root(self.store_root, report=False)
        self._folder_signatures = {root: folder_signature(root) for root in self.project.sources}
        self.refresh()

    def export_follow(self):
        if not self._selected_family:
            self.messagebox.showinfo("Export Follow profile", "Select a signal family first.", parent=self.window)
            return
        path = self.filedialog.asksaveasfilename(parent=self.window, defaultextension=".follow.json")
        if not path:
            return
        try:
            self.project.export_follow(self._selected_family, path)
        except Exception as exc:
            self._report_error("Cannot export Follow profile", exc)

    def export(self, kind):
        path = self.filedialog.asksaveasfilename(parent=self.window, defaultextension="." + kind,
                                                 filetypes=[(kind.upper(), "*." + kind)])
        if not path:
            return
        events = self.project.filtered(self._spec())
        try:
            (self.project.export_json if kind == "json" else self.project.export_csv)(path, events)
        except Exception as exc:
            self._report_error(f"Cannot export {kind.upper()}", exc)
            return
        self.status.configure(text=f"Exported {len(events)} observations to {os.path.basename(path)}")


# =========================================================================== standalone / CLI
def open_analyzer(store_dirs=None) -> int:
    """Standalone viewer: creates its own Tk root in the calling thread and blocks in mainloop.

    The first folder is the primary store (default: the companion store
    :func:`default_store_root`), further folders are opened read-only.
    Imports are not available here (``sync=None``).
    """
    import tkinter as tk
    from tkinter import ttk

    roots = [os.fspath(path) for path in (store_dirs or ())]
    root = tk.Tk()
    try:
        root.withdraw()
        try:
            ttk.Style(root).theme_use("clam")
        except tk.TclError:
            pass
        window = AnalyzerWindow(root, roots[0] if roots else default_store_root(), sync=None,
                                extra_roots=roots[1:], on_close=root.quit)
        try:
            root.mainloop()
        finally:
            window.close()
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass
    return 0


def setup_standalone_logging() -> None:
    """Log to %LOCALAPPDATA%\\DedSecUplink\\rf_hunter.log (pythonw has no console).

    Called by the script entry points only; inside the companion the
    ``uplink.rf_analyzer`` logger goes to the companion's log.
    """
    root_logger = logging.getLogger()
    if root_logger.handlers:
        return
    try:
        os.makedirs(app_data_dir(), exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            os.path.join(app_data_dir(), LOG_NAME), maxBytes=512 * 1024, backupCount=2, encoding="utf-8")
    except OSError:
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)


def _show_message(title: str, text: str, error: bool = False) -> None:
    """Best-effort dialog for windowed builds without stdout/stderr."""
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        try:
            (messagebox.showerror if error else messagebox.showinfo)(title, text, parent=root)
        finally:
            root.destroy()
    except Exception:
        log.debug("cannot show message dialog", exc_info=True)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rf_analyzer", description="RF Signal Hunter analyzer (viewer and exports).")
    parser.add_argument("roots", nargs="*",
                        help="event store folders (default: the companion store "
                             "%%LOCALAPPDATA%%\\DedSecUplink\\rf_hunter); a copied SD journal "
                             "(apps_data/dedsec_uplink/rf or its events folder) is accepted")
    parser.add_argument("--export-json", metavar="PATH", help="write the observations as JSON and exit")
    parser.add_argument("--export-csv", metavar="PATH", help="write the observations as CSV and exit")
    return parser


def _parse_args(parser: argparse.ArgumentParser, argv):
    """argparse that also works under pythonw/windowed builds (stdout/stderr are None)."""
    if sys.stdout is not None and sys.stderr is not None:
        return parser.parse_args(argv)
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            return parser.parse_args(argv)
    except SystemExit as exc:
        text = buffer.getvalue().strip()
        if text:
            log.info("command line: %s", text)
            _show_message("RF Signal Hunter", text, error=bool(exc.code))
        raise


def main(argv=None) -> int:
    """CLI: ``rf_analyzer [folders...] [--export-json PATH] [--export-csv PATH]``."""
    parser = _build_parser()
    try:
        args = _parse_args(parser, argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 2)
    if args.export_json or args.export_csv:
        project = AnalyzerProject()
        try:
            for root in args.roots or [default_store_root()]:
                project.add_root(root)
            if args.export_json:
                project.export_json(args.export_json)
            if args.export_csv:
                project.export_csv(args.export_csv)
        except Exception as exc:
            log.exception("RF export failed")
            if sys.stderr is not None:
                print(f"RF export failed: {exc}", file=sys.stderr)
            else:
                _show_message("RF Signal Hunter", f"Export failed:\n{exc}", error=True)
            return 1
        return 0
    try:
        import tkinter as tk
        tcl_error = tk.TclError
    except ImportError as exc:
        log.error("Tkinter is unavailable: %s", exc)
        tcl_error = None
    if tcl_error is not None:
        try:
            return open_analyzer(args.roots or None)
        except tcl_error as exc:
            log.error("no display for the RF analyzer: %s", exc)
        except Exception as exc:
            log.exception("RF analyzer failed")
            _show_message("RF Signal Hunter", f"The analyzer stopped:\n{exc}", error=True)
            return 1
    # Headless fallback: summarize what would have been shown.
    project = AnalyzerProject()
    for root in args.roots or [default_store_root()]:
        try:
            project.add_root(root)
        except Exception as exc:
            print(f"{root}: {exc}")
    print(f"{len(project.events)} observations from {len(project.sources)} folder(s)")
    return 0 if args.roots else 1


if __name__ == "__main__":
    setup_standalone_logging()
    raise SystemExit(main())
