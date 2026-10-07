"""What a recorded OOK burst is: the Python port of the Flipper's ``rf_decode.c``.

The Flipper app decodes every capture before it saves the record (``rf_protocol``, ``rf_key``,
``rf_info`` ...).  The companion runs the same decoder over the pulse timings of records that
were made by older apps, and turns a decode into the sentences the analyzer shows.

The two implementations are kept line for line in step: ``uplink/tests/test_rf_decode.py``
synthesises every protocol and checks that the compiled C code and this module agree.

Record timings are durations only.  The level of the first one is in ``first_level`` for records
from app 1.5.0 on; without it both polarities are tried and the better decode wins.
"""
from __future__ import annotations

from typing import Iterable, Optional, Sequence

NONE, OOK, PRINCETON, CAME, NICE_FLO, NICE_FLOR_S, KEELOQ, STARLINE, LINEAR, HORMANN, GATE_TX, \
    FAAC_SLH, HOLTEK, NEXUS = range(14)

PROTOCOL_NAMES = {
    NONE: "carrier", OOK: "OOK", PRINCETON: "Princeton", CAME: "CAME", NICE_FLO: "Nice FLO",
    NICE_FLOR_S: "Nice FloR-S", KEELOQ: "KeeLoq", STARLINE: "Starline", LINEAR: "Linear",
    HORMANN: "Hormann", GATE_TX: "GateTX", FAAC_SLH: "FAAC SLH", HOLTEK: "Holtek", NEXUS: "Nexus-TH",
}
NAME_TO_PROTOCOL = {name: pid for pid, name in PROTOCOL_NAMES.items()}
ROLLING = {KEELOQ, STARLINE, NICE_FLOR_S, FAAC_SLH}


def _near(d: int, nominal: int, tolerance: int) -> bool:
    return d + tolerance > nominal and d < nominal + tolerance


class _Frame:
    __slots__ = ("key", "bits", "extra", "te_sum", "te_n")

    def __init__(self):
        self.key = 0
        self.bits = 0
        self.extra = 0
        self.te_sum = 0
        self.te_n = 0

    def add_bit(self, bit: bool):
        if self.bits < 64:
            self.key = ((self.key << 1) | (1 if bit else 0)) & 0xFFFFFFFFFFFFFFFF
        elif self.bits < 72:
            self.extra = ((self.extra << 1) | (1 if bit else 0)) & 0xFF
        self.bits += 1


class _Spec:
    __slots__ = ("te_short", "te_long", "tol_short", "tol_long", "end_min", "first_high",
                 "short_first_bit", "symmetric", "end_first", "last_pair_is_bit")

    def __init__(self, te_short, te_long, tol_short, tol_long, end_min, first_high, short_first_bit,
                 symmetric, end_first, last_pair_is_bit):
        self.te_short, self.te_long = te_short, te_long
        self.tol_short, self.tol_long = tol_short, tol_long
        self.end_min = end_min
        self.first_high, self.short_first_bit = first_high, short_first_bit
        self.symmetric, self.end_first, self.last_pair_is_bit = symmetric, end_first, last_pair_is_bit


