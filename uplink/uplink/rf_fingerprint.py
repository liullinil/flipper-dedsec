"""Conservative RF structure fingerprints and desktop family grouping.

The hunter event model intentionally stays small and portable.  This module
keeps the richer desktop comparison logic separate and accepts any object with
the event attributes (or a mapping with those keys), so it works with the
root ``rf_hunter.RfEvent`` as well as imported JSON records.

Payload bytes never become device identity.  They are retained as a comparison
hint so changing counters/rolling codes can be explained as a variable payload.
RSSI is a supporting observation only and is excluded from identity.

Grouping never modifies the events: :class:`StructuralGrouper` keeps the
desktop family and fingerprint of every event in its own maps
(``family_of``/``fingerprint_of``), so the Flipper's stored evidence (for
example its ``local-...`` fingerprint hint) is never overwritten.
"""

from __future__ import annotations

import hashlib
import json
import operator
from collections import Counter
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple


# Version 3 includes source/NFC technology metadata in the canonical shape;
# old fingerprints remain valid evidence but are intentionally not silently
# treated as equivalent to the new schema.
FINGERPRINT_VERSION = 3
FREQUENCY_TOLERANCE_HZ = 150_000
PULSE_LIMIT = 256
MIN_STRUCTURAL_PULSES = 3
MERGE_SCORE = 0.76
HIGH_CONFIDENCE_SCORE = 0.86

# Exact pruning bound used before a full comparison (see
# ``StructuralGrouper._candidate``).  A structural match needs a rounded score
# of at least HIGH_CONFIDENCE_SCORE, i.e. a weighted loss of at most
# 0.1405 * 1.08 (all weights present) = 0.152.  Pulse-ratio (0.30) and
# histogram (0.16) similarities are both bounded by min/max of the pulse
# counts, so counts with a ratio below 0.670 can never match; 0.66 leaves a
# safety margin.  A carrier more than 2 x tolerance away scores 0 and loses
# 0.18 > 0.152, so it can never match either.
MIN_PULSE_COUNT_RATIO = 0.66
PAIR_CACHE_LIMIT = 200_000

_MISSING = object()
_CONTENT_FIELDS = (
    "source_type", "pulse_timings_us", "payload", "payload_bytes", "raw_payload",
    "demodulated_payload", "capture_data", "data", "bits", "payload_bits",
    "payload_bit_length", "bit_order", "frequency_hz", "bandwidth_hz", "repeat_count",
    "modulation", "nfc_technology", "technology", "nfc_protocol", "protocol",
)


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
    except (TypeError, ValueError, OverflowError):
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


def _fingerprint_features(features: dict) -> str:
    raw = json.dumps(_canonical_structure(features), sort_keys=True, separators=(",", ":"), default=list)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def fingerprint(event: Any, cache: Optional["FeatureCache"] = None) -> str:
    """Build a stable structural hash; payload bytes and RSSI are excluded."""
    if cache is not None:
        return cache.prepared(event).fingerprint
    return _fingerprint_features(extract_features(event))


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
    matches = sum(1 for a, b in zip(left, right) if abs(a - b) <= 0.25)
    return matches / max(len(left), len(right))


def _half_step_similarity(left: tuple, right: tuple) -> float:
    """``_ratio_similarity`` for ratios produced by ``_round_ratio``.

    Those ratios are exact multiples of 0.5, so ``abs(a - b) <= 0.25`` holds
    exactly when ``a == b``; the C-level comparison gives the same result
    several times faster.
    """
    if not left or not right:
        return 0.0
    return sum(map(operator.eq, left, right)) / max(len(left), len(right))


def _hist_map_similarity(a: dict, a_total: int, b: dict, b_total: int) -> float:
    if not a or not b:
        return 0.0
    total = max(a_total, b_total)
    # Only shared widths contribute (min(count, 0) == 0); the key
    # intersection runs in C and keeps the Python loop short.
    overlap = sum(min(a[key], b[key]) for key in a.keys() & b.keys())
    return overlap / max(1, total)


def _hist_similarity(left: Iterable[tuple], right: Iterable[tuple]) -> float:
    a = dict(left)
    b = dict(right)
    return _hist_map_similarity(a, sum(a.values()), b, sum(b.values()))


def _frequency_similarity(left: int, right: int, tolerance_hz: int = FREQUENCY_TOLERANCE_HZ) -> Optional[float]:
    if not left or not right:
        return None
    drift = abs(left - right)
    tolerance_hz = max(1, int(tolerance_hz))
    if drift > tolerance_hz * 2:
        return 0.0
    return max(0.0, 1.0 - drift / float(tolerance_hz * 2))


