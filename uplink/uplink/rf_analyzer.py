"""Desktop RF Hunter analyzer (investigation window of the DedSec Uplink companion).

:class:`AnalyzerProject` is headless: it merges any number of event stores
(opened read-only), deduplicates them by event ID and keeps desktop family
grouping in memory.  :class:`AnalyzerWindow` is a ``tk.Toplevel`` that the
companion opens on its single Tk UI thread; it reloads the companion's store
when the folder changes, shows :meth:`RfSync.status` and offers
FLIPPER -> PC / FLIPPER <- PC.  It never opens a BLE connection - transfers
are done by :class:`uplink.rf_sync.RfSync` over the companion's link.

The window shows sampled RF observations (discrete bursts with RSSI values and
pulse timing), not continuous IQ: a summary, an activity chart, the list of
captures with a plain verdict and the recorded pulses of one capture.
``main(argv)`` keeps the CLI: exports and a standalone viewer with its own Tk root.
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
import math
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
           "signal_verdict", "store_summary", "format_local", "format_duration_us"]


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
        if status.get("carry"):
            parts.append(f"carrying {status['carry']} for other PCs")
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
    if status.get("pushing"):
        parts.append(f"to the Flipper {status.get('push_done', 0)}/{status.get('push_total', 0)}")
    parts.append(f"imported {status.get('imported', 0)} · failed {status.get('failed', 0)}")
    if status.get("last_push") and not status.get("pushing"):
        parts.append(f"last transfer {status.get('push_sent', 0)} sent, "
                     f"{status.get('push_present', 0)} already there")
    if status.get("last_sync"):
        parts.append(f"last sync {_format_local_time(status['last_sync'])}")
    errors = []
    if status.get("last_error"):
        errors.append(f"Last error: {status['last_error']}")
    if status.get("push_error"):
        errors.append(f"Transfer to the Flipper: {status['push_error']}")
    return " · ".join(parts), "   ".join(errors)


# =========================================================================== reading captures
# Plain-language views of the records for the window (pure functions, tested headless).
BANDS = {"ALL": (None, None, ""), "315": (300_000_000, 330_000_000, ""),
         "433": (420_000_000, 450_000_000, ""), "868": (860_000_000, 880_000_000, ""),
         "NFC": (None, None, "nfc")}
VERDICT_TEXT = {"signal": "signal", "noise": "noise", "nfc": "NFC field"}
NOISE_EDGES = 16            # fewer recorded edges than this ...
NOISE_LENGTH_US = 50_000    # ... in a shorter burst: a noise spike, not a transmission
TICK_STEPS = (60, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800)


def format_duration_us(us) -> str:
    us = max(0, int(us or 0))
    if us < 1000:
        return f"{us} µs"
    if us < 10_000:
        return f"{us / 1000:.1f} ms"
    if us < 1_000_000:
        return f"{us / 1000:.0f} ms"
    return f"{us / 1e6:.1f} s"


def format_local(epoch, full: bool = False, now: Optional[datetime] = None) -> str:
    """Local time: ``HH:MM:SS`` today, ``MM-DD HH:MM`` on other days; ``full`` adds the date and UTC."""
    try:
        local = datetime.fromtimestamp(float(epoch))
        utc = datetime.fromtimestamp(float(epoch), timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"
    if full:
        return f"{local:%Y-%m-%d %H:%M:%S}  ({utc:%H:%M} UTC)"
    if local.date() == (now or datetime.now()).date():
        return f"{local:%H:%M:%S}"
    return f"{local:%m-%d %H:%M}"


def signal_verdict(event: RfEvent) -> tuple:
    """``(kind, explanation)``: ``signal``, ``noise`` or ``nfc``, in plain words."""
    if event.source_type == "nfc":
        return "nfc", ("A reader's 13.56 MHz field was next to the Flipper: a card reader, a door lock "
                       "or a phone with NFC.")
    edges = len(event.pulse_timings_us)
    length = format_duration_us(event.duration_us)
    if edges < NOISE_EDGES and event.duration_us < NOISE_LENGTH_US:
        return "noise", (f"Only {edges} edge(s) in {length}: too short for a remote or a sensor. Interference "
                         "next to the Flipper crossed the trigger level for a moment; the current Flipper app "
                         "no longer saves these.")
    return "signal", (f"{edges} edges over {length}: an on/off keyed transmission. Remotes, key fobs, "
                      "doorbells and weather sensors look like this.")


def lane_of(event: RfEvent) -> str:
    if event.source_type == "nfc":
        return "NFC"
    for name in ("315", "433", "868"):
        low, high, _source = BANDS[name]
        if low <= event.frequency_hz <= high:
            return name
    return "OTHER"


def activity_lanes(events) -> list:
    """The three Sub-GHz bands always (they give the picture context), then what else occurs."""
    present = {lane_of(event) for event in events}
    return ["315", "433", "868"] + [lane for lane in ("OTHER", "NFC") if lane in present]


def activity_window(times) -> tuple:
    """Time range to draw: the captures with a margin, at least ten minutes wide."""
    lo, hi = min(times), max(times)
    span = hi - lo
    pad = (600 - span) / 2 + 30 if span < 600 else span * 0.04
    return lo - pad, hi + pad


def time_ticks(t0: float, t1: float, most: int = 8) -> list:
    """``(epoch, label)`` gridlines on whole local minutes / hours / days."""
    span = max(1.0, t1 - t0)
    step = next((s for s in TICK_STEPS if span / s <= most), TICK_STEPS[-1])
    try:
        offset = datetime.fromtimestamp(t0).astimezone().utcoffset().total_seconds()
    except (OverflowError, OSError, ValueError, AttributeError):
        offset = 0.0
    first = math.ceil((t0 + offset) / step) * step - offset
    ticks = []
    t = first
    while t <= t1 and len(ticks) <= most + 1:
        local = datetime.fromtimestamp(t)
        ticks.append((t, f"{local:%m-%d}" if step >= 86400 else f"{local:%H:%M}"))
        t += step
    return ticks


def dot_radius(event: RfEvent) -> float:
    """3 px for a blip, up to 9 px for a capture of a second or more."""
    us = event.nfc_field_duration_ms * 1000 if event.source_type == "nfc" else event.duration_us
    return 3.0 + min(6.0, math.log10(max(1, us) / 1000.0 + 1.0) * 2.0)


def neon(rssi) -> str:
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


def strength_bar(dbm) -> str:
    """Five blocks: -100 dBm empty, -50 dBm and stronger full."""
    level = max(0, min(5, int(((dbm or -110) + 100) // 10)))
    return "▮" * level + "▯" * (5 - level)


def group_numbers(project: "AnalyzerProject", events) -> dict:
    """Family id -> ``#n`` among real signals, the most frequent first."""
    project.ensure_families()
    counts: dict = {}
    for event in events:
        if signal_verdict(event)[0] == "signal":
            key = project.family_key(event)
            counts[key] = counts.get(key, 0) + 1
    ordered = sorted(counts, key=lambda key: (-counts[key], key))
    return {key: f"#{index + 1}" for index, key in enumerate(ordered)}


