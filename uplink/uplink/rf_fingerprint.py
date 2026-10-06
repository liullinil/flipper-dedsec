"""Conservative RF structure fingerprints and desktop family grouping.

The hunter event model intentionally stays small and portable.  This module
keeps the richer desktop comparison logic separate and accepts any object with
the event attributes (or a mapping with those keys), so it works with the
root ``rf_hunter.RfEvent`` as well as imported JSON records.

Payload bytes never become device identity.  They are retained as a comparison
hint so changing counters/rolling codes can be explained as a variable payload.
RSSI is a supporting observation only and is excluded from identity.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from datetime import datetime
from typing import Any, Iterable, Mapping, Optional


# Version 3 includes source/NFC technology metadata in the canonical shape;
# old fingerprints remain valid evidence but are intentionally not silently
# treated as equivalent to the new schema.
FINGERPRINT_VERSION = 3
FREQUENCY_TOLERANCE_HZ = 150_000
PULSE_LIMIT = 256
MIN_STRUCTURAL_PULSES = 3
MERGE_SCORE = 0.76
HIGH_CONFIDENCE_SCORE = 0.86


def _get(event: Any, name: str, default: Any = None) -> Any:
    if isinstance(event, Mapping):
        return event.get(name, default)
    return getattr(event, name, default)


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _is_nfc(event: Any) -> bool:
    """Return true for passive NFC field observations.

    NFC field detection intentionally has no pulse stream in the FAP event;
    technology/protocol metadata and the carrier frequency are the stable
    structural evidence available for grouping.
    """
    return _text(_get(event, "source_type", "subghz"), "subghz").lower() == "nfc"


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normal_modulation(value: Any) -> str:
    text = _text(value, "unknown").lower().replace("-", "").replace("_", "")
    aliases = {
        "ook": "OOK", "ask": "ASK", "am": "ASK", "fsk": "FSK",
        "2fsk": "2FSK", "gfsk": "GFSK", "msk": "MSK", "psk": "PSK",
        "bpsk": "BPSK", "qpsk": "QPSK", "nrz": "NRZ", "nfc": "NFC",
    }
    return aliases.get(text, _text(value, "unknown").upper() or "unknown")


def _pulses(event: Any) -> tuple[int, ...]:
    value = _get(event, "pulse_timings_us", ())
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = [part for part in value.replace(";", ",").split(",") if part.strip()]
    if not value:
        return ()
    result = []
    try:
        for item in value:
            pulse = _int(item)
            if pulse > 0:
                result.append(pulse)
            if len(result) >= PULSE_LIMIT:
                break
    except TypeError:
        return ()
    return tuple(result)


def _payload_bytes(event: Any) -> bytes:
    """Read optional payload forms without treating capture paths as payload."""
    candidates = (
        "payload", "payload_bytes", "raw_payload", "demodulated_payload",
        "capture_data", "data", "bits", "payload_bits",
    )
    for name in candidates:
        value = _get(event, name, None)
        if value is None or value == "":
            continue
        if isinstance(value, bytes):
            return value
        if isinstance(value, bytearray):
            return bytes(value)
        if isinstance(value, (tuple, list)):
            try:
                return bytes(int(part) & 0xFF for part in value)
            except (TypeError, ValueError):
                continue
        text = _text(value)
        if text.startswith("0x"):
            text = text[2:]
        if text and all(char in "0123456789abcdefABCDEF" for char in text) and len(text) % 2 == 0:
            try:
                return bytes.fromhex(text)
            except ValueError:
                pass
        if name.endswith("bits") and all(char in "01" for char in text):
            packed = bytearray()
            for offset in range(0, len(text), 8):
                packed.append(int(text[offset:offset + 8].ljust(8, "0"), 2))
            return bytes(packed)
        return text.encode("utf-8", "replace")
    return b""


def _payload_bit_length(event: Any, payload: bytes) -> int:
    explicit = _get(event, "payload_bit_length", None)
    if explicit is not None:
        return max(0, _int(explicit))
    bits = _get(event, "payload_bits", None)
    if isinstance(bits, str) and bits and set(bits) <= {"0", "1"}:
        return len(bits)
    return len(payload) * 8


def _round_ratio(value: float) -> float:
    # Half-unit ratios tolerate clock jitter (including a slightly different
    # shortest pulse used as the normalisation base) while retaining the
    # short/long pulse shape used by OOK/ASK and FSK timings.
    return round(value * 2.0) / 2.0


def _timing_features(pulses: tuple[int, ...]) -> dict:
    if not pulses:
        return {
            "pulse_count": 0,
            "pulse_ratios": (),
            "pulse_histogram": (),
            "preamble": (),
            "frame_length": None,
            "gap_histogram": (),
            "burst_duration_us": 0,
        }
    base = max(1, min(pulses))
    ratios = tuple(_round_ratio(pulse / base) for pulse in pulses)
    # Absolute widths are an explanatory feature and a coarse fingerprint
    # component.  A 100 microsecond bucket absorbs normal receiver clock
    # jitter while the ratio sequence retains the finer waveform shape.
    width_hist = Counter(max(1, int(round(pulse / 100.0) * 100)) for pulse in pulses)
    ratio_hist = Counter(ratios)
    gap_values = [pulse for pulse in pulses if pulse >= base * 2.5]
    gap_hist = Counter(_round_ratio(pulse / base) for pulse in gap_values)
    return {
        "pulse_count": len(pulses),
        "pulse_ratios": ratios,
        "pulse_histogram": tuple(sorted((int(width), int(count)) for width, count in width_hist.items())),
        "ratio_histogram": tuple(sorted((float(width), int(count)) for width, count in ratio_hist.items())),
        "preamble": ratios[: min(8, len(ratios))],
        "frame_length": len(pulses),
        "gap_histogram": tuple(sorted((float(width), int(count)) for width, count in gap_hist.items())),
        "burst_duration_us": sum(pulses),
        "base_pulse_us": base,
    }


def _payload_features(event: Any, payload: bytes) -> dict:
    bits = _payload_bit_length(event, payload)
    digest = hashlib.sha256(payload).hexdigest() if payload else ""
    transitions = 0
    runs = Counter()
    if payload:
        bit_string = "".join(f"{byte:08b}" for byte in payload)
        bit_string = bit_string[:bits] if bits else bit_string
        if bit_string:
            previous = bit_string[0]
            run = 1
            for current in bit_string[1:]:
                if current == previous:
                    run += 1
                else:
                    runs[min(32, run)] += 1
                    transitions += 1
                    previous, run = current, 1
            runs[min(32, run)] += 1
    return {
        "bit_length": bits,
        "byte_length": len(payload),
        "run_histogram": tuple(sorted((int(length), int(count)) for length, count in runs.items())),
        "transitions": transitions,
        "digest": digest,
        # Bit order is deliberately informational.  It is not part of the
        # structural identity because rolling-code implementations may reverse
        # or rotate the payload representation between observations.
        "bit_order": _text(_get(event, "bit_order", "unknown"), "unknown").lower(),
    }


def extract_features(event: Any) -> dict:
    """Return deterministic, JSON-friendly measurable structure features."""
    pulses = _pulses(event)
    payload = _payload_bytes(event)
    timing = _timing_features(pulses)
    payload_info = _payload_features(event, payload)
    frequency = _int(_get(event, "frequency_hz", 0))
    bandwidth = _int(_get(event, "bandwidth_hz", 0))
    repeat_count = max(0, _int(_get(event, "repeat_count", 1), 1))
    timing["preamble_shape"] = timing.get("preamble", ())
    return {
        "frequency_hz": frequency,
        "frequency_bucket_hz": int(round(frequency / 100_000.0) * 100_000) if frequency else 0,
        "modulation": _normal_modulation(_get(event, "modulation", "unknown")),
        "bandwidth_hz": bandwidth,
        "bandwidth_bucket_hz": int(round(bandwidth / 1_000.0) * 1_000) if bandwidth else 0,
        "repeat_count": repeat_count,
        "timing": timing,
        "payload": payload_info,
        "has_pulses": len(pulses) >= MIN_STRUCTURAL_PULSES,
        "has_payload": bool(payload),
        "source_type": _text(_get(event, "source_type", "subghz"), "subghz").lower(),
        "nfc_technology": _text(_get(event, "nfc_technology", _get(event, "technology", ""))),
        "nfc_protocol": _text(_get(event, "nfc_protocol", _get(event, "protocol", ""))),
    }


def _canonical_structure(features: dict) -> dict:
    timing = features["timing"]
    return {
        "v": FINGERPRINT_VERSION,
        "frequency": features["frequency_bucket_hz"],
        "modulation": features["modulation"],
        "bandwidth": features["bandwidth_bucket_hz"],
        "repeat": features["repeat_count"],
        "pulse_count": timing["pulse_count"],
        "pulse_histogram": timing["pulse_histogram"],
        "ratio_histogram": timing.get("ratio_histogram", ()),
        "preamble": timing["preamble"],
        "gap_histogram": timing["gap_histogram"],
        "frame_length": timing["frame_length"],
        "burst_bucket": int(round(timing["burst_duration_us"] / 250.0) * 250),
        # Payload length is structural framing, while bytes and bit order are
        # intentionally excluded so rolling code changes remain related.
        "payload_bits": features["payload"]["bit_length"] or None,
        "source_type": features["source_type"],
        "nfc_technology": features.get("nfc_technology", ""),
        "nfc_protocol": features.get("nfc_protocol", ""),
    }


def fingerprint(event: Any) -> str:
    """Build a stable structural hash; payload bytes and RSSI are excluded."""
    features = extract_features(event)
    raw = json.dumps(_canonical_structure(features), sort_keys=True, separators=(",", ":"), default=list)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _identity(event: Any) -> str:
    event_id = _text(_get(event, "event_id", ""))
    if event_id:
        return event_id
    parts = (_text(_get(event, "device_uuid", "")), _text(_get(event, "session_id", "")),
             _text(_get(event, "sequence_number", "")))
    return "|".join(parts) if any(parts) else ""


def _ratio_similarity(left: Iterable[float], right: Iterable[float]) -> float:
    left, right = tuple(left), tuple(right)
    if not left or not right:
        return 0.0
    count = min(len(left), len(right))
    matches = sum(abs(left[index] - right[index]) <= 0.25 for index in range(count))
    return matches / max(len(left), len(right))


def _hist_similarity(left: Iterable[tuple], right: Iterable[tuple]) -> float:
    a = Counter(dict(left))
    b = Counter(dict(right))
    if not a or not b:
        return 0.0
    total = max(sum(a.values()), sum(b.values()))
    overlap = sum(min(value, b.get(key, 0)) for key, value in a.items())
    return overlap / max(1, total)


def _frequency_similarity(left: int, right: int, tolerance_hz: int = FREQUENCY_TOLERANCE_HZ) -> Optional[float]:
    if not left or not right:
        return None
    drift = abs(left - right)
    tolerance_hz = max(1, int(tolerance_hz))
    if drift > tolerance_hz * 2:
        return 0.0
    return max(0.0, 1.0 - drift / float(tolerance_hz * 2))


def compare_events(left: Any, right: Any,
                   frequency_tolerance_hz: int = FREQUENCY_TOLERANCE_HZ) -> dict:
    """Explain whether two observations are exact, structural, or uncertain."""
    left_id, right_id = _identity(left), _identity(right)
    lf, rf = extract_features(left), extract_features(right)
    feature_view = {
        "frequency": {"left": lf["frequency_hz"], "right": rf["frequency_hz"]},
        "modulation": {"left": lf["modulation"], "right": rf["modulation"]},
        "bandwidth": {"left": lf["bandwidth_hz"], "right": rf["bandwidth_hz"]},
        "pulse_count": {"left": lf["timing"]["pulse_count"], "right": rf["timing"]["pulse_count"]},
        "preamble": {"left": lf["timing"]["preamble"], "right": rf["timing"]["preamble"]},
        "frame_length": {"left": lf["timing"]["frame_length"], "right": rf["timing"]["frame_length"]},
        "repeat_count": {"left": lf["repeat_count"], "right": rf["repeat_count"]},
        "payload_bits": {"left": lf["payload"]["bit_length"], "right": rf["payload"]["bit_length"]},
    }
    if left is right or (left_id and left_id == right_id):
        return {
            "score": 1.0, "percent": 100, "confidence": 1.0,
            "relationship": "exact_event", "relationship_text": "exact event",
            "reasons": ["same event identity"],
            "features": feature_view,
            "matched_features": ("same event identity",),
        }

    # A field detector event has no decoded frame or pulse stream.  Group
    # repeated observations by the passive evidence that is actually present
    # instead of manufacturing a weak pulse similarity score.
    if _is_nfc(left) and _is_nfc(right):
        same_technology = bool(lf["nfc_technology"] and rf["nfc_technology"] and
                               lf["nfc_technology"].casefold() == rf["nfc_technology"].casefold())
        same_protocol = bool(lf["nfc_protocol"] and rf["nfc_protocol"] and
                             lf["nfc_protocol"].casefold() == rf["nfc_protocol"].casefold())
        frequency_score = _frequency_similarity(lf["frequency_hz"], rf["frequency_hz"],
                                                 frequency_tolerance_hz)
        same_carrier = frequency_score is not None and frequency_score >= 0.75
        score = 0.0
        reasons = []
        if same_technology:
            score += 0.40; reasons.append("same NFC technology")
        if same_protocol:
            score += 0.30; reasons.append("same NFC protocol")
        if same_carrier:
            score += 0.30; reasons.append("same NFC carrier")
        if score >= 0.70:
            return {
                "score": round(score, 3), "percent": int(round(score * 100)),
                "confidence": round(score, 3), "relationship": "same_structure",
                "relationship_text": "same NFC field source", "reasons": reasons,
                "features": feature_view, "matched_features": tuple(reasons),
            }
        return {
            "score": round(score, 3), "percent": int(round(score * 100)),
            "confidence": round(score * 0.55, 3), "relationship": "unknown",
            "relationship_text": "unknown", "reasons": reasons or ["insufficient NFC metadata"],
            "features": feature_view,
            "matched_features": tuple(reasons),
        }

    reasons = []
    weighted = []
    frequency_score = _frequency_similarity(lf["frequency_hz"], rf["frequency_hz"],
                                             frequency_tolerance_hz)
    if frequency_score is not None:
        weighted.append((0.18, frequency_score))
        if frequency_score >= 0.75:
            reasons.append("same carrier frequency")
    if lf["modulation"] != "unknown" and rf["modulation"] != "unknown":
        score = 1.0 if lf["modulation"] == rf["modulation"] else 0.0
        weighted.append((0.15, score))
        if score:
            reasons.append("same modulation")
    if lf["bandwidth_hz"] and rf["bandwidth_hz"]:
        bandwidth_score = max(0.0, 1.0 - abs(lf["bandwidth_hz"] - rf["bandwidth_hz"]) / 20_000.0)
        weighted.append((0.08, bandwidth_score))
        if bandwidth_score >= 0.75:
            reasons.append("similar bandwidth")

    timing_known = lf["has_pulses"] and rf["has_pulses"]
    if timing_known:
        ratio_score = _ratio_similarity(lf["timing"]["pulse_ratios"], rf["timing"]["pulse_ratios"])
        hist_score = _hist_similarity(lf["timing"]["pulse_histogram"], rf["timing"]["pulse_histogram"])
        preamble_score = _ratio_similarity(lf["timing"]["preamble"], rf["timing"]["preamble"])
        length_score = 1.0 if abs(lf["timing"]["pulse_count"] - rf["timing"]["pulse_count"]) <= 2 else 0.0
        repeat_score = 1.0 if lf["repeat_count"] == rf["repeat_count"] else 0.5
        weighted.extend(((0.30, ratio_score), (0.16, hist_score), (0.10, preamble_score),
                         (0.06, length_score), (0.05, repeat_score)))
        if ratio_score >= 0.8:
            reasons.append("same pulse timing")
        if preamble_score >= 0.8:
            reasons.append("same preamble structure")
        if length_score:
            reasons.append("same frame length")
    else:
        # Unknown structural data must not count as a match.  Carrier and
        # modulation alone are deliberately capped at weak confidence.
        reasons.append("insufficient pulse structure")

    total_weight = sum(weight for weight, _ in weighted)
    score = sum(weight * value for weight, value in weighted) / total_weight if total_weight else 0.0
    if not timing_known:
        score = min(score, 0.42)
    score = round(max(0.0, min(1.0, score)), 3)

    payload_same = False
    payload_diff = False
    if lf["has_payload"] and rf["has_payload"]:
        payload_same = lf["payload"]["digest"] == rf["payload"]["digest"]
        payload_diff = not payload_same
        if payload_diff and lf["payload"]["bit_length"] == rf["payload"]["bit_length"]:
            reasons.append("payload differs with stable frame length")
        elif payload_diff:
            reasons.append("payload length differs")

    if timing_known and score >= HIGH_CONFIDENCE_SCORE:
        relationship = "variable_payload" if payload_diff else "same_structure"
        confidence = score
    elif score >= 0.55:
        relationship = "weak_similarity"
        confidence = score * 0.75
    else:
        relationship = "unknown"
        confidence = score * 0.55
    if payload_same and timing_known and score >= 0.85:
        reasons.append("same payload")
    relationship_alias = {
        "exact_event": "exact event", "same_structure": "same structure",
        "variable_payload": "variable payload", "weak_similarity": "weak similarity",
        "unknown": "unknown",
    }[relationship]
    return {
        "score": score,
        "percent": int(round(score * 100)),
        "confidence": round(confidence, 3),
        "relationship": relationship,
        "relationship_text": relationship_alias,
        "reasons": reasons,
        "features": feature_view,
        "matched_features": tuple(reasons),
    }


def classify(event: Any) -> dict:
    """Return a conservative source hypothesis, never an exact device claim."""
    features = extract_features(event)
    if not features["has_pulses"]:
        result = {"classification": "unknown", "label": "unknown", "confidence": 0.0,
                  "reasons": ["insufficient pulse structure"]}
    elif features["repeat_count"] >= 2 and features["timing"]["pulse_count"] <= 96:
        result = {"classification": "remote-like", "label": "remote-like", "confidence": 0.55,
                  "reasons": ["short repeated pulse frame"]}
    elif features["timing"]["pulse_count"] >= 48 or features["timing"]["burst_duration_us"] >= 50_000:
        result = {"classification": "sensor-like", "label": "sensor-like", "confidence": 0.45,
                  "reasons": ["long or dense burst structure"]}
    else:
        result = {"classification": "unknown", "label": "unknown", "confidence": 0.2,
                  "reasons": ["structure is not distinctive enough"]}
    return result


# Descriptive aliases make the helper convenient for callers that do not use
# the short UI-facing name while keeping one implementation and one schema.
classify_event = classify
extract_signal_features = extract_features


class StructuralGrouper:
    """Complete-link structural families with deterministic IDs and rebuild."""

    def __init__(self, frequency_tolerance_hz=FREQUENCY_TOLERANCE_HZ,
                 merge_score=MERGE_SCORE):
        self.frequency_tolerance_hz = int(frequency_tolerance_hz)
        self.merge_score = float(merge_score)
        self.families: dict[str, dict] = {}
        self._events: dict[str, Any] = {}
        self._members: dict[str, list[Any]] = {}

    def _new_family(self, event: Any, fp: str) -> str:
        family_id = "family-" + hashlib.sha256(fp.encode("ascii")).hexdigest()[:12]
        # A missing-data event gets a unique provisional family even when a
        # coincident fingerprint is present; it cannot be forced into a group.
        # Pulse-less Sub-GHz observations remain provisional and unique. NFC
        # field events are a deliberate exception: field presence plus the
        # 13.56 MHz technology/protocol metadata is the available structure.
        if not extract_features(event)["has_pulses"] and not _is_nfc(event):
            family_id = family_id + "-" + hashlib.sha256(_identity(event).encode()).hexdigest()[:6]
        features = extract_features(event)
        classification = classify(event)
        when = _text(_get(event, "captured_at_utc", ""))
        self.families[family_id] = {
            "event_ids": [], "fingerprints": set(), "first_seen": when,
            "last_seen": when, "frequency_hz": features["frequency_hz"],
            "modulation": features["modulation"], "representative": event,
            "confidence": 0.0 if (not features["has_pulses"] and not _is_nfc(event)) else 1.0,
            "provisional": not features["has_pulses"] and not _is_nfc(event),
            "classification": classification["classification"],
            "classification_confidence": classification["confidence"],
        }
        self._members[family_id] = []
        return family_id

    def _add(self, family_id: str, event: Any, fp: str, relation: Optional[dict] = None):
        family = self.families[family_id]
        event_id = _identity(event)
        if event_id and event_id not in family["event_ids"]:
            family["event_ids"].append(event_id)
        family["fingerprints"].add(fp)
        self._members[family_id].append(event)
        when = _text(_get(event, "captured_at_utc", ""))
        if when:
            if not family["first_seen"] or when < family["first_seen"]:
                family["first_seen"] = when
            if not family["last_seen"] or when > family["last_seen"]:
                family["last_seen"] = when
        if relation:
            family["confidence"] = min(family["confidence"] or relation["score"], relation["score"])
        if hasattr(event, "family_id"):
            try:
                event.family_id = family_id
            except Exception:
                pass

    def _candidate(self, event: Any) -> Optional[tuple[str, float]]:
        features = extract_features(event)
        if not features["has_pulses"] and not _is_nfc(event):
            return None
        best = None
        for family_id in sorted(self.families):
            members = self._members.get(family_id, ())
            if not members:
                continue
            if _is_nfc(event):
                if not all(_is_nfc(member) for member in members):
                    continue
            elif not all(extract_features(member)["has_pulses"] for member in members):
                continue
            comparisons = [compare_events(event, member,
                                           frequency_tolerance_hz=self.frequency_tolerance_hz)
                           for member in members]
            if any(result["relationship"] not in ("same_structure", "variable_payload")
                   or result["score"] < self.merge_score for result in comparisons):
                continue
            confidence = min(result["score"] for result in comparisons)
            if best is None or confidence > best[1]:
                best = (family_id, confidence)
        return best

    def assign(self, event: Any) -> str:
        """Assign one observation; NFC field metadata can group without pulses."""
        event_id = _identity(event)
        if event_id and event_id in self._events:
            for family_id, members in self._members.items():
                if any(_identity(member) == event_id for member in members):
                    return family_id
        provided_fp = _text(_get(event, "fingerprint_id", ""))
        # The FAP's ``local-`` hash is a compact receipt hint.  It is not the
        # desktop SHA-256 fingerprint and must not make unrelated pulse shapes
        # share a family merely because their short hints collide.
        fp = fingerprint(event) if not provided_fp or provided_fp.startswith("local-") else provided_fp
        if hasattr(event, "fingerprint_id"):
            try:
                if not provided_fp or provided_fp.startswith("local-"):
                    event.fingerprint_id = fp
            except Exception:
                pass
        candidate = self._candidate(event)
        family_id = candidate[0] if candidate else self._new_family(event, fp)
        self._add(family_id, event, fp, {"score": candidate[1]} if candidate else None)
        if event_id:
            self._events[event_id] = event
        return family_id

    def rebuild(self, events: Iterable[Any]) -> dict[str, dict]:
        """Recompute families from immutable events using deterministic order."""
        self.families = {}
        self._members = {}
        self._events = {}
        if isinstance(events, Mapping):
            events = events.values()
        ordered = sorted(list(events), key=lambda event: (fingerprint(event), _identity(event)))
        for event in ordered:
            self.assign(event)
        return self.families


__all__ = ["FINGERPRINT_VERSION", "StructuralGrouper", "classify", "classify_event",
           "compare_events", "extract_features", "extract_signal_features", "fingerprint"]