def _nfc_score(lf: dict, rf: dict, tolerance_hz: int, reasons: Optional[list] = None) -> float:
    """Raw (unrounded) passive NFC field-source score."""
    same_technology = bool(lf["nfc_technology"] and rf["nfc_technology"] and
                           lf["nfc_technology"].casefold() == rf["nfc_technology"].casefold())
    same_protocol = bool(lf["nfc_protocol"] and rf["nfc_protocol"] and
                         lf["nfc_protocol"].casefold() == rf["nfc_protocol"].casefold())
    frequency_score = _frequency_similarity(lf["frequency_hz"], rf["frequency_hz"], tolerance_hz)
    same_carrier = frequency_score is not None and frequency_score >= 0.75
    score = 0.0
    if same_technology:
        score += 0.40
        if reasons is not None:
            reasons.append("same NFC technology")
    if same_protocol:
        score += 0.30
        if reasons is not None:
            reasons.append("same NFC protocol")
    if same_carrier:
        score += 0.30
        if reasons is not None:
            reasons.append("same NFC carrier")
    return score


def _subghz_score(lf: dict, rf: dict, tolerance_hz: int, reasons: Optional[list] = None,
                  hist: Optional[tuple] = None) -> Tuple[float, bool]:
    """Rounded weighted structure score and whether pulse timing was compared.

    ``compare_events`` and the grouper share this single implementation so a
    family decision always matches the explanation shown to the user.
    ``hist`` optionally carries precomputed ``(left_map, left_total,
    right_map, right_total)`` width histograms.
    """
    weighted = []
    frequency_score = _frequency_similarity(lf["frequency_hz"], rf["frequency_hz"], tolerance_hz)
    if frequency_score is not None:
        weighted.append((0.18, frequency_score))
        if reasons is not None and frequency_score >= 0.75:
            reasons.append("same carrier frequency")
    if lf["modulation"] != "unknown" and rf["modulation"] != "unknown":
        score = 1.0 if lf["modulation"] == rf["modulation"] else 0.0
        weighted.append((0.15, score))
        if reasons is not None and score:
            reasons.append("same modulation")
    if lf["bandwidth_hz"] and rf["bandwidth_hz"]:
        bandwidth_score = max(0.0, 1.0 - abs(lf["bandwidth_hz"] - rf["bandwidth_hz"]) / 20_000.0)
        weighted.append((0.08, bandwidth_score))
        if reasons is not None and bandwidth_score >= 0.75:
            reasons.append("similar bandwidth")

    timing_known = lf["has_pulses"] and rf["has_pulses"]
    if timing_known:
        left_timing, right_timing = lf["timing"], rf["timing"]
        ratio_score = _half_step_similarity(left_timing["pulse_ratios"], right_timing["pulse_ratios"])
        if hist is None:
            hist_score = _hist_similarity(left_timing["pulse_histogram"], right_timing["pulse_histogram"])
        else:
            hist_score = _hist_map_similarity(*hist)
        preamble_score = _half_step_similarity(left_timing["preamble"], right_timing["preamble"])
        length_score = 1.0 if abs(left_timing["pulse_count"] - right_timing["pulse_count"]) <= 2 else 0.0
        repeat_score = 1.0 if lf["repeat_count"] == rf["repeat_count"] else 0.5
        weighted.extend(((0.30, ratio_score), (0.16, hist_score), (0.10, preamble_score),
                         (0.06, length_score), (0.05, repeat_score)))
        if reasons is not None:
            if ratio_score >= 0.8:
                reasons.append("same pulse timing")
            if preamble_score >= 0.8:
                reasons.append("same preamble structure")
            if length_score:
                reasons.append("same frame length")
    elif reasons is not None:
        # Unknown structural data must not count as a match.  Carrier and
        # modulation alone are deliberately capped at weak confidence.
        reasons.append("insufficient pulse structure")

    total_weight = sum(weight for weight, _ in weighted)
    score = sum(weight * value for weight, value in weighted) / total_weight if total_weight else 0.0
    if not timing_known:
        score = min(score, 0.42)
    return round(max(0.0, min(1.0, score)), 3), timing_known


def _hashable(value: Any) -> Any:
    if value is _MISSING or value is None or isinstance(value, (str, bytes, int, float)):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, (tuple, list)):
        value = tuple(value)
        try:
            hash(value)
            return value
        except TypeError:
            return tuple(_hashable(item) for item in value)
    hash(value)  # raises TypeError for unhashable values: such an event is not cached
    return value