def store_summary(events, now: Optional[datetime] = None) -> dict:
    """The few numbers on top of the window."""
    events = list(events)
    kinds = [signal_verdict(event)[0] for event in events]
    signals = [event for event, kind in zip(events, kinds) if kind == "signal"]
    sub_ghz = signals or [event for event, kind in zip(events, kinds) if kind == "noise"]
    frequencies = frequency_clusters(event.frequency_hz for event in sub_ghz)
    bands = ", ".join(f"{hz / 1e6:.2f}" for hz in frequencies[:3])
    if len(frequencies) > 3:
        bands += f" +{len(frequencies) - 3}"
    newest = max(events, key=lambda event: event.captured_at_unix or 0, default=None)
    strongest = max(signals, key=lambda event: event.rssi_max_dbm, default=None)
    return {"signals": len(signals), "noise": kinds.count("noise"), "nfc": kinds.count("nfc"),
            "bands": bands, "last": format_local(newest.captured_at_unix, now=now) if newest else "",
            "strongest": f"{strongest.rssi_max_dbm:.0f} dBm" if strongest else ""}


def frequency_clusters(frequencies, gap_hz: int = 100_000) -> list:
    """One frequency per transmitter channel: captures less than 100 kHz apart are one channel
    (its median), so 433.912 and 433.928 MHz read as 433.92."""
    values = sorted(hz for hz in frequencies if hz)
    clusters, current = [], []
    for hz in values:
        if current and hz - current[-1] >= gap_hz:
            clusters.append(int(statistics.median(current)))
            current = []
        current.append(hz)
    if current:
        clusters.append(int(statistics.median(current)))
    return clusters