def _read_pairs(levels, durs, i, p: _Spec, f: _Frame):
    """Returns the index of the ending pair's first element (or after Linear's last bit), or None."""
    n = len(durs)
    while i + 1 < n:
        a, b = durs[i], durs[i + 1]
        if levels[i] != p.first_high or levels[i + 1] == p.first_high:
            return None
        if p.end_first and a >= p.end_min:
            return i
        if not p.end_first and b >= p.end_min:
            if p.last_pair_is_bit:
                if _near(a, p.te_short, p.tol_short):
                    f.add_bit(p.short_first_bit)
                elif _near(a, p.te_long, p.tol_long):
                    f.add_bit(not p.short_first_bit)
                else:
                    return None
                return i + 1
            return i
        a_short = _near(a, p.te_short, p.tol_short)
        a_long = _near(a, p.te_long, p.tol_long)
        b_short = _near(b, p.te_short, p.tol_short)
        b_long = _near(b, p.te_long, p.tol_long)
        if p.symmetric:
            if a_short and b_short:
                f.add_bit(False)
            elif a_long and b_long:
                f.add_bit(True)
            else:
                return None
        elif a_short and b_long:
            f.add_bit(p.short_first_bit)
        elif a_long and b_short:
            f.add_bit(not p.short_first_bit)
        else:
            return None
        f.te_sum += a if a_short else (a // 2 if p.symmetric else b)
        f.te_n += 1
        if f.bits > 80:
            return None
        i += 2
    return None


# --------------------------------------------------------------------------- protocols
_CAME = _Spec(320, 640, 150, 150, 320 * 4, False, False, False, True, False)
_NICE_FLO = _Spec(700, 1400, 250, 250, 700 * 4, False, False, False, True, False)
_HOLTEK = _Spec(430, 870, 100, 200, 430 * 10 + 100, False, False, False, True, False)
_GATE_TX = _Spec(350, 700, 100, 300, 350 * 10 + 100, False, False, False, True, False)
_KEELOQ = _Spec(400, 800, 180, 360, 400 * 2 + 180, True, True, False, False, False)
_STARLINE = _Spec(250, 500, 120, 120, 500 + 120, True, False, True, True, False)
_NICE_FLOR_S = _Spec(500, 1000, 300, 300, 1500 - 300, True, False, False, True, False)
_LINEAR = _Spec(500, 1500, 350, 350, 500 * 5, True, False, False, False, True)
_HORMANN = _Spec(500, 1000, 200, 200, 500 * 5, True, False, False, True, False)
_FAAC = _Spec(255, 595, 100, 100, 255 * 3 + 100, True, False, False, True, False)


def _dec_princeton(lv, du, i, f):
    n = len(du)
    if lv[i] or not _near(du[i], 390 * 36, 300 * 36):
        return None
    te = 0
    i += 1
    while i + 1 < n:
        if not lv[i] or lv[i + 1]:
            return None
        a, b = du[i], du[i + 1]
        if b >= (te * 6 if te else 2340):
            return i if f.bits == 24 else None
        sh, lg = (a, b) if a < b else (b, a)
        if sh < 90 or sh > 690 or lg * 10 < sh * 22 or lg > sh * 4 + 200:
            return None
        if not te:
            te = sh
        elif sh * 10 < te * 6 or sh * 10 > te * 14:
            return None
        f.add_bit(a > b)
        f.te_sum += sh
        f.te_n += 1
        if f.bits > 24:
            return None
        i += 2
    return None


def _dec_came(lv, du, i, f):
    n = len(du)
    if i + 1 >= n or lv[i] or not _near(du[i], 320 * 56, 150 * 63):
        return None
    if not lv[i + 1] or not _near(du[i + 1], 320, 150):
        return None
    nxt = _read_pairs(lv, du, i + 2, _CAME, f)
    return nxt if nxt is not None and f.bits in (12, 18, 24, 25) else None


def _dec_nice_flo(lv, du, i, f):
    n = len(du)
    if i + 1 >= n or lv[i] or not _near(du[i], 700 * 36, 250 * 29):
        return None
    if not lv[i + 1] or not _near(du[i + 1], 700, 250):
        return None
    nxt = _read_pairs(lv, du, i + 2, _NICE_FLO, f)
    return nxt if nxt is not None and 12 <= f.bits <= 24 else None


def _dec_holtek(lv, du, i, f):
    n = len(du)
    if i + 1 >= n or lv[i] or not _near(du[i], 430 * 36, 100 * 36):
        return None
    if not lv[i + 1] or not _near(du[i + 1], 430, 100):
        return None
    nxt = _read_pairs(lv, du, i + 2, _HOLTEK, f)
    if nxt is None or f.bits != 40:
        return None
    return nxt if (f.key >> 36) == 0x5 else None


def _dec_gate_tx(lv, du, i, f):
    n = len(du)
    if i + 1 >= n or lv[i] or not _near(du[i], 350 * 47, 100 * 47):
        return None
    if not lv[i + 1] or not _near(du[i + 1], 700, 300):
        return None
    nxt = _read_pairs(lv, du, i + 2, _GATE_TX, f)
    return nxt if nxt is not None and f.bits == 24 else None


def _dec_keeloq(lv, du, i, f):
    n = len(du)
    count = 0
    while (i + 1 < n and lv[i] and _near(du[i], 400, 180) and not lv[i + 1]
           and _near(du[i + 1], 400, 180)):
        count += 1
        i += 2
    if count < 3 or i + 1 >= n:
        return None
    if not (lv[i] and _near(du[i], 400, 180) and not lv[i + 1] and _near(du[i + 1], 400 * 10, 180 * 10)):
        return None
    nxt = _read_pairs(lv, du, i + 2, _KEELOQ, f)
    return nxt if nxt is not None and 64 <= f.bits <= 67 else None


def _dec_starline(lv, du, i, f):
    n = len(du)
    count = 0
    while (i + 1 < n and lv[i] and _near(du[i], 1000, 240) and not lv[i + 1]
           and _near(du[i + 1], 1000, 240)):
        count += 1
        i += 2
    if count < 5 or i >= n or not lv[i]:
        return None
    nxt = _read_pairs(lv, du, i, _STARLINE, f)
    return nxt if nxt is not None and 64 <= f.bits <= 66 else None


def _dec_nice_flor_s(lv, du, i, f):
    n = len(du)
    if i + 2 >= n or lv[i] or not _near(du[i], 500 * 38, 300 * 38):
        return None
    if not lv[i + 1] or not _near(du[i + 1], 1500, 900):
        return None
    if lv[i + 2] or not _near(du[i + 2], 1500, 900):
        return None
    nxt = _read_pairs(lv, du, i + 3, _NICE_FLOR_S, f)
    return nxt if nxt is not None and f.bits in (52, 72) else None


def _dec_linear(lv, du, i, f):
    if lv[i] or not _near(du[i], 500 * 42, 350 * 15):
        return None
    nxt = _read_pairs(lv, du, i + 1, _LINEAR, f)
    return nxt if nxt is not None and f.bits == 10 else None


def _dec_hormann(lv, du, i, f):
    n = len(du)
    if i + 1 >= n or not lv[i] or not _near(du[i], 500 * 24, 200 * 24):
        return None
    if lv[i + 1] or not _near(du[i + 1], 500, 200):
        return None
    nxt = _read_pairs(lv, du, i + 2, _HORMANN, f)
    return nxt if nxt is not None and 44 <= f.bits <= 48 else None


def _dec_faac_slh(lv, du, i, f):
    n = len(du)
    if i + 1 >= n or not lv[i] or not _near(du[i], 1190, 300):
        return None
    if lv[i + 1] or not _near(du[i + 1], 1190, 300):
        return None
    nxt = _read_pairs(lv, du, i + 2, _FAAC, f)
    return nxt if nxt is not None and f.bits == 64 else None


def _dec_nexus(lv, du, i, f):
    n = len(du)
    if i + 1 >= n or not lv[i] or not _near(du[i], 500, 250):
        return None
    if lv[i + 1] or not _near(du[i + 1], 4000, 1200):
        return None
    i += 2
    while i + 1 < n:
        a, b = du[i], du[i + 1]
        if not lv[i] or lv[i + 1] or not _near(a, 500, 250):
            return None
        if b >= 3000:
            return i if f.bits == 36 else None
        if _near(b, 1000, 350):
            f.add_bit(False)
        elif _near(b, 2000, 500):
            f.add_bit(True)
        else:
            return None
        f.te_sum += a
        f.te_n += 1
        if f.bits > 36:
            return None
        i += 2
    return None


# the C table order: preference when two grammars fit equally well
_PROTOCOLS = (
    (KEELOQ, _dec_keeloq), (STARLINE, _dec_starline), (NICE_FLOR_S, _dec_nice_flor_s),
    (FAAC_SLH, _dec_faac_slh), (NEXUS, _dec_nexus), (PRINCETON, _dec_princeton),
    (HOLTEK, _dec_holtek), (GATE_TX, _dec_gate_tx), (NICE_FLO, _dec_nice_flo), (CAME, _dec_came),
    (LINEAR, _dec_linear), (HORMANN, _dec_hormann),
)


# --------------------------------------------------------------------------- descriptions
def _reverse64(key: int) -> int:
    rev = 0
    for i in range(64):
        rev = (rev << 1) | ((key >> i) & 1)
    return rev


def _princeton_info(key: int) -> str:
    key &= 0xFFFFFF
    low = key & 0xFF
    if low in (0x30, 0xC0, 0x03, 0x0C):
        return "sn %04X btn %02X" % (key >> 8, low)
    return "sn %05X btn %X" % (key >> 4, key & 0xF)


def _keeloq_info(key: int) -> str:
    rev = _reverse64(key)
    return "sn %07X btn %X" % ((rev >> 32) & 0x0FFFFFFF, rev >> 60)


def _starline_info(key: int) -> str:
    fix = key >> 32
    return "sn %06X btn %02X" % (fix & 0xFFFFFF, fix >> 24)


def nexus_fields(key: int) -> dict:
    temp = (key >> 12) & 0xFFF
    if temp & 0x800:
        temp -= 0x1000
    return {"id": (key >> 28) & 0xFF, "battery_ok": bool((key >> 27) & 1),
            "channel": ((key >> 24) & 0x3) + 1, "temperature_c": temp / 10.0, "humidity": key & 0xFF}


def _nexus_info(key: int) -> str:
    f = nexus_fields(key)
    temp = int(round(f["temperature_c"] * 10))
    sign = "-" if temp < 0 else "+"
    temp = abs(temp)
    return "%s%d.%dC %d%% ch%d id%02X%s" % (sign, temp // 10, temp % 10, f["humidity"], f["channel"],
                                            f["id"], "" if f["battery_ok"] else " LOW BAT")


def _key_info(key: int, bits: int) -> str:
    if bits > 32:
        return "key %08X%08X" % (key >> 32, key & 0xFFFFFFFF)
    return "key %0*X" % ((bits + 3) // 4, key)


def _describe_ook(levels, durs) -> dict:
    buckets, bucket_us = 64, 50
    hist = [0] * buckets
    total = 0
    for us in durs:
        if us < buckets * bucket_us:
            hist[us // bucket_us] += 1
            total += 1
    te = 0
    for b in range(1, buckets):
        c = hist[b] + (hist[b + 1] if b + 1 < buckets else 0)
        if c >= 3 and c * 100 >= total * 12:
            te = b * bucket_us + bucket_us
            break
    if not te:
        te = 500
    inside = [us for us in durs if us * 4 >= te * 3 and us * 4 <= te * 5]
    if inside:
        te = sum(inside) // len(inside)
    gap = max(te * 6, 2500)
    symbols = frames = identical = 0
    hash_ = 2166136261
    first_hash = first_len = cur_len = 0
    n = len(durs)
    for i, us in enumerate(durs):
        separator = (not levels[i]) and us >= gap
        if separator or i + 1 == n:
            if not separator:
                cur_len += 1
                q = (us * 2 + te) // (te * 2)
                hash_ = ((hash_ ^ q) * 16777619) & 0xFFFFFFFF
            if cur_len >= 4:
                frames += 1
                if frames == 1:
                    first_hash, first_len = hash_, cur_len
                elif hash_ == first_hash and cur_len == first_len:
                    identical += 1
            hash_ = 2166136261
            cur_len = 0
            continue
        cur_len += 1
        symbols += 1
        q = (us * 2 + te) // (te * 2)
        hash_ = ((hash_ ^ q) * 16777619) & 0xFFFFFFFF
    bits = min(255, symbols // 2)
    frames = min(255, frames)
    identical = min(255, identical)
    if frames >= 2:
        info = "te %dus %dsym x%d %s" % (te, bits, frames, "same" if identical + 1 >= frames else "differ")
    else:
        info = "te %dus %dsym" % (te, bits)
    return {"protocol": OOK, "name": "OOK", "info": info, "bits": bits, "key": 0, "frames": frames,
            "identical": identical, "te_us": te, "rolling": False, "confidence": 20}


def _decode_levels(levels: Sequence[bool], durs: Sequence[int]) -> dict:
    n = len(durs)
    if n < 8:
        return {"protocol": NONE, "name": "carrier", "info": "no OOK data", "bits": 0, "key": 0,
                "frames": 0, "identical": 0, "te_us": 0, "rolling": False, "confidence": 0}
    best = None
    best_first = None
    best_frames = best_identical = 0
    for pid, fn in _PROTOCOLS:
        first = None
        frames = identical = 0
        i = 0
        while i < n:
            f = _Frame()
            nxt = fn(levels, durs, i, f)
            if nxt is not None and nxt > i:
                frames += 1
                if frames == 1:
                    first = f
                else:
                    if f.key == first.key and f.bits == first.bits:
                        identical += 1
                    first.te_sum += f.te_sum
                    first.te_n += f.te_n
                i = nxt
            else:
                i += 1
        if frames > best_frames:
            best, best_first, best_frames, best_identical = pid, first, frames, identical
    if best is None:
        return _describe_ook(levels, durs)
    bits, key = best_first.bits, best_first.key
    if best == PRINCETON:
        info = _princeton_info(key)
    elif best == KEELOQ:
        info = _keeloq_info(key)
    elif best == STARLINE:
        info = _starline_info(key)
    elif best == NEXUS:
        info = _nexus_info(key)
    elif best == NICE_FLOR_S:
        info = "encrypted, %d bits" % bits
    elif best == FAAC_SLH:
        info = "sn %07X" % ((key >> 32) & 0x0FFFFFFF)
    else:
        info = _key_info(key, bits)
    return {"protocol": best, "name": PROTOCOL_NAMES[best], "info": info, "bits": bits, "key": key,
            "frames": min(255, best_frames), "identical": min(255, best_identical),
            "te_us": best_first.te_sum // best_first.te_n if best_first.te_n else 0,
            "rolling": best in ROLLING,
            "confidence": 90 if best_frames >= 2 else (75 if bits >= 40 else 60)}


def decode(timings: Iterable[int], first_level: Optional[int] = None) -> dict:
    """Decode durations (microseconds, alternating levels).  ``first_level`` is the level of the
    first duration (1 = carrier on); ``None`` tries both and keeps the better decode."""
    durs = [int(t) for t in timings]
    if first_level is None:
        a = decode(durs, 1)
        b = decode(durs, 0)
        return a if _rank(a) >= _rank(b) else b
    start = bool(first_level)
    levels = [start if i % 2 == 0 else not start for i in range(len(durs))]
    result = _decode_levels(levels, durs)
    result["first_level"] = 1 if start else 0
    return result


def _rank(d: dict) -> tuple:
    named = d["protocol"] not in (NONE, OOK)
    return (1 if named else 0, d["frames"], d["identical"], d["confidence"])


def identity(d: dict) -> int:
    """What identifies the transmitter across presses (``rf_decode_identity``)."""
    pid, key = d.get("protocol"), d.get("key", 0)
    if pid == KEELOQ:
        return (_reverse64(key) >> 32) & 0x0FFFFFFF
    if pid == STARLINE:
        return (key >> 32) & 0xFFFFFF
    if pid == FAAC_SLH:
        return (key >> 32) & 0x0FFFFFFF
    if pid == NEXUS:
        return (key >> 24) & 0xFF3
    if pid in (PRINCETON, CAME, NICE_FLO, LINEAR, HORMANN, GATE_TX, HOLTEK):
        return key if key else 1
    return 0


def label(d: dict) -> str:
    """The short Flipper-screen label: ``KeeLoq 66b``."""
    pid = d.get("protocol")
    if pid == OOK:
        return "OOK %dsym" % d.get("bits", 0)
    if pid in (NONE, NEXUS):
        return d.get("name", "")
    return "%s %db" % (d.get("name", ""), d.get("bits", 0))


# --------------------------------------------------------------------------- sentences
_WHAT = {
    PRINCETON: ("fixed-code remote (Princeton PT2262 / EV1527)",
                "Cheap 433/315 MHz remotes, doorbells, car alarm pagers and light switches use this chip "
                "family. The code never changes: {key}."),
    CAME: ("CAME gate remote (or a Holtek HT12E remote: the same frame)",
           "A 12-bit fixed code set by DIP switches, as in CAME TOP-432 and many clones."),
    NICE_FLO: ("Nice FLO gate remote",
               "A 12-bit fixed code set by DIP switches."),
    NICE_FLOR_S: ("Nice FloR-S rolling-code remote",
                  "The frame is encrypted and changes with every press; the serial stays hidden."),
    KEELOQ: ("KeeLoq rolling-code remote",
             "Gate openers (DoorHan, AN-Motors, Nice Smilo...), car alarms and garage doors. The serial and "
             "the button are in the clear; the 32-bit hopping code changes with every press, so this "
             "cannot be replayed."),
    STARLINE: ("Starline car alarm remote (rolling code)",
               "The 24-bit serial and the button are in the clear; the other half changes every press."),
    LINEAR: ("Linear garage remote (315 MHz, 10 DIP switches)", "A fixed 10-bit code."),
    HORMANN: ("Hormann HSM garage remote (868 MHz)", "A fixed 44-bit code."),
    GATE_TX: ("GateTX gate remote", "A fixed 24-bit code."),
    FAAC_SLH: ("FAAC SLH rolling-code remote", "The serial is in the clear; the rest changes every press."),
    HOLTEK: ("Holtek 40-bit remote", "A fixed 40-bit code (header 0x5)."),
    NEXUS: ("Nexus / Rubicson weather sensor",
            "Outdoor thermometer-hygrometers send this every minute or so: {reading}."),
}


def describe(d: dict, frequency_hz: int = 0) -> tuple:
    """``(headline, sentences)`` for the analyzer: what the transmitter is, in plain words."""
    pid = d.get("protocol")
    frames, identical = d.get("frames", 0), d.get("identical", 0)
    repeat = ""
    if frames >= 2:
        repeat = (" The frame repeats %d times in this capture%s." %
                  (frames, ", every copy identical" if identical + 1 >= frames else ", not all copies alike"))
    if pid == NONE:
        return ("Carrier without OOK data",
                "The receiver saw a carrier but no on/off keying it could follow. FSK/GFSK transmitters "
                "(car keys, TPMS, LoRa, many weather stations) and continuous jam-like carriers look like this.")
    if pid == OOK:
        bits = d.get("bits", 0)
        if frames >= 2 and identical + 1 >= frames:
            kind = "an unknown fixed-code transmitter"
            tail = "Every repeat carries the same bits, so the code does not change between presses."
        elif frames >= 2:
            kind = "an unknown transmitter"
            tail = "The repeats differ: a rolling code, a sensor packet or a noisy capture."
        else:
            kind = "an unknown OOK burst"
            tail = "One frame only; a second capture would tell whether the code is fixed."
        return ("Unknown OOK: %s" % kind,
                "Base pulse about %d us, about %d symbols per frame.%s %s" % (d.get("te_us", 0), bits, repeat, tail))
    what, text = _WHAT.get(pid, (d.get("name", ""), ""))
    key = d.get("key", 0)
    if pid == PRINCETON:
        text = text.format(key=_princeton_info(key).replace("sn", "serial").replace("btn", "button"))
    elif pid == NEXUS:
        f = nexus_fields(key)
        text = text.format(reading="%.1f C, %d %% humidity, channel %d, sensor id %02X%s" % (
            f["temperature_c"], f["humidity"], f["channel"], f["id"],
            "" if f["battery_ok"] else ", battery low"))
    head = "%s %d-bit: %s" % (d.get("name", ""), d.get("bits", 0), what) if pid != NEXUS else \
        "%s: %s" % (d.get("name", ""), what)
    return head, (text + repeat).strip()


def from_record(event) -> dict:
    """The decode of an analyzer event: the Flipper's own when it has one, otherwise computed here."""
    name = getattr(event, "rf_protocol", "") or ""
    if name:
        pid = NAME_TO_PROTOCOL.get(name, OOK)
        try:
            key = int(getattr(event, "rf_key", "") or "0", 16)
        except ValueError:
            key = 0
        return {"protocol": pid, "name": name, "info": getattr(event, "rf_info", "") or "",
                "bits": int(getattr(event, "rf_bits", 0) or 0), "key": key,
                "frames": int(getattr(event, "rf_frames", 0) or 0),
                "identical": int(getattr(event, "rf_identical", 0) or 0),
                "te_us": int(getattr(event, "rf_te_us", 0) or 0), "rolling": pid in ROLLING,
                "confidence": int(getattr(event, "rf_confidence", 0) or 0),
                "first_level": getattr(event, "first_level", -1)}
    first = getattr(event, "first_level", -1)
    return decode(getattr(event, "pulse_timings_us", ()) or (), None if first in (-1, None) else first)