def _content_key(event: Any) -> tuple:
    """Every attribute ``extract_features`` reads; equal keys give equal features."""
    if isinstance(event, Mapping):
        return tuple(_hashable(event.get(name, _MISSING)) for name in _CONTENT_FIELDS)
    return tuple(_hashable(getattr(event, name, _MISSING)) for name in _CONTENT_FIELDS)


def _structure_signature(features: dict) -> tuple:
    """Everything a grouping decision depends on (payload bytes are excluded)."""
    timing = features["timing"]
    return (features["source_type"] == "nfc", features["frequency_hz"], features["modulation"],
            features["bandwidth_hz"], features["repeat_count"], features["has_pulses"],
            timing["pulse_ratios"], timing["pulse_histogram"],
            features["nfc_technology"].casefold(), features["nfc_protocol"].casefold())


class _Prepared:
    """Features of one event content plus the precomputed comparison inputs."""

    __slots__ = ("features", "nfc", "freq", "has_pulses", "count", "hist", "hist_total", "sig", "_fp")

    def __init__(self, features: dict, sig: int):
        timing = features["timing"]
        self.features = features
        self.nfc = features["source_type"] == "nfc"
        self.freq = features["frequency_hz"]
        self.has_pulses = features["has_pulses"]
        self.count = timing["pulse_count"]
        self.hist = dict(timing["pulse_histogram"])
        self.hist_total = sum(self.hist.values())
        self.sig = sig
        self._fp = None

    @property
    def fingerprint(self) -> str:
        if self._fp is None:
            self._fp = _fingerprint_features(self.features)
        return self._fp


class FeatureCache:
    """Features keyed by event content, shared by rebuilds and comparisons.

    The key is built from every attribute :func:`extract_features` reads, so a
    replaced or mutated event is recomputed automatically while a reloaded
    copy of the same record reuses the cached result.
    """

    def __init__(self):
        self._items: Dict[tuple, _Prepared] = {}
        self._sig_ids: Dict[tuple, int] = {}
        self._next_sig = 0
        self.pairs: Dict[tuple, Tuple[bool, float]] = {}

    def __len__(self):
        return len(self._items)

    def _prepare(self, event: Any) -> _Prepared:
        features = extract_features(event)
        signature = _structure_signature(features)
        sig = self._sig_ids.get(signature)
        if sig is None:
            # Monotonic ids are never reused, so cached pair results stay valid.
            sig = self._sig_ids[signature] = self._next_sig
            self._next_sig += 1
        return _Prepared(features, sig)

    def prepared(self, event: Any) -> _Prepared:
        try:
            key = _content_key(event)
        except TypeError:
            return self._prepare(event)
        item = self._items.get(key)
        if item is None:
            item = self._items[key] = self._prepare(event)
        return item

    def features(self, event: Any) -> dict:
        return self.prepared(event).features

    def prune(self, events: Iterable[Any]) -> None:
        """Forget contents that no longer belong to any event (bounded memory)."""
        keep: Dict[tuple, _Prepared] = {}
        for event in events:
            try:
                key = _content_key(event)
            except TypeError:
                continue
            item = self._items.get(key)
            if item is not None:
                keep[key] = item
        self._items = keep
        live = {item.sig for item in keep.values()}
        self._sig_ids = {signature: sig for signature, sig in self._sig_ids.items() if sig in live}
        self.pairs = {key: value for key, value in self.pairs.items()
                      if key[1] in live and key[2] in live}

    def clear(self) -> None:
        self._items.clear()
        self._sig_ids.clear()
        self.pairs.clear()


def compare_events(left: Any, right: Any,
                   frequency_tolerance_hz: int = FREQUENCY_TOLERANCE_HZ,
                   cache: Optional[FeatureCache] = None) -> dict:
    """Explain whether two observations are exact, structural, or uncertain."""
    left_id, right_id = _identity(left), _identity(right)
    if cache is not None:
        lf, rf = cache.features(left), cache.features(right)
    else:
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
    if lf["source_type"] == "nfc" and rf["source_type"] == "nfc":
        reasons = []
        score = _nfc_score(lf, rf, frequency_tolerance_hz, reasons)
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
    score, timing_known = _subghz_score(lf, rf, frequency_tolerance_hz, reasons)

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


