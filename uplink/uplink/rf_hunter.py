"""Portable RF Signal Hunter event model and reliable desktop import store.

This module deliberately contains no radio or transmit calls.  The Flipper
adapter supplies passive observations; the desktop side owns durable metadata,
fingerprint grouping, and idempotent upload acknowledgement.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterator, Optional


def event_id(device_uuid: str, session_id: str, sequence_number: int) -> str:
    raw = f"{device_uuid}\0{session_id}\0{int(sequence_number)}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class RfEvent:
    device_uuid: str
    session_id: str
    sequence_number: int
    captured_at_utc: str
    monotonic_ms: int
    source_type: str = "subghz"
    frequency_hz: int = 0
    modulation: str = "unknown"
    bandwidth_hz: int = 0
    rssi_min_dbm: float = 0.0
    rssi_avg_dbm: float = 0.0
    rssi_max_dbm: float = 0.0
    duration_us: int = 0
    repeat_count: int = 1
    fingerprint_id: str = ""
    family_id: Optional[str] = None
    classification: str = "unknown"
    classification_confidence: float = 0.0
    capture_blob: str = ""
    upload_state: str = "pending"
    timezone_offset_minutes: int = 0
    pulse_timings_us: tuple = field(default_factory=tuple)
    event_id: str = ""

    def __post_init__(self):
        if not self.event_id:
            self.event_id = event_id(self.device_uuid, self.session_id, self.sequence_number)
        self.pulse_timings_us = tuple(int(x) for x in self.pulse_timings_us)

    def to_dict(self):
        data = asdict(self)
        data["pulse_timings_us"] = list(self.pulse_timings_us)
        return data

    @classmethod
    def from_dict(cls, data):
        data = dict(data)
        event = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        return event


def fingerprint(event: RfEvent) -> str:
    """Build a stable structural fingerprint, ignoring payload bytes."""
    pulses = event.pulse_timings_us
    if pulses:
        # Quantize timings to tolerate clock and radio drift while retaining shape.
        shape = ",".join(str(max(1, int(round(p / 25.0) * 25))) for p in pulses[:96])
    else:
        shape = ""
    freq = int(round(event.frequency_hz / 1000.0) * 1000) if event.frequency_hz else 0
    raw = f"{freq}|{event.modulation}|{event.bandwidth_hz // 1000}|{shape}|{event.repeat_count}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


class FamilyGrouper:
    """Small provisional grouping engine; desktop history remains authoritative."""

    def __init__(self, frequency_tolerance_hz=150_000):
        self.frequency_tolerance_hz = frequency_tolerance_hz
        self.families: Dict[str, dict] = {}

    def assign(self, event: RfEvent) -> str:
        fp = event.fingerprint_id or fingerprint(event)
        event.fingerprint_id = fp
        for family_id, family in self.families.items():
            if event.modulation != family["modulation"]:
                continue
            if abs(event.frequency_hz - family["frequency_hz"]) > self.frequency_tolerance_hz:
                continue
            event.family_id = family_id
            family["event_ids"].append(event.event_id)
            family["fingerprints"].add(fp)
            family["last_seen"] = event.captured_at_utc
            return family_id
        family_id = "family-" + hashlib.sha256(fp.encode("ascii")).hexdigest()[:12]
        self.families[family_id] = {
            "frequency_hz": event.frequency_hz,
            "modulation": event.modulation,
            "event_ids": [event.event_id],
            "fingerprints": {fp},
            "first_seen": event.captured_at_utc,
            "last_seen": event.captured_at_utc,
        }
        event.family_id = family_id
        return family_id


class EventStore:
    """JSONL metadata plus capture blobs with safe post-ACK reclamation."""

    def __init__(self, root):
        self.root = os.fspath(root)
        self.capture_dir = os.path.join(self.root, "captures")
        self.events_path = os.path.join(self.root, "events.jsonl")
        self.events: Dict[str, RfEvent] = {}
        self._load()

    def _load(self):
        try:
            with open(self.events_path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        event = RfEvent.from_dict(json.loads(line))
                    except (TypeError, ValueError, KeyError):
                        continue
                    self.events[event.event_id] = event
        except OSError:
            return

    def _flush(self):
        os.makedirs(self.root, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="events-", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for event in self.events.values():
                    fh.write(json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n")
            os.replace(tmp, self.events_path)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def add(self, event: RfEvent, capture=b""):
        """Insert idempotently; a repeated event ID never creates a second event."""
        existing = self.events.get(event.event_id)
        if existing:
            if capture and not existing.capture_blob:
                os.makedirs(self.capture_dir, exist_ok=True)
                existing.capture_blob = f"captures/{existing.event_id}.bin"
                with open(os.path.join(self.root, existing.capture_blob), "wb") as fh:
                    fh.write(capture)
                self._flush()
            return existing
        os.makedirs(self.capture_dir, exist_ok=True)
        if capture:
            event.capture_blob = event.capture_blob or f"captures/{event.event_id}.bin"
            path = os.path.join(self.root, event.capture_blob)
            with open(path, "wb") as fh:
                fh.write(capture)
        self.events[event.event_id] = event
        self._flush()
        return event

    def pending(self) -> list:
        return [event for event in self.events.values() if event.upload_state == "pending"]

    def manifest(self) -> list:
        return [
            {"event_id": event.event_id, "size": self._capture_size(event), "metadata": event.to_dict()}
            for event in self.pending()
        ]

    def _capture_size(self, event):
        if not event.capture_blob:
            return 0
        try:
            return os.path.getsize(os.path.join(self.root, event.capture_blob))
        except OSError:
            return 0

    def chunks(self, event_id_value, chunk_size=192) -> Iterator[tuple]:
        event = self.events[event_id_value]
        if not event.capture_blob:
            yield (0, b"")
            return
        with open(os.path.join(self.root, event.capture_blob), "rb") as fh:
            offset = 0
            while True:
                data = fh.read(chunk_size)
                if not data:
                    break
                yield offset, data
                offset += len(data)

    def acknowledge(self, event_id_value):
        event = self.events.get(event_id_value)
        if event is None:
            return False
        event.upload_state = "uploaded"
        if event.capture_blob:
            try:
                os.remove(os.path.join(self.root, event.capture_blob))
            except OSError:
                pass
        self._flush()
        return True


class UploadReceiver:
    """Validate an interrupted upload before committing it to an EventStore."""

    def __init__(self, store: EventStore):
        self.store = store
        self._metadata: Dict[str, RfEvent] = {}
        self._buffers: Dict[str, bytearray] = {}

    def begin(self, metadata: dict):
        event = RfEvent.from_dict(metadata)
        self._metadata[event.event_id] = event
        self._buffers.setdefault(event.event_id, bytearray())
        return event.event_id

    def receive_chunk(self, event_id_value: str, offset: int, data: bytes):
        if event_id_value not in self._metadata:
            raise KeyError(event_id_value)
        buffer = self._buffers[event_id_value]
        if int(offset) != len(buffer):
            raise ValueError("upload offset mismatch")
        buffer.extend(data)
        return len(buffer)

    def commit(self, event_id_value: str, sha256_hex: Optional[str] = None):
        event = self._metadata[event_id_value]
        payload = bytes(self._buffers[event_id_value])
        if sha256_hex and hashlib.sha256(payload).hexdigest() != sha256_hex:
            raise ValueError("capture checksum mismatch")
        self.store.add(event, payload)
        del self._metadata[event_id_value]
        del self._buffers[event_id_value]
        return event.event_id
