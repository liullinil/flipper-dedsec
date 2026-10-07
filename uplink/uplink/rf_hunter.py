"""Portable RF Signal Hunter event model and durable desktop event store.

This module deliberately contains no radio or transmit calls.  The Flipper
(the RF tab of the DedSec Uplink app) supplies passive observations; the
companion owns durable metadata, fingerprint grouping and the import that lets
the Flipper reclaim a record.  Because an import acknowledgement allows the
Flipper to delete its copy, every write in :class:`EventStore` is flushed and
fsynced before the call returns.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

log = logging.getLogger("uplink.rf_hunter")

_SAFE_EVENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
MAX_LOAD_ERRORS = 20


def app_data_dir() -> str:
    """Per-user companion folder (``%LOCALAPPDATA%\\DedSecUplink``)."""
    return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "DedSecUplink")


def default_store_root(create: bool = False) -> str:
    """The companion's RF event store: ``%LOCALAPPDATA%\\DedSecUplink\\rf_hunter``."""
    path = os.path.join(app_data_dir(), "rf_hunter")
    if create:
        os.makedirs(path, exist_ok=True)
    return path


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
        root_abs = os.path.realpath(os.path.abspath(os.fspath(root)))
        candidate = os.path.realpath(os.path.join(root_abs, normalized))
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


_TEXT_FIELDS = (
    ("device_uuid", ""), ("session_id", ""), ("source_type", "subghz"), ("nfc_technology", ""),
    ("nfc_protocol", ""), ("nfc_identifier", ""), ("modulation", "unknown"), ("firmware_version", ""),
    ("fingerprint_id", ""), ("classification", "unknown"), ("profile_id", ""),
    ("follow_profile_id", ""), ("capture_blob", ""), ("upload_state", "pending"), ("event_id", ""),
)
_INT_FIELDS = (("sequence_number", 0), ("monotonic_ms", 0), ("nfc_field_duration_ms", 0),
               ("nfc_field_count", 0), ("frequency_hz", 0), ("bandwidth_hz", 0), ("battery_pct", 0),
               ("duration_us", 0), ("repeat_count", 1))
_FLOAT_FIELDS = ("nfc_confidence", "rssi_min_dbm", "rssi_avg_dbm", "rssi_max_dbm",
                 "classification_confidence")


def _as_float(value, name: str) -> float:
    result = float(0.0 if value is None or value == "" else value)
    if not math.isfinite(result):
        raise ValueError(f"{name} is not a finite number")
    return result


def _as_int(value, name: str) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return int(_as_float(value, name))


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
    battery_pct: int = 0
    firmware_version: str = ""
    rssi_min_dbm: float = 0.0
    rssi_avg_dbm: float = 0.0
    rssi_max_dbm: float = 0.0
    duration_us: int = 0
    repeat_count: int = 1
    # ``fingerprint_id``/``family_id`` are stored evidence from the Flipper
    # (for example its ``local-...`` hint).  Desktop grouping keeps its own
    # results in memory and never writes them back into these fields.
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
    schema_version: int = 1

    def __post_init__(self):
        # Records come from an SD card or over BLE: coerce scalar fields to
        # their declared types here so a malformed record is rejected (and
        # counted by the store) instead of breaking a later view.
        for name, default in _TEXT_FIELDS:
            value = getattr(self, name)
            setattr(self, name, default if value is None or value == "" else str(value))
        for name, default in _INT_FIELDS:
            value = getattr(self, name)
            setattr(self, name, default if value is None else _as_int(value, name))
        for name in _FLOAT_FIELDS:
            setattr(self, name, _as_float(getattr(self, name), name))
        if self.family_id is not None:
            self.family_id = str(self.family_id)
        if not self.event_id:
            self.event_id = event_id(self.device_uuid, self.session_id, self.sequence_number)
        self.event_id = validate_event_id(self.event_id)
        self.schema_version = int(self.schema_version or 1)
        if isinstance(self.pulse_timings_us, (str, bytes, dict)):
            raise ValueError("pulse_timings_us must be a list of integers")
        self.pulse_timings_us = tuple(int(x) for x in (self.pulse_timings_us or ()))
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
            self.captured_at_unix = _as_float(self.captured_at_unix, "captured_at_unix")
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
        if not isinstance(data, dict):
            raise ValueError("RF record is not a JSON object")
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
    """Legacy provisional grouping engine (the analyzer uses rf_fingerprint)."""

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