def _classify_features(features: dict) -> dict:
    if not features["has_pulses"]:
        return {"classification": "unknown", "label": "unknown", "confidence": 0.0,
                "reasons": ["insufficient pulse structure"]}
    if features["repeat_count"] >= 2 and features["timing"]["pulse_count"] <= 96:
        return {"classification": "remote-like", "label": "remote-like", "confidence": 0.55,
                "reasons": ["short repeated pulse frame"]}
    if features["timing"]["pulse_count"] >= 48 or features["timing"]["burst_duration_us"] >= 50_000:
        return {"classification": "sensor-like", "label": "sensor-like", "confidence": 0.45,
                "reasons": ["long or dense burst structure"]}
    return {"classification": "unknown", "label": "unknown", "confidence": 0.2,
            "reasons": ["structure is not distinctive enough"]}


def classify(event: Any, cache: Optional[FeatureCache] = None) -> dict:
    """Return a conservative source hypothesis, never an exact device claim."""
    features = cache.features(event) if cache is not None else extract_features(event)
    return _classify_features(features)


# Descriptive aliases make the helper convenient for callers that do not use
# the short UI-facing name while keeping one implementation and one schema.
classify_event = classify
extract_signal_features = extract_features


class _FamilyIndex:
    """Aggregates of one family used to rule it out before full comparisons."""

    __slots__ = ("all_nfc", "all_pulses", "fmin", "fmax", "cmin", "cmax")

    def __init__(self):
        self.all_nfc = True
        self.all_pulses = True
        self.fmin = None
        self.fmax = None
        self.cmin = None
        self.cmax = None

    def add(self, prepared: _Prepared) -> None:
        self.all_nfc = self.all_nfc and prepared.nfc
        self.all_pulses = self.all_pulses and prepared.has_pulses
        if prepared.freq:
            self.fmin = prepared.freq if self.fmin is None else min(self.fmin, prepared.freq)
            self.fmax = prepared.freq if self.fmax is None else max(self.fmax, prepared.freq)
        self.cmin = prepared.count if self.cmin is None else min(self.cmin, prepared.count)
        self.cmax = prepared.count if self.cmax is None else max(self.cmax, prepared.count)


def _count_ratio_ok(left: int, right: int) -> bool:
    low, high = (left, right) if left <= right else (right, left)
    return high <= 0 or low >= MIN_PULSE_COUNT_RATIO * high


# A raw score below 0.859 always rounds below HIGH_CONFIDENCE_SCORE (0.86);
# the 0.001 margin absorbs floating-point summation order differences.
_BOUND_THRESHOLD = HIGH_CONFIDENCE_SCORE - 0.001


def _structure_upper_bound(left: _Prepared, right: _Prepared, tolerance_hz: int) -> float:
    """Upper bound of ``_subghz_score`` for two events with pulse structure.

    Every component is exact except the pulse-ratio sequence similarity
    (the only O(pulses) part), which is bounded by min/max of the pulse
    counts.  Pairs whose bound is below the structural threshold are rejected
    without the full comparison.
    """
    lf, rf = left.features, right.features
    weight = value = 0.0
    frequency_score = _frequency_similarity(lf["frequency_hz"], rf["frequency_hz"], tolerance_hz)
    if frequency_score is not None:
        weight += 0.18
        value += 0.18 * frequency_score
    if lf["modulation"] != "unknown" and rf["modulation"] != "unknown":
        weight += 0.15
        value += 0.15 if lf["modulation"] == rf["modulation"] else 0.0
    if lf["bandwidth_hz"] and rf["bandwidth_hz"]:
        weight += 0.08
        value += 0.08 * max(0.0, 1.0 - abs(lf["bandwidth_hz"] - rf["bandwidth_hz"]) / 20_000.0)
    low, high = (left.count, right.count) if left.count <= right.count else (right.count, left.count)
    ratio_bound = low / high if high else 0.0
    hist_score = _hist_map_similarity(left.hist, left.hist_total, right.hist, right.hist_total)
    preamble_score = _half_step_similarity(lf["timing"]["preamble"], rf["timing"]["preamble"])
    length_score = 1.0 if high - low <= 2 else 0.0
    repeat_score = 1.0 if lf["repeat_count"] == rf["repeat_count"] else 0.5
    weight += 0.30 + 0.16 + 0.10 + 0.06 + 0.05
    value += (0.30 * ratio_bound + 0.16 * hist_score + 0.10 * preamble_score
              + 0.06 * length_score + 0.05 * repeat_score)
    return value / weight


