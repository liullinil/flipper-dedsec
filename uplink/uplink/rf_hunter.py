"""Portable RF Signal Hunter event model and reliable desktop import store.

This module deliberately contains no radio or transmit calls.  The Flipper
adapter supplies passive observations; the desktop side owns durable metadata,
fingerprint grouping, and idempotent upload acknowledgement.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterator, Optional


_SAFE_EVENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def validate_event_id(value: str) -> str:
    """Validate the identifier before it can become a filesystem path."""
    value = str(value)
    if not _SAFE_EVENT_ID.fullmatch(value):
        raise ValueError("unsafe RF event id")
    return value


def validate_capture_blob(value: str, root: str = "") -> str:
    """Validate a capture path before it is used as a filesystem path.

    Event metadata can arrive from a removable Flipper SD card or over BLE;
    the path is therefore untrusted even though the event ID itself is safe.
    Relative paths below the store root are accepted, while absolute paths and
    ``..`` escapes are rejected.  Returning the original spelling keeps old
    stores readable without allowing a path traversal.
    """
    value = str(value or "")
    if not value:
        return ""
    if os.path.isabs(value) or (os.path.altsep and os.path.isabs(value.replace("/", os.path.altsep))):
        raise ValueError("unsafe RF capture path")
    # Normalize both slash conventions because Flipper records use '/'
    # while the desktop may run on Windows.
    normalized = os.path.normpath(value.replace("/", os.sep))
    if normalized in (".", "") or normalized == os.pardir or normalized.startswith(os.pardir + os.sep):
        raise ValueError("unsafe RF capture path")
    if root:
        root_abs = os.path.abspath(os.fspath(root))
        candidate = os.path.abspath(os.path.join(root_abs, normalized))
        try:
            common = os.path.commonpath((root_abs, candidate))
        except ValueError:
            common = ""
        if os.path.normcase(common) != os.path.normcase(root_abs):
            raise ValueError("unsafe RF capture path")
    return normalized.replace(os.sep, "/")


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
    # NFC field observations are intentionally metadata-only.  The Flipper
    # field detector reports carrier presence without polling or transmitting,
    # so a record can describe the technology source even when no UID or raw
    # frame was available.
    nfc_technology: str = ""
    nfc_protocol: str = ""
    nfc_identifier: str = ""
    nfc_field_duration_ms: int = 0
    nfc_field_count: int = 0
    nfc_confidence: float = 0.0
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
    profile_id: str = ""
    follow_profile_id: str = ""
    follow_similarity: float = 0.0
    capture_blob: str = ""
    upload_state: str = "pending"
    timezone_offset_minutes: int = 0
    captured_at_unix: Optional[float] = None
    rtc_local_unix: Optional[int] = None
    pulse_timings_us: tuple = field(default_factory=tuple)
    event_id: str = ""

    def __post_init__(self):
        if not self.event_id:
            self.event_id = event_id(self.device_uuid, self.session_id, self.sequence_number)
        self.event_id = validate_event_id(self.event_id)
        self.pulse_timings_us = tuple(int(x) for x in self.pulse_timings_us)
        self.follow_similarity = float(self.follow_similarity)
        if not 0.0 <= self.follow_similarity <= 1.0:
            raise ValueError("follow similarity must be between 0 and 1")
        self.timezone_offset_minutes = int(self.timezone_offset_minutes)
        if not -14 * 60 <= self.timezone_offset_minutes <= 14 * 60:
            raise ValueError("timezone offset is outside the supported UTC±14:00 range")
        # Normalize once at the import boundary; selected display timezone is
        # independent of the offset recorded by the capture device.
        instant = datetime.fromisoformat(str(self.captured_at_utc).replace("Z", "+00:00"))
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone(timedelta(minutes=self.timezone_offset_minutes)))
        instant = instant.astimezone(timezone.utc)
        self.captured_at_utc = instant.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        if self.captured_at_unix is None:
            self.captured_at_unix = instant.timestamp()
        else:
            self.captured_at_unix = float(self.captured_at_unix)
            if abs(self.captured_at_unix - instant.timestamp()) >= 1:
                raise ValueError("UTC calendar timestamp and epoch disagree")
        if self.rtc_local_unix is not None:
            self.rtc_local_unix = int(self.rtc_local_unix)

    def to_dict(self):
        data = asdict(self)
        data["pulse_timings_us"] = list(self.pulse_timings_us)
        return data

    @classmethod
    def from_dict(cls, data):
        data = dict(data)
        # Accept the compact Flipper JSONL shape as well as the desktop schema.
        if not data.get("device_uuid") and data.get("device_id"):
            data["device_uuid"] = data["device_id"]
        if not data.get("captured_at_utc") and data.get("captured_at"):
            data["captured_at_utc"] = data["captured_at"]
        if not data.get("captured_at_utc") and data.get("captured_at_unix") is not None:
            data["captured_at_utc"] = datetime.fromtimestamp(
                float(data["captured_at_unix"]), tz=timezone.utc).isoformat()
        if "rssi_dbm" in data:
            value = float(data.get("rssi_dbm") or 0)
            data.setdefault("rssi_min_dbm", value)
            data.setdefault("rssi_avg_dbm", value)
            data.setdefault("rssi_max_dbm", value)
        if not data.get("duration_us") and data.get("last_duration_us"):
            data["duration_us"] = int(data["last_duration_us"])
        if not data.get("source_type"):
            data["source_type"] = "nfc" if data.get("mode") == "NFC" else "subghz"
        # Accept the compact FAP names while keeping a stable desktop schema.
        if not data.get("nfc_technology") and data.get("technology"):
            data["nfc_technology"] = data["technology"]
        if not data.get("nfc_protocol") and data.get("protocol"):
            data["nfc_protocol"] = data["protocol"]
        if not data.get("nfc_field_duration_ms") and data.get("field_duration_ms"):
            data["nfc_field_duration_ms"] = int(data["field_duration_ms"])
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

    @staticmethod
    def similarity(left: RfEvent, right: RfEvent) -> dict:
        reasons = []
        score = 0.0
        if left.source_type == right.source_type:
            score += 0.10
            reasons.append("same source type")
        if left.modulation == right.modulation:
            score += 0.25
            reasons.append("same modulation")
        if left.frequency_hz and right.frequency_hz:
            drift = abs(left.frequency_hz - right.frequency_hz)
            if drift <= 150_000:
                score += 0.25
                reasons.append("same carrier frequency")
        if left.pulse_timings_us and right.pulse_timings_us:
            count = min(len(left.pulse_timings_us), len(right.pulse_timings_us), 96)
            matches = sum(
                abs(left.pulse_timings_us[i] - right.pulse_timings_us[i]) <= 75
                for i in range(count)
            )
            timing = matches / max(1, count)
            score += 0.30 * timing
            if timing >= 0.75:
                reasons.append("same pulse timing")
        if left.repeat_count == right.repeat_count:
            score += 0.10
            reasons.append("same repetition pattern")
        return {"score": round(min(1.0, score), 3), "reasons": reasons}

    def assign(self, event: RfEvent) -> str:
        fp = event.fingerprint_id or fingerprint(event)
        event.fingerprint_id = fp
        for family_id, family in self.families.items():
            representative = family["representative"]
            match = self.similarity(event, representative)
            if match["score"] < 0.55:
                continue
            event.family_id = family_id
            family["event_ids"].append(event.event_id)
            family["fingerprints"].add(fp)
            family["last_seen"] = event.captured_at_utc
            family["confidence"] = max(family["confidence"], match["score"])
            return family_id
        family_id = "family-" + hashlib.sha256(fp.encode("ascii")).hexdigest()[:12]
        self.families[family_id] = {
            "frequency_hz": event.frequency_hz,
            "modulation": event.modulation,
            "event_ids": [event.event_id],
            "fingerprints": {fp},
            "representative": event,
            "confidence": 1.0,
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
        # API-88.9 FAP journals use one immutable JSON record per event under
        # ``events/`` and compact ACK receipts under ``receipts/``.  Keep the
        # older JSONL layout for existing desktop projects, but read both
        # layouts so a copied Flipper journal can be opened directly.
        self.events_dir = os.path.join(self.root, "events")
        self.receipts_dir = os.path.join(self.root, "receipts")
        self._per_event_layout = os.path.isdir(self.events_dir) or os.path.isdir(self.receipts_dir)
        self.events: Dict[str, RfEvent] = {}
        self._load()

    def _load(self):
        self._load_jsonl()
        self._load_per_event_files()

    @staticmethod
    def _record_event(data, filename_id=""):
        """Decode one journal record and bind a missing ID to its filename."""
        if not isinstance(data, dict):
            return None
        data = dict(data)
        if filename_id:
            filename_id = validate_event_id(filename_id)
            if data.get("event_id") and str(data["event_id"]) != filename_id:
                # A filename is the durable identity in the FAP layout.  A
                # mismatch is a corrupt record, never a reason to import an
                # event under a different path.
                return None
            data.setdefault("event_id", filename_id)
        try:
            return RfEvent.from_dict(data)
        except (TypeError, ValueError, KeyError):
            return None

    def _insert_loaded(self, event: RfEvent, *, prefer: bool = False):
        """Insert a decoded record while keeping transport duplicates stable."""
        # A malformed capture path must never become a path read later by the
        # analyzer.  Keep the scalar journal evidence and simply mark its raw
        # payload unavailable.
        if event.capture_blob:
            try:
                event.capture_blob = validate_capture_blob(event.capture_blob, self.root)
            except ValueError:
                event.capture_blob = ""
        previous = self.events.get(event.event_id)
        if previous is None or prefer:
            self.events[event.event_id] = event
            return
        # A copied FAP journal and a desktop JSONL export can contain the same
        # event.  Prefer the representation that still has a usable capture;
        # otherwise retain the first deterministic metadata record.
        previous_has_blob = bool(previous.capture_blob)
        incoming_has_blob = bool(event.capture_blob)
        if incoming_has_blob and not previous_has_blob:
            self.events[event.event_id] = event
        elif previous.upload_state != "uploaded" and event.upload_state == "uploaded":
            previous.upload_state = "uploaded"

    def _load_jsonl(self):
        try:
            with open(self.events_path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        event = RfEvent.from_dict(json.loads(line))
                    except (TypeError, ValueError, KeyError):
                        continue
                    self._insert_loaded(event)
        except OSError:
            return

    def _load_per_event_files(self):
        """Load API-88.9 event files and ACK receipts.

        ACK receipts are compact index records.  When the raw event was
        reclaimed, the receipt is the only remaining evidence and is still a
        valid, already-uploaded desktop observation.
        """
        try:
            names = sorted(os.listdir(self.events_dir))
        except OSError:
            names = []
        for name in names:
            if not name.lower().endswith(".json"):
                continue
            try:
                event_id_value = validate_event_id(name[:-5])
            except ValueError:
                continue
            path = os.path.join(self.events_dir, name)
            try:
                with open(path, encoding="utf-8") as fh:
                    event = self._record_event(json.load(fh), event_id_value)
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if event is not None:
                self._insert_loaded(event)

        try:
            names = sorted(os.listdir(self.receipts_dir))
        except OSError:
            names = []
        for name in names:
            if not name.lower().endswith(".ack"):
                continue
            try:
                event_id_value = validate_event_id(name[:-4])
            except ValueError:
                continue
            path = os.path.join(self.receipts_dir, name)
            try:
                with open(path, encoding="utf-8") as fh:
                    event = self._record_event(json.load(fh), event_id_value)
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if event is None:
                continue
            event.upload_state = "uploaded"
            current = self.events.get(event.event_id)
            if current is None:
                self._insert_loaded(event)
            else:
                # The event JSON can retain richer pulse data under the KEEP
                # policy.  Merge only transport state from the receipt.
                current.upload_state = "uploaded"

    def _flush(self):
        os.makedirs(self.root, exist_ok=True)
        if self._per_event_layout:
            # Preserve the FAP's per-event journal shape when a caller opens a
            # copied SD-card directory as its EventStore.  Files are replaced
            # atomically one at a time; an interrupted replacement leaves the
            # previous immutable record readable.
            os.makedirs(self.events_dir, exist_ok=True)
            for event in self.events.values():
                try:
                    event_id_value = validate_event_id(event.event_id)
                except ValueError:
                    continue
                path = os.path.join(self.events_dir, event_id_value + ".json")
                fd, tmp = tempfile.mkstemp(prefix=event_id_value + ".", suffix=".tmp", dir=self.events_dir)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        fh.write(json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":")))
                        fh.flush()
                        os.fsync(fh.fileno())
                    os.replace(tmp, path)
                finally:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
            return
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
        if not isinstance(event, RfEvent):
            event = RfEvent.from_dict(event)
        if event.capture_blob:
            event.capture_blob = validate_capture_blob(event.capture_blob, self.root)
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
            path = validate_capture_blob(event.capture_blob, self.root)
            return os.path.getsize(os.path.join(self.root, path))
        except (OSError, ValueError):
            return 0

    def read_capture(self, event_or_id) -> bytes:
        """Read a raw capture only after validating its store-relative path."""
        event_id_value = event_or_id if isinstance(event_or_id, str) else event_or_id.event_id
        event = self.events.get(str(event_id_value))
        if event is None or not event.capture_blob:
            return b""
        try:
            path = validate_capture_blob(event.capture_blob, self.root)
            with open(os.path.join(self.root, path), "rb") as fh:
                return fh.read()
        except (OSError, ValueError):
            return b""

    def chunks(self, event_id_value, chunk_size=192) -> Iterator[tuple]:
        event = self.events[event_id_value]
        if not event.capture_blob:
            yield (0, b"")
            return
        path = validate_capture_blob(event.capture_blob, self.root)
        with open(os.path.join(self.root, path), "rb") as fh:
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
                path = validate_capture_blob(event.capture_blob, self.root)
                os.remove(os.path.join(self.root, path))
            except (OSError, ValueError):
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