# --------------------------------------------------------------------------- durability
def _fsync_dir(path: str) -> None:
    """Persist directory entries (a rename or a new file) on POSIX.

    Windows offers no directory handle that ``os.fsync`` accepts; there
    :func:`_replace` requests a write-through rename (``MOVEFILE_WRITE_THROUGH``)
    and NTFS journals the metadata change.
    """
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


if os.name == "nt":  # pragma: no cover - exercised on Windows only
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _MoveFileExW = _kernel32.MoveFileExW
    _MoveFileExW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    _MoveFileExW.restype = wintypes.BOOL
    _MOVEFILE_REPLACE_EXISTING = 0x1
    _MOVEFILE_WRITE_THROUGH = 0x8

    def _replace(src: str, dst: str) -> None:
        """Atomic rename that returns only after the change reached the disk.

        A reader (for example the analyzer reloading the folder) can hold the
        target open for a moment; Windows reports that as a sharing violation,
        so retry briefly before giving up.
        """
        flags = _MOVEFILE_REPLACE_EXISTING | _MOVEFILE_WRITE_THROUGH
        for attempt in range(10):
            if _MoveFileExW(os.fspath(src), os.fspath(dst), flags):
                return
            error = ctypes.get_last_error()
            if error not in (5, 32, 33) or attempt == 9:  # access denied / sharing / lock violation
                raise ctypes.WinError(error)
            time.sleep(0.05)
else:
    def _replace(src: str, dst: str) -> None:
        os.replace(src, dst)
        _fsync_dir(os.path.dirname(os.path.abspath(dst)))


def _write_durable(path: str, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically: temp file, fsync, durable rename."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        _replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _dump_record(event: RfEvent) -> str:
    return json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":"))


def resolve_store_root(path) -> str:
    """Map a folder the user picked to the event-store root inside it.

    Accepted: the store root itself (``events.jsonl``, ``events/``,
    ``uploaded/`` or ``receipts/``), one of those journal sub-folders, the
    Flipper app-data folder ``apps_data/dedsec_uplink`` (journal in ``rf/``),
    an SD-card root, and the former standalone app folders
    (``apps_data/rf_signal_hunter[/rf_signal_hunter]``).
    """
    path = os.path.abspath(os.fspath(path))

    def is_store(candidate: str) -> bool:
        return (os.path.isfile(os.path.join(candidate, "events.jsonl"))
                or any(os.path.isdir(os.path.join(candidate, name))
                       for name in ("events", "uploaded", "receipts")))

    if is_store(path):
        return path
    parent = os.path.dirname(path)
    if os.path.basename(path).lower() in ("events", "uploaded", "receipts") and parent:
        return parent
    for relative in ("rf",
                     os.path.join("dedsec_uplink", "rf"),
                     os.path.join("apps_data", "dedsec_uplink", "rf"),
                     "rf_signal_hunter",
                     os.path.join("rf_signal_hunter", "rf_signal_hunter"),
                     os.path.join("apps_data", "rf_signal_hunter", "rf_signal_hunter"),
                     os.path.join("apps_data", "rf_signal_hunter")):
        candidate = os.path.join(path, relative)
        if is_store(candidate):
            return candidate
    return path