def default_selection(events) -> str:
    """The newest real capture, else the newest of all."""
    ordered = sorted(events, key=lambda event: (event.captured_at_unix or 0, event.event_id), reverse=True)
    for event in ordered:
        if signal_verdict(event)[0] != "noise":
            return event.event_id
    return ordered[0].event_id if ordered else ""


class AnalyzerWindow:
    """RF investigation window: a ``tk.Toplevel`` on an existing Tk root.

    ``parent`` is the companion's Tk root (or any widget of it); all calls must
    come from that root's thread.  ``store_root`` is the companion's event
    store (polled for changes every few seconds, opened read-only), ``sync``
    an optional :class:`uplink.rf_sync.RfSync` whose status is shown and whose
    ``sync_now()`` / ``push_now()`` the two buttons call.  ``extra_roots`` are
    further folders opened read-only (for example a copied SD journal) and
    ``on_close`` is called after the window was destroyed.

    The window is meant to be read at a glance: a summary, an activity chart
    (time across, one lane per band), the list of captures in local time with
    a plain verdict (signal / noise / NFC), and for the selected capture its
    details, the recorded pulses and the captures that look like it.
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
        self._selected_event = ""
        self._events: list = []
        self._groups: dict = {}
        self._dots: list = []
        self._similar_rows: list = []
        self._band = "ALL"
        self._last_imported = None
        self.window = tk.Toplevel(parent)
        self.window.title("DEDSEC // RF HUNTER")
        self.window.geometry("1280x820")
        self.window.minsize(980, 700)
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
        ui.style_treeview(window)
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

        toolbar = tk.Frame(window, bg=ui.BG)
        toolbar.pack(fill="x", padx=12, pady=(2, 2))
        self.sync_button = ui.NeonButton(toolbar, "FLIPPER → PC", self.sync_now,
                                         style="primary").pack(side="left", padx=(0, 8))
        self.push_button = ui.NeonButton(toolbar, "FLIPPER ← PC", self.push_now,
                                         style="accent").pack(side="left")
        if self.sync is None:
            self.sync_button.state(["disabled"])
            self.push_button.state(["disabled"])
        tk.Label(toolbar, text="import the Flipper's records  ·  put this PC's records on the Flipper "
                               "for another PC", fg=ui.MUTED, bg=ui.BG, font=(ui.MONO, 8)).pack(
            side="left", padx=(12, 0))

        line = tk.Frame(window, bg=ui.BG)
        line.pack(fill="x", padx=12, pady=(6, 0))
        self.sync_dot = tk.Label(line, text="●", fg=ui.MUTED, bg=ui.BG, font=(ui.MONO, 10))
        self.sync_dot.pack(side="left")
        self.sync_label = tk.Label(line, text="", fg=ui.TEXT, bg=ui.BG, font=(ui.MONO, 9, "bold"),
                                   anchor="w")
        self.sync_label.pack(side="left", padx=(4, 0))
        self.sync_error = tk.Label(window, text="", fg=ui.RED, bg=ui.BG, font=(ui.MONO, 9), anchor="w")
        self.sync_error.pack(fill="x", padx=12)

        # summary: what the store holds, in a few big numbers
        cards = tk.Frame(window, bg=ui.BG)
        cards.pack(fill="x", padx=12, pady=(4, 8))
        self._cards = {}
        for key, label, color in (("signals", "SIGNALS", ui.GREEN), ("noise", "NOISE", ui.DIM),
                                  ("nfc", "NFC FIELDS", ui.CYAN), ("bands", "FREQUENCIES", ui.TEXT),
                                  ("last", "LAST CAPTURE", ui.TEXT), ("strongest", "STRONGEST", ui.MAGENTA)):
            card = tk.Frame(cards, bg=ui.PANEL, highlightthickness=1, highlightbackground=ui.LINE)
            card.pack(side="left", padx=(0, 8), ipadx=10, ipady=2)
            value = tk.Label(card, text="—", fg=color, bg=ui.PANEL, font=(ui.MONO, 15, "bold"),
                             anchor="w")
            value.pack(anchor="w", padx=8, pady=(4, 0))
            tk.Label(card, text=label, fg=ui.MUTED, bg=ui.PANEL, font=(ui.MONO, 7, "bold"),
                     anchor="w").pack(anchor="w", padx=8, pady=(0, 4))
            self._cards[key] = value

        # packed before the panes so that a small window clips the panes, not this line
        self.store_label = tk.Label(window, text="", fg=ui.MUTED, bg=ui.BG, font=(ui.MONO, 8), anchor="w")
        self.store_label.pack(side="bottom", fill="x", padx=12, pady=(0, 6))
        body = tk.PanedWindow(window, orient="horizontal", bg=ui.LINE, sashwidth=3, borderwidth=0,
                              sashrelief="flat", opaqueresize=True)
        body.pack(fill="both", expand=True, padx=12, pady=(0, 4))
        left = tk.Frame(body, bg=ui.BG)
        right = tk.Frame(body, bg=ui.BG)
        body.add(left, minsize=520, stretch="always")
        body.add(right, minsize=330, width=410, stretch="never")

        # ---- left: activity chart, then the list
        head = tk.Frame(left, bg=ui.BG)
        head.pack(fill="x", padx=(0, 8))
        ui.section(head, "ACTIVITY").pack(side="left", fill="x", expand=True)
        tk.Label(head, text="one dot per capture · bigger = longer · colour = stronger · "
                            "hollow = noise", fg=ui.MUTED, bg=ui.BG, font=(ui.MONO, 8)).pack(
            side="right", padx=(8, 0))
        self.activity = tk.Canvas(left, bg="#020507", highlightthickness=1, highlightbackground=ui.LINE,
                                  height=190)
        self.activity.pack(fill="x", padx=(0, 8), pady=(4, 8))
        self.activity.bind("<Button-1>", self.activity_click)
        self.activity.bind("<Configure>", lambda _event: self._schedule("activity", 80, self._draw_activity))

        list_head = tk.Frame(left, bg=ui.BG)
        list_head.pack(fill="x", padx=(0, 8))
        ui.section(list_head, "CAPTURES").pack(side="left", fill="x", expand=True)
        filters = tk.Frame(left, bg=ui.BG)
        filters.pack(fill="x", padx=(0, 8), pady=(4, 4))
        self.search = tk.StringVar(master=window)
        search = ui.entry(filters, self.search, 22)
        search.pack(side="left", ipady=2)
        search.bind("<KeyRelease>", lambda _event: self._schedule("search", 250, self.refresh))
        tk.Label(filters, text="search", fg=ui.MUTED, bg=ui.BG, font=(ui.MONO, 8)).pack(
            side="left", padx=(4, 12))
        self._band_chips = {}
        for band in ("ALL", "315", "433", "868", "NFC"):
            chip = ui.Chip(filters, band, lambda band=band: self.set_band(band))
            chip.pack(side="left", padx=(0, 4))
            self._band_chips[band] = chip
        self._band_chips["ALL"].set(True)
        self.hide_noise = ui.Chip(filters, "HIDE NOISE", self._toggle_noise)
        self.hide_noise.pack(side="left", padx=(12, 0))

        table = tk.Frame(left, bg=ui.PANEL, highlightthickness=1, highlightbackground=ui.LINE)
        table.pack(fill="both", expand=True, padx=(0, 8), pady=(0, 4))
        columns = (("time", "TIME", 128, "w"), ("mhz", "MHZ", 74, "e"), ("dbm", "DBM", 56, "e"),
                   ("length", "LENGTH", 72, "e"), ("edges", "EDGES", 60, "e"), ("group", "GROUP", 64, "center"),
                   ("verdict", "VERDICT", 90, "w"))
        self.table = ttk.Treeview(table, columns=[c[0] for c in columns], show="headings",
                                  selectmode="browse", style="DedSec.Treeview")
        for key, title, width, anchor in columns:
            self.table.heading(key, text=title, anchor=anchor)
            self.table.column(key, width=width, minwidth=40, anchor=anchor, stretch=key == "verdict")
        self.table.tag_configure("noise", foreground=ui.MUTED)
        self.table.tag_configure("nfc", foreground=ui.CYAN)
        self.table.tag_configure("signal", foreground=ui.TEXT)
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.table.yview,
                               style="DedSec.Vertical.TScrollbar")
        self.table.configure(yscrollcommand=scroll.set)
        self.table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.table.bind("<<TreeviewSelect>>", self._table_selected)

        # ---- right: the selected capture
        ui.section(right, "CAPTURE", ui.MAGENTA).pack(fill="x", padx=(8, 0))
        details_box, self.details = ui.text_box(right, height=11)
        details_box.pack(fill="x", padx=(8, 0), pady=(4, 8))
        ui.section(right, "RECORDED PULSES", ui.MAGENTA).pack(fill="x", padx=(8, 0))
        self.pulses = tk.Canvas(right, bg="#020507", highlightthickness=1, highlightbackground=ui.LINE,
                                height=100)
        self.pulses.pack(fill="x", padx=(8, 0), pady=(4, 8))
        self.pulses.bind("<Configure>", lambda _event: self._schedule("pulses", 80, self._draw_pulses))
        # the note sits at the bottom; "looks like" takes what is left between
        notes = tk.Frame(right, bg=ui.BG)
        notes.pack(side="bottom", fill="x", padx=(8, 0), pady=(4, 0))
        self.note_entry = ui.entry(notes)
        self.note_entry.pack(side="left", fill="x", expand=True, ipady=2)
        ui.NeonButton(notes, "SAVE NOTE", self.save_note, style="accent").pack(side="left", padx=(6, 0))
        ui.section(right, "NOTE", ui.MAGENTA).pack(side="bottom", fill="x", padx=(8, 0))
        ui.section(right, "LOOKS LIKE", ui.MAGENTA).pack(fill="x", padx=(8, 0))
        box = tk.Frame(right, bg=ui.PANEL, highlightthickness=1, highlightbackground=ui.LINE)
        box.pack(fill="both", expand=True, padx=(8, 0), pady=(4, 8))
        self.similar = tk.Listbox(box, bg=ui.PANEL, fg=ui.TEXT, selectbackground=ui.MAGENTA,
                                  selectforeground=ui.BG, relief="flat", exportselection=False,
                                  font=(ui.MONO, 9), activestyle="none", highlightthickness=0,
                                  borderwidth=0, height=3)
        self.similar.pack(fill="both", expand=True, padx=6, pady=4)
        self.similar.bind("<<ListboxSelect>>", self._similar_selected)

    # ------------------------------------------------------------------ folders
    def _open_root(self, path, report: bool = True) -> bool:
        try:
            self.project.add_root(path)
            root = resolve_store_root(path)
            self._folder_signatures[root] = folder_signature(root)
            return True
        except Exception as exc:
            if report:
                self._report_error("Cannot open RF folder", f"{path}\n\n{exc}")
            else:
                log.warning("cannot open RF store %s: %s", path, exc)
            return False

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

    def push_now(self):
        if self.sync is None:
            return
        try:
            self.sync.push_now()
        except Exception as exc:
            self._report_error("Transfer to the Flipper failed", exc)
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
        elif status.get("syncing") or status.get("pushing"):
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
    def set_band(self, band: str):
        self._band = band if band in BANDS else "ALL"
        for name, chip in self._band_chips.items():
            chip.set(name == self._band)
        self.refresh()

    def _toggle_noise(self):
        self.hide_noise.set(not self.hide_noise.on)
        self.refresh()

    def _spec(self) -> EventFilter:
        low, high, source = BANDS.get(self._band, (None, None, ""))
        return EventFilter(source_type=source, text=self.search.get().strip(),
                           min_frequency_hz=low, max_frequency_hz=high)

    def refresh(self):
        if not self._alive:
            return
        everything = self.project.filtered(EventFilter())
        self._groups = group_numbers(self.project, everything)
        events = self.project.filtered(self._spec())
        if self.hide_noise.on:
            events = [event for event in events if signal_verdict(event)[0] != "noise"]
        self._events = events
        self._fill_summary(everything)
        self._fill_table(events)
        visible = {event.event_id for event in events}
        if self._selected_event not in visible:
            self._selected_event = default_selection(events)
        self._select(self._selected_event, scroll=True)
        self._update_store_label()

    def _fill_summary(self, events):
        summary = store_summary(events)
        cards = self._cards
        cards["signals"].configure(text=str(summary["signals"]))
        cards["noise"].configure(text=str(summary["noise"]))
        cards["nfc"].configure(text=str(summary["nfc"]))
        cards["bands"].configure(text=summary["bands"] or "—")
        cards["last"].configure(text=summary["last"] or "—")
        cards["strongest"].configure(text=summary["strongest"] or "—")

    def _fill_table(self, events):
        table = self.table
        table.delete(*table.get_children())
        for event in sorted(events, key=lambda item: (item.captured_at_unix or 0, item.event_id), reverse=True):
            kind, _text = signal_verdict(event)
            edges = len(event.pulse_timings_us)
            table.insert("", "end", iid=event.event_id, tags=(kind,), values=(
                format_local(event.captured_at_unix),
                "NFC" if event.source_type == "nfc" else f"{event.frequency_hz / 1e6:.2f}",
                "" if event.source_type == "nfc" else f"{event.rssi_max_dbm:.0f}",
                format_duration_us(event.duration_us if event.source_type != "nfc"
                                   else event.nfc_field_duration_ms * 1000),
                "" if event.source_type == "nfc" else str(edges),
                "" if kind == "noise" else self._groups.get(self.project.family_key(event), ""),
                VERDICT_TEXT[kind]))

    # ------------------------------------------------------------------ selection
    def _event(self, event_id):
        return self.project.events.get(event_id) if event_id else None

    def _select(self, event_id, scroll=False):
        self._selected_event = event_id or ""
        if event_id and self.table.exists(event_id):
            if self.table.selection() != (event_id,):
                self.table.selection_set(event_id)
            if scroll:
                self.table.see(event_id)
        self.show_event(self._event(event_id))
        self._draw_activity()

    def _table_selected(self, _event=None):
        selection = self.table.selection()
        if selection and selection[0] != self._selected_event:
            self._select(selection[0])

    def _similar_selected(self, _event=None):
        selection = self.similar.curselection()
        if selection and selection[0] < len(self._similar_rows):
            event_id = self._similar_rows[selection[0]]
            if self.table.exists(event_id):
                self._select(event_id, scroll=True)

    def activity_click(self, event):
        """Select the capture whose dot is nearest to the click (within 14 px)."""
        best, distance = None, 14.0 ** 2
        for x, y, event_id in self._dots:
            d = (x - event.x) ** 2 + (y - event.y) ** 2
            if d <= distance:
                best, distance = event_id, d
        if best:
            self._select(best, scroll=True)

    # ------------------------------------------------------------------ drawing
    def _draw_activity(self):
        canvas = self.activity
        canvas.delete("all")
        self._dots = []
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())
        events = self._events
        if not events:
            cx, cy = width // 2, height // 2
            for dx, color in ((2, ui.MAGENTA), (-2, ui.CYAN), (0, "#f4fbff")):
                canvas.create_text(cx + dx, cy - 16, text="NO CAPTURES YET", fill=color,
                                   font=("Segoe UI Black", 16))
            canvas.create_text(cx, cy + 12, text="start RF on the Flipper:  RF tab  >  OK", fill=ui.DIM,
                               font=(ui.MONO, 9))
            canvas.create_text(cx, cy + 30, text="captures arrive here while the Flipper is connected",
                               fill=ui.MUTED, font=(ui.MONO, 8))
            return
        lanes = activity_lanes(events)
        left, right, top, bottom = 58, width - 14, 8, height - 22
        lane_h = (bottom - top) / len(lanes)
        times = [event.captured_at_unix or 0 for event in events]
        t0, t1 = activity_window(times)
        span = max(1.0, t1 - t0)

        def x_of(t):
            return left + (t - t0) * (right - left) / span

        for t, label in time_ticks(t0, t1):
            x = x_of(t)
            canvas.create_line(x, top, x, bottom, fill="#0b1d26")
            canvas.create_text(x, bottom + 11, text=label, fill=ui.DIM, font=(ui.MONO, 8))
        for index, lane in enumerate(lanes):
            y0 = top + index * lane_h
            canvas.create_line(left, y0 + lane_h / 2, right, y0 + lane_h / 2, fill=ui.LINE)
            canvas.create_text(left - 8, y0 + lane_h / 2, text=lane, anchor="e", fill=ui.CYAN,
                               font=(ui.MONO, 8, "bold"))
        lane_index = {lane: index for index, lane in enumerate(lanes)}
        for event in sorted(events, key=lambda item: item.captured_at_unix or 0):
            kind, _text = signal_verdict(event)
            x = x_of(event.captured_at_unix or 0)
            y = top + (lane_index[lane_of(event)] + 0.5) * lane_h
            radius = dot_radius(event)
            if kind == "noise":
                canvas.create_oval(x - radius, y - radius, x + radius, y + radius, outline=ui.DIM, width=1)
            else:
                color = ui.CYAN if kind == "nfc" else neon(event.rssi_max_dbm)
                canvas.create_oval(x - radius - 2, y - radius - 2, x + radius + 2, y + radius + 2,
                                   outline=color, width=1)
                canvas.create_oval(x - radius, y - radius, x + radius, y + radius, fill=color, outline="")
            self._dots.append((x, y, event.event_id))
            if event.event_id == self._selected_event:
                canvas.create_oval(x - radius - 6, y - radius - 6, x + radius + 6, y + radius + 6,
                                   outline=ui.YELLOW, width=2)

    def _draw_pulses(self):
        canvas = self.pulses
        canvas.delete("all")
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())
        event = self._event(self._selected_event)
        if event is None:
            return
        if event.source_type == "nfc":
            canvas.create_text(width // 2, height // 2, text="an NFC field has no pulses to show",
                               fill=ui.DIM, font=(ui.MONO, 9))
            return
        timings = [max(0, int(value)) for value in event.pulse_timings_us]
        total = sum(timings)
        if len(timings) < 2 or total <= 0:
            canvas.create_text(width // 2, height // 2 - 8, text="NOTHING RECORDED", fill=ui.YELLOW,
                               font=(ui.MONO, 11, "bold"))
            canvas.create_text(width // 2, height // 2 + 12, text=f"{len(timings)} edge(s): no pulse train to draw",
                               fill=ui.DIM, font=(ui.MONO, 8))
            return
        left, right, high, low = 10, width - 10, 18, height - 26
        x, level = float(left), True
        points = [left, low]
        for duration in timings:
            y = high if level else low
            points += [x, y]
            x += duration * (right - left) / total
            points += [x, y]
            level = not level
        points += [x, low]
        canvas.create_line(*points, fill="#0d4a52", width=4)        # glow
        canvas.create_line(*points, fill=ui.CYAN, width=1)
        canvas.create_text(left, 8, anchor="w", text=f"{len(timings)} edges · {format_duration_us(total)}",
                           fill=ui.TEXT, font=(ui.MONO, 8, "bold"))
        shortest = min(value for value in timings if value > 0)
        canvas.create_text(right, 8, anchor="e", text=f"shortest {shortest} µs", fill=ui.DIM,
                           font=(ui.MONO, 8))
        canvas.create_text(left, height - 10, anchor="w", text="0", fill=ui.DIM, font=(ui.MONO, 8))
        canvas.create_text(right, height - 10, anchor="e", text=format_duration_us(total), fill=ui.DIM,
                           font=(ui.MONO, 8))

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
            segments += [(f"{key:<11}", "muted"), (f"{value}\n", None)]
        return segments

    def show_event(self, event):
        """Fill the right column for one capture (None clears it)."""
        self.similar.delete(0, "end")
        self._similar_rows = []
        self.note_entry.delete(0, "end")
        if event is None:
            self._write(self.details, [("NOTHING SELECTED\n", "title"),
                                       ("pick a capture in the list or a dot in the chart", "muted")])
            self._draw_pulses()
            return
        project = self.project
        kind, verdict = signal_verdict(event)
        when = format_local(event.captured_at_unix, full=True)
        if event.source_type == "nfc":
            title = f"NFC FIELD  ·  {format_local(event.captured_at_unix)}\n"
            rows = [("WHEN", when),
                    ("FIELD", f"{format_duration_us(event.nfc_field_duration_ms * 1000)} · "
                              f"{event.nfc_field_count} time(s)"),
                    ("TYPE", f"{event.nfc_technology or 'unknown'} / {event.nfc_protocol or 'unknown'}")]
        else:
            title = f"{event.frequency_hz / 1e6:.2f} MHz  ·  {format_local(event.captured_at_unix)}\n"
            group = self._groups.get(project.family_key(event), "")
            seen = sum(1 for other in self._events_all() if project.family_key(other) == project.family_key(event))
            rows = [("WHEN", when),
                    ("STRENGTH", f"{strength_bar(event.rssi_max_dbm)}  {event.rssi_max_dbm:.0f} dBm "
                                 f"(avg {event.rssi_avg_dbm:.0f})"),
                    ("LENGTH", format_duration_us(event.duration_us)),
                    ("EDGES", f"{len(event.pulse_timings_us)} recorded"),
                    ("GROUP", "—" if kind == "noise" else f"{group} · seen {seen}×")]
        segments = [(title, "title")]
        segments += self._pairs(rows)
        segments += [("\n" + VERDICT_TEXT[kind].upper() + "\n", {"signal": "good", "noise": "warn",
                                                                  "nfc": "head"}[kind]),
                     (verdict + "\n", None),
                     (f"\n{event.event_id}", "muted")]
        self._write(self.details, segments)
        self._draw_pulses()
        if kind != "noise":
            pool = [other for other in self._events_all() if signal_verdict(other)[0] == kind]
            for row in project.similar_events(event, limit=4, candidates=pool):
                other = row["event"]
                comparison = row["comparison"]
                self._similar_rows.append(other.event_id)
                self.similar.insert("end", f"{comparison.get('percent', 0):>3}%  {format_local(other.captured_at_unix)}"
                                           f"  {comparison.get('relationship_text', '')}")
        if not self._similar_rows:
            self.similar.insert("end", "nothing similar" if kind != "noise" else "noise is not compared")
        note = project.note(event.event_id)
        self.note_entry.insert(0, note.get("text", ""))

    def _events_all(self):
        return self.project.events.values()

    # ------------------------------------------------------------------ actions
    def save_note(self):
        if self._selected_event:
            self.project.add_note(self._selected_event, self.note_entry.get(), "")


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