class StructuralGrouper:
    """Complete-link structural families with deterministic IDs and rebuild.

    Results live in ``families``, ``family_of`` (event identity -> family id)
    and ``fingerprint_of`` (event identity -> desktop fingerprint).  The
    grouped events are never modified.
    """

    def __init__(self, frequency_tolerance_hz=FREQUENCY_TOLERANCE_HZ,
                 merge_score=MERGE_SCORE, cache: Optional[FeatureCache] = None):
        self.frequency_tolerance_hz = int(frequency_tolerance_hz)
        self.merge_score = float(merge_score)
        self.cache = cache if cache is not None else FeatureCache()
        self.families: dict[str, dict] = {}
        self.family_of: dict[str, str] = {}
        self.fingerprint_of: dict[str, str] = {}
        self._reset()

    def _reset(self):
        self.families = {}
        self.family_of = {}
        self.fingerprint_of = {}
        self._members: dict[str, list] = {}
        self._member_ids: dict[str, set] = {}
        self._signatures: dict[str, dict] = {}
        self._index: dict[str, _FamilyIndex] = {}
        self._buckets: dict[Optional[int], set] = {}

    @staticmethod
    def key(event: Any) -> str:
        """Map key of an event (its identity, or its object id when it has none)."""
        return _identity(event) or f"object:{id(event)}"

    def family_for(self, event: Any) -> Optional[str]:
        return self.family_of.get(self.key(event))

    def fingerprint_for(self, event: Any) -> Optional[str]:
        return self.fingerprint_of.get(self.key(event))

    # ------------------------------------------------------------------ pruning
    def _bucket(self, frequency: int) -> Optional[int]:
        return frequency // (2 * max(1, self.frequency_tolerance_hz)) if frequency else None

    def _candidate_ids(self, prepared: _Prepared) -> Iterable[str]:
        frequency_decides = prepared.freq and (not prepared.nfc or self.merge_score > 0.70)
        if not frequency_decides:
            return self.families.keys()
        bucket = self._bucket(prepared.freq)
        found = set(self._buckets.get(None, ()))
        for neighbour in (bucket - 1, bucket, bucket + 1):
            found.update(self._buckets.get(neighbour, ()))
        return found

    def _relation(self, left: _Prepared, right: _Prepared) -> Tuple[bool, float]:
        """(structural relationship, rounded score) exactly as compare_events decides.

        The score is only meaningful when the first value is true; the
        grouper never needs the score of an unrelated pair.
        """
        first, second = (left, right) if left.sig <= right.sig else (right, left)
        key = (self.frequency_tolerance_hz, first.sig, second.sig)
        cached = self.cache.pairs.get(key)
        if cached is not None:
            return cached
        if first.nfc and second.nfc:
            raw = _nfc_score(first.features, second.features, self.frequency_tolerance_hz)
            result = (raw >= 0.70, round(raw, 3))
        elif not (first.has_pulses and second.has_pulses):
            result = (False, 0.0)  # no pulse structure: never a structural match
        elif _structure_upper_bound(first, second, self.frequency_tolerance_hz) < _BOUND_THRESHOLD:
            result = (False, 0.0)
        else:
            score, timing_known = _subghz_score(
                first.features, second.features, self.frequency_tolerance_hz,
                hist=(first.hist, first.hist_total, second.hist, second.hist_total))
            result = (timing_known and score >= HIGH_CONFIDENCE_SCORE, score)
        if len(self.cache.pairs) >= PAIR_CACHE_LIMIT:
            self.cache.pairs.clear()
        self.cache.pairs[key] = result
        return result

    def _candidate(self, prepared: _Prepared) -> Optional[tuple[str, float]]:
        if not prepared.has_pulses and not prepared.nfc:
            return None
        limit = 2 * max(1, self.frequency_tolerance_hz)
        check_frequency = prepared.freq and (not prepared.nfc or self.merge_score > 0.70)
        best = None
        for family_id in sorted(self._candidate_ids(prepared)):
            index = self._index[family_id]
            if prepared.nfc:
                if not index.all_nfc:
                    continue
            else:
                if not index.all_pulses:
                    continue
                if not (_count_ratio_ok(prepared.count, index.cmin)
                        and _count_ratio_ok(prepared.count, index.cmax)):
                    continue
            if check_frequency and index.fmin is not None:
                if index.fmax - prepared.freq > limit or prepared.freq - index.fmin > limit:
                    continue
            # Complete link: every distinct member structure must match.
            # Members with an identical structure give identical results, so
            # each structure is compared once.
            confidence = None
            for member in self._signatures[family_id].values():
                related, score = self._relation(prepared, member)
                if not related or score < self.merge_score:
                    confidence = None
                    break
                confidence = score if confidence is None else min(confidence, score)
            if confidence is None:
                continue
            if best is None or confidence > best[1]:
                best = (family_id, confidence)
        return best

    # ------------------------------------------------------------------ families
    def _new_family(self, event: Any, prepared: _Prepared, fp: str) -> str:
        base = "family-" + hashlib.sha256(fp.encode("utf-8")).hexdigest()[:12]
        # A missing-data event gets a unique provisional family even when a
        # coincident fingerprint is present; it cannot be forced into a group.
        # Pulse-less Sub-GHz observations remain provisional and unique. NFC
        # field events are a deliberate exception: field presence plus the
        # 13.56 MHz technology/protocol metadata is the available structure.
        provisional = not prepared.has_pulses and not prepared.nfc
        if provisional:
            base = base + "-" + hashlib.sha256(_identity(event).encode()).hexdigest()[:6]
        family_id, suffix = base, 2
        while family_id in self.families:
            # Same fingerprint but not complete-link compatible: keep both.
            family_id = f"{base}-{suffix}"
            suffix += 1
        features = prepared.features
        classification = _classify_features(features)
        when = _text(_get(event, "captured_at_utc", ""))
        self.families[family_id] = {
            "event_ids": [], "fingerprints": set(), "first_seen": when,
            "last_seen": when, "frequency_hz": features["frequency_hz"],
            "modulation": features["modulation"], "representative": event,
            "confidence": 0.0 if provisional else 1.0,
            "provisional": provisional,
            "classification": classification["classification"],
            "classification_confidence": classification["confidence"],
        }
        self._members[family_id] = []
        self._member_ids[family_id] = set()
        self._signatures[family_id] = {}
        self._index[family_id] = _FamilyIndex()
        return family_id

    def _add(self, family_id: str, event: Any, prepared: _Prepared, fp: str,
             relation: Optional[dict] = None):
        family = self.families[family_id]
        event_id = _identity(event)
        if event_id and event_id not in self._member_ids[family_id]:
            self._member_ids[family_id].add(event_id)
            family["event_ids"].append(event_id)
        family["fingerprints"].add(fp)
        self._members[family_id].append(event)
        self._signatures[family_id].setdefault(prepared.sig, prepared)
        self._index[family_id].add(prepared)
        self._buckets.setdefault(self._bucket(prepared.freq), set()).add(family_id)
        when = _text(_get(event, "captured_at_utc", ""))
        if when:
            if not family["first_seen"] or when < family["first_seen"]:
                family["first_seen"] = when
            if not family["last_seen"] or when > family["last_seen"]:
                family["last_seen"] = when
        if relation:
            family["confidence"] = min(family["confidence"] or relation["score"], relation["score"])

    def assign(self, event: Any) -> str:
        """Assign one observation; NFC field metadata can group without pulses."""
        key = self.key(event)
        existing = self.family_of.get(key)
        if existing is not None:
            return existing
        prepared = self.cache.prepared(event)
        provided_fp = _text(_get(event, "fingerprint_id", ""))
        # The FAP's ``local-`` hash is a compact receipt hint.  It is not the
        # desktop SHA-256 fingerprint and must not make unrelated pulse shapes
        # share a family merely because their short hints collide.  The hint
        # stays untouched on the event; the desktop value lives in the map.
        fp = prepared.fingerprint if not provided_fp or provided_fp.startswith("local-") else provided_fp
        self.fingerprint_of[key] = fp
        candidate = self._candidate(prepared)
        family_id = candidate[0] if candidate else self._new_family(event, prepared, fp)
        self._add(family_id, event, prepared, fp, {"score": candidate[1]} if candidate else None)
        self.family_of[key] = family_id
        return family_id

    def rebuild(self, events: Iterable[Any]) -> dict[str, dict]:
        """Recompute families from immutable events using deterministic order."""
        self._reset()
        if isinstance(events, Mapping):
            events = events.values()
        ordered = sorted(list(events), key=lambda event: (self.cache.prepared(event).fingerprint,
                                                         _identity(event)))
        for event in ordered:
            self.assign(event)
        return self.families


__all__ = ["FINGERPRINT_VERSION", "FeatureCache", "StructuralGrouper", "classify", "classify_event",
           "compare_events", "extract_features", "extract_signal_features", "fingerprint"]