class EventStore:
    """RF observations plus raw capture blobs, written durably.

    Two on-disk layouts are read:

    * the companion layout: ``events.jsonl`` (one record per line) plus
      ``captures/<event_id>.bin`` holding the exact bytes received from the
      Flipper;
    * a copied Flipper journal: ``events/<id>.json`` (pending),
      ``uploaded/<id>.json`` (kept after an ACK) and the former
      ``receipts/<id>.ack`` compact receipts.

    A malformed record never prevents the store from opening: it is counted in
    :attr:`skipped_records` (details in :attr:`load_errors`).  Only records
    that changed are written (dirty tracking), and every write is fsynced
    before the call returns, so a caller may acknowledge the import right
    after :meth:`add` or :meth:`attach_capture` succeeds.
    """

    def __init__(self, root, read_only: bool = False):
        self.root = os.fspath(root)
        self.read_only = bool(read_only)
        self.capture_dir = os.path.join(self.root, "captures")
        self.events_path = os.path.join(self.root, "events.jsonl")
        self.events_dir = os.path.join(self.root, "events")
        self.receipts_dir = os.path.join(self.root, "receipts")
        self.uploaded_dir = os.path.join(self.root, "uploaded")
        self._per_event_layout = any(os.path.isdir(path) for path in
                                     (self.events_dir, self.receipts_dir, self.uploaded_dir))
        self.events: Dict[str, RfEvent] = {}
        self.skipped_records = 0
        self.load_errors: List[str] = []
        self._dirty: set = set()
        self._in_jsonl: set = set()
        self._jsonl_preserved: List[bytes] = []
        self._jsonl_terminated = True
        self._paths: Dict[str, str] = {}
        self._load()

    # ------------------------------------------------------------------ loading
    def _skip(self, where: str, error) -> None:
        self.skipped_records += 1
        if len(self.load_errors) < MAX_LOAD_ERRORS:
            self.load_errors.append(f"{where}: {error}")
        log.warning("skipped malformed RF record %s: %s", where, error)

    def _load(self):
        self._load_jsonl()
        self._load_per_event_files()

    @staticmethod
    def _record_event(data, filename_id=""):
        """Decode one journal record and bind a missing ID to its filename."""
        if not isinstance(data, dict):
            raise ValueError("record is not a JSON object")
        data = dict(data)
        if filename_id:
            filename_id = validate_event_id(filename_id)
            if data.get("event_id") and str(data["event_id"]) != filename_id:
                # A filename is the durable identity in the FAP layout.  A
                # mismatch is a corrupt record, never a reason to import an
                # event under a different path.
                raise ValueError("event_id does not match the file name")
            data.setdefault("event_id", filename_id)
        return RfEvent.from_dict(data)

    def _insert_loaded(self, event: RfEvent, path: str = ""):
        """Insert a decoded record while keeping transport duplicates stable."""
        # A malformed capture path must never become a path read later by the
        # analyzer.  Keep the scalar journal evidence and simply mark its raw
        # payload unavailable.  Absolute paths and ".." escapes are what an
        # untrusted record can express; resolving symlinks (realpath, about a
        # millisecond per call on Windows) is left to explicit add() paths.
        if event.capture_blob:
            try:
                event.capture_blob = validate_capture_blob(event.capture_blob)
            except ValueError:
                event.capture_blob = ""
        previous = self.events.get(event.event_id)
        if previous is None:
            self.events[event.event_id] = event
            if path:
                self._paths[event.event_id] = path
            return
        # A copied FAP journal and a desktop JSONL export can contain the same
        # event.  Prefer the representation that still has a usable capture;
        # otherwise retain the first deterministic metadata record.
        if event.capture_blob and not previous.capture_blob:
            if previous.upload_state == "uploaded":
                event.upload_state = "uploaded"
            self.events[event.event_id] = event
            if path:
                self._paths[event.event_id] = path
        elif previous.upload_state != "uploaded" and event.upload_state == "uploaded":
            previous.upload_state = "uploaded"

    def _load_jsonl(self):
        try:
            with open(self.events_path, "rb") as fh:
                data = fh.read()
        except FileNotFoundError:
            return
        except OSError as exc:
            self._skip("events.jsonl", exc)
            return
        self._jsonl_terminated = not data or data.endswith(b"\n")
        lines = data.split(b"\n")
        tail = lines.pop()
        for number, raw in enumerate(lines, 1):
            if not raw.strip():
                continue
            try:
                event = RfEvent.from_dict(json.loads(raw.decode("utf-8")))
            except Exception as exc:  # one bad line must not hide the rest
                self._skip(f"events.jsonl line {number}", exc)
                # Keep the bytes: a later rewrite must not silently drop evidence.
                self._jsonl_preserved.append(raw.rstrip(b"\r"))
                continue
            self._insert_loaded(event)
            self._in_jsonl.add(event.event_id)
        if tail.strip():
            # A record without its newline is either being appended right now
            # or was torn by a crash; it is not committed.  Accept it only if
            # it is complete JSON (for example a hand-edited file).
            try:
                event = RfEvent.from_dict(json.loads(tail.decode("utf-8")))
            except Exception:
                return
            self._insert_loaded(event)
            self._in_jsonl.add(event.event_id)

    def _load_dir(self, directory: str, suffix: str, uploaded: bool):
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            return
        for name in names:
            if not name.lower().endswith(suffix):
                continue
            path = os.path.join(directory, name)
            where = f"{os.path.basename(directory)}/{name}"
            try:
                with open(path, encoding="utf-8") as fh:
                    event = self._record_event(json.load(fh), name[:-len(suffix)])
            except Exception as exc:
                self._skip(where, exc)
                continue
            if uploaded:
                event.upload_state = "uploaded"
                current = self.events.get(event.event_id)
                if current is not None:
                    # The event JSON can retain richer pulse data under the KEEP
                    # policy.  Merge only transport state from the receipt.
                    current.upload_state = "uploaded"
                    if not (event.capture_blob and not current.capture_blob):
                        continue
            self._insert_loaded(event, path)

    def _load_per_event_files(self):
        """Load a copied Flipper journal.

        ``uploaded/`` holds full records the Flipper kept after an ACK and
        ``receipts/`` the compact receipts of the former standalone app; both
        are already-uploaded observations.
        """
        self._load_dir(self.events_dir, ".json", uploaded=False)
        self._load_dir(self.uploaded_dir, ".json", uploaded=True)
        self._load_dir(self.receipts_dir, ".ack", uploaded=True)

    # ------------------------------------------------------------------ writing
    def _check_writable(self):
        if self.read_only:
            raise PermissionError(f"RF event store {self.root} is open read-only")

    def mark_dirty(self, event_id_value: str) -> None:
        """Schedule one changed record for the next :meth:`_flush`."""
        if event_id_value in self.events:
            self._dirty.add(event_id_value)

    def _flush(self):
        """Durably write the records that changed since the last flush."""
        if not self._dirty:
            return
        self._check_writable()
        os.makedirs(self.root, exist_ok=True)
        if self._per_event_layout:
            self._flush_per_event()
        else:
            self._flush_jsonl()

    def _flush_per_event(self):
        # Preserve the journal shape when a copied directory is opened
        # writable.  Each record is replaced atomically; untouched records are
        # never rewritten, so their bytes stay identical to the Flipper's.
        for event_id_value in [eid for eid in self.events if eid in self._dirty]:
            event = self.events[event_id_value]
            path = self._paths.get(event_id_value, "")
            if not path.lower().endswith(".json"):
                path = os.path.join(self.events_dir, validate_event_id(event_id_value) + ".json")
            _write_durable(path, _dump_record(event).encode("utf-8"))
            self._paths[event_id_value] = path
            self._dirty.discard(event_id_value)
        self._dirty.clear()

    def _flush_jsonl(self):
        dirty = [eid for eid in self.events if eid in self._dirty]
        appendable = (os.path.exists(self.events_path)
                      and all(eid not in self._in_jsonl for eid in dirty))
        if appendable:
            self._append_jsonl([self.events[eid] for eid in dirty])
        else:
            self._rewrite_jsonl()
        self._in_jsonl.update(dirty)
        self._dirty.clear()

    def _append_jsonl(self, events):
        data = "".join(_dump_record(event) + "\n" for event in events).encode("utf-8")
        if not self._jsonl_terminated:
            # Terminate a torn line instead of gluing the new record to it.
            # (Known from loading: re-reading the file before every append made
            # the whole process measurably slower on Windows.)
            data = b"\n" + data
        # Until the fsync succeeded the file may end in a partial line (an
        # extra newline later only produces an empty line, which is ignored).
        self._jsonl_terminated = False
        with open(self.events_path, "ab") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        self._jsonl_terminated = True

    def _rewrite_jsonl(self):
        data = b"".join(line + b"\n" for line in self._jsonl_preserved)
        data += "".join(_dump_record(event) + "\n" for event in self.events.values()).encode("utf-8")
        _write_durable(self.events_path, data)
        self._in_jsonl = set(self.events)
        self._jsonl_terminated = True

    def _write_capture(self, event_id_value: str, capture: bytes) -> str:
        relative = f"captures/{validate_event_id(event_id_value)}.bin"
        _write_durable(os.path.join(self.root, relative), bytes(capture))
        return relative

    def add(self, event: RfEvent, capture=b""):
        """Insert durably and idempotently; a repeated event ID never creates a second event."""
        self._check_writable()
        if not isinstance(event, RfEvent):
            event = RfEvent.from_dict(event)
        if event.capture_blob:
            event.capture_blob = validate_capture_blob(event.capture_blob, self.root)
        existing = self.events.get(event.event_id)
        if existing is not None:
            if capture and not existing.capture_blob:
                self.attach_capture(existing.event_id, capture)
            return existing
        if capture:
            if event.capture_blob:
                _write_durable(os.path.join(self.root, event.capture_blob), bytes(capture))
            else:
                event.capture_blob = self._write_capture(event.event_id, capture)
        self.events[event.event_id] = event
        self._dirty.add(event.event_id)
        try:
            self._flush()
        except BaseException:
            # A record that is not on disk must not look imported.
            self.events.pop(event.event_id, None)
            self._dirty.discard(event.event_id)
            raise
        return event

    def attach_capture(self, event_id_value: str, capture: bytes) -> RfEvent:
        """Durably attach the raw record bytes to an event that has none."""
        self._check_writable()
        event = self.events[event_id_value]
        if event.capture_blob and self.read_capture(event):
            raise ValueError(f"RF event {event_id_value} already has a capture")
        previous = event.capture_blob
        event.capture_blob = self._write_capture(event_id_value, capture)
        self._dirty.add(event_id_value)
        try:
            self._flush()
        except BaseException:
            event.capture_blob = previous
            self._dirty.discard(event_id_value)
            raise
        return event

    # ------------------------------------------------------------------ reading
    def pending(self) -> list:
        return [event for event in self.events.values() if event.upload_state == "pending"]

    def _capture_path(self, event_or_id) -> str:
        event_id_value = event_or_id if isinstance(event_or_id, str) else event_or_id.event_id
        event = self.events.get(str(event_id_value))
        if event is None or not event.capture_blob:
            return ""
        try:
            return os.path.join(self.root, validate_capture_blob(event.capture_blob))
        except ValueError:
            return ""

    def has_capture(self, event_or_id) -> bool:
        """Cheap check (no read) that the raw capture file exists."""
        path = self._capture_path(event_or_id)
        return bool(path) and os.path.isfile(path)

    def read_capture(self, event_or_id) -> bytes:
        """Read a raw capture only after validating its store-relative path."""
        path = self._capture_path(event_or_id)
        if not path:
            return b""
        try:
            with open(path, "rb") as fh:
                return fh.read()
        except OSError:
            return b""

    def signature(self) -> tuple:
        """Cheap change detector for the folder (stat calls only)."""
        return folder_signature(self.root)


def folder_signature(root) -> tuple:
    """``(name, mtime_ns, size)`` of the files/folders that change on import."""
    root = os.fspath(root)
    parts = []
    for name in ("events.jsonl", "events", "uploaded", "receipts", "captures"):
        try:
            info = os.stat(os.path.join(root, name))
        except OSError:
            continue
        parts.append((name, info.st_mtime_ns, info.st_size))
    return tuple(parts)


__all__ = ["EventStore", "FamilyGrouper", "RfEvent", "app_data_dir", "default_store_root",
           "event_id", "fingerprint", "folder_signature", "resolve_store_root", "utc_now",
           "validate_capture_blob", "validate_event_id"]
