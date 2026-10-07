"""Protocol recognition: the C decoder of the Flipper app (compiled on the host) and its
Python port must agree on every synthesised burst, and both must name the right remote."""
import json
import os
import random
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from uplink import rf_decode  # noqa: E402
from test_rf_native_reliability import _build  # noqa: E402


@pytest.fixture(scope="module")
def decoder(tmp_path_factory):
    return _build(tmp_path_factory, "rf_decode_harness", ["decode_harness.c"], ["rf_decode.c"])


def run_c(decoder, bursts):
    """bursts: list of (first_level, [durations]) -> list of decode dicts from the C code."""
    text = "".join("%d %s\n" % (first, " ".join(str(d) for d in durs)) for first, durs in bursts)
    ran = subprocess.run([str(decoder)], input=text, capture_output=True, text=True, timeout=60)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    return [json.loads(line) for line in ran.stdout.splitlines() if line.strip()]


# --------------------------------------------------------------------------- synthesis
# every synthesiser returns (first_level, durations) with alternating levels, carrier on = 1
def _pairs(first_high, pairs):
    """[(a, b), ...] pairs of (first element, second element) -> flat durations."""
    out = []
    for a, b in pairs:
        out += [a, b]
    return 1 if first_high else 0, out


def jitter(durs, rnd, pct=0.08):
    return [max(1, int(round(d * (1 + rnd.uniform(-pct, pct))))) for d in durs]


def bits_of(value, count):
    return [(value >> (count - 1 - i)) & 1 for i in range(count)]


def princeton(code, te=400, repeats=3):
    pairs = []
    for _ in range(repeats):
        pairs.append((te, te * 31))               # sync: high te, low 31 te
        for bit in bits_of(code, 24):
            pairs.append((te * 3, te) if bit else (te, te * 3))
    pairs.append((te, te * 31))
    return _pairs(True, pairs)


def came(code, bits=12, te=320, repeats=3):
    durs = []
    for _ in range(repeats):
        durs += [te * 36, te]                     # sync low, start bit high
        for bit in bits_of(code, bits):
            durs += [te * 2, te] if bit else [te, te * 2]   # (low, high)
    durs += [te * 36, te]
    return 0, durs


def nice_flo(code, repeats=3):
    return came(code, bits=12, te=700, repeats=repeats)


def holtek(code, repeats=2):
    code = (0x5 << 36) | (code & 0xFFFFFFFFF)
    return came(code, bits=40, te=430, repeats=repeats)


def gate_tx(code, repeats=2):
    te = 350
    durs = []
    for _ in range(repeats):
        durs += [te * 47, te * 2]
        for bit in bits_of(code, 24):
            durs += [te * 2, te] if bit else [te, te * 2]
    durs += [te * 47, te * 2]
    return 0, durs


def keeloq(serial, button, hop, repeats=2, vlow=0):
    te = 400
    stream = bits_of(hop, 32)[::-1] + bits_of(serial, 28)[::-1] + bits_of(button, 4)[::-1] + [vlow, 0]
    durs = []
    for _ in range(repeats):
        durs += [te, te] * 11 + [te, te * 10]    # preamble, header
        for bit in stream:
            durs += [te, te * 2] if bit else [te * 2, te]   # (high, low): short high = 1
        durs += [te, te * 40]                     # guard
    return 1, durs


def starline(serial, button, hop, repeats=2):
    te = 250
    fix = (button << 24) | (serial & 0xFFFFFF)
    stream = bits_of(fix, 32) + bits_of(hop, 32)
    durs = []
    for _ in range(repeats):
        durs += [1000, 1000] * 6
        for bit in stream:
            durs += [te * 2, te * 2] if bit else [te, te]
        durs += [te * 2 + 300, 5000]             # end: a high longer than te_long + delta
    return 1, durs


def nice_flor_s(code, repeats=2):
    te = 500
    durs = []
    for _ in range(repeats):
        durs += [te * 38, te * 3, te * 3]
        for bit in bits_of(code, 52):
            durs += [te * 2, te] if bit else [te, te * 2]
        durs += [te * 3, te * 38]
    return 0, durs


def linear(code, repeats=3):
    te = 500
    durs = []
    for _ in range(repeats):
        durs += [te * 42]
        pairs = [(te * 3, te) if bit else (te, te * 3) for bit in bits_of(code, 10)]
        for a, b in pairs[:-1]:
            durs += [a, b]
        durs += [pairs[-1][0]]                    # the last bit's low is the next guard
    durs += [te * 42]
    return 0, durs


def hormann(code, repeats=2):
    te = 500
    durs = []
    for _ in range(repeats):
        durs += [te * 24, te]
        for bit in bits_of(code, 44):
            durs += [te * 2, te] if bit else [te, te * 2]
    durs += [te * 24, te]
    return 1, durs


def faac_slh(serial, hop, repeats=2):
    durs = []
    code = (serial << 32) | hop
    for _ in range(repeats):
        durs += [1190, 1190]
        for bit in bits_of(code, 64):
            durs += [595, 255] if bit else [255, 595]
    durs += [1190, 1190]
    return 1, durs


def nexus(sensor_id, channel, temp_c, humidity, battery_ok=True, repeats=4):
    temp = int(round(temp_c * 10)) & 0xFFF
    value = (sensor_id << 28) | (int(battery_ok) << 27) | ((channel - 1) << 24) | (temp << 12) | (0xF << 8) | humidity
    durs = []
    for _ in range(repeats):
        durs += [500, 4000]
        for bit in bits_of(value, 36):
            durs += [500, 2000] if bit else [500, 1000]
    durs += [500, 4000]
    return 1, durs


def unknown_fixed(rnd, te=420, symbols=25, repeats=4):
    frame = [(te, te * 3) if rnd.random() < 0.5 else (te * 3, te) for _ in range(symbols)]
    durs = []
    for _ in range(repeats):
        for a, b in frame:
            durs += [a, b]
        durs += [te, te * 20]
    return 1, durs


def noise(rnd, count=60):
    return 1, [rnd.randint(30, 900) for _ in range(count)]


CASES = {
    "princeton": (princeton(0x1A2B3C), "Princeton", 24, "sn 1A2B3 btn C"),
    "princeton_8bit_btn": (princeton(0x5A7E30), "Princeton", 24, "sn 5A7E btn 30"),
    "came12": (came(0xA5B), "CAME", 12, "key A5B"),
    "came24": (came(0x123ABC, bits=24), "CAME", 24, "key 123ABC"),
    "nice_flo": (nice_flo(0x3C5), "Nice FLO", 12, "key 3C5"),
    "holtek": (holtek(0x1234567AB), "Holtek", 40, "key 00000051234567AB"),
    "gate_tx": (gate_tx(0xC0FFEE), "GateTX", 24, "key C0FFEE"),
    "keeloq": (keeloq(0x0ABCDEF, 0x2, 0x8F3A1C77), "KeeLoq", 66, "sn 0ABCDEF btn 2"),
    "keeloq_once": (keeloq(0x0123456, 0x4, 0x11223344, repeats=1), "KeeLoq", 66, "sn 0123456 btn 4"),
    "starline": (starline(0x123456, 0x01, 0xDEADBEEF), "Starline", 64, "sn 123456 btn 01"),
    "nice_flor_s": (nice_flor_s(0x5F0E3A1B2C4D5), "Nice FloR-S", 52, "encrypted, 52 bits"),
    "linear": (linear(0x2A5), "Linear", 10, "key 2A5"),
    "hormann": (hormann(0xF0A5B3C7D9E), "Hormann", 44, "key 00000F0A5B3C7D9E"),
    "faac_slh": (faac_slh(0x1234567, 0xABCDEF01), "FAAC SLH", 64, "sn 1234567"),
    "nexus": (nexus(0x3A, 2, 23.4, 45), "Nexus-TH", 36, "+23.4C 45% ch2 id3A"),
    "nexus_cold": (nexus(0x7F, 1, -5.6, 88, battery_ok=False), "Nexus-TH", 36, "-5.6C 88% ch1 id7F LOW BAT"),
}


# --------------------------------------------------------------------------- tests
def test_python_names_every_protocol():
    for name, (burst, proto, bits, info) in CASES.items():
        first, durs = burst
        d = rf_decode.decode(durs, first)
        assert d["name"] == proto, (name, d)
        assert d["bits"] == bits, (name, d)
        assert d["info"] == info, (name, d)
        assert d["frames"] >= 1


def test_python_finds_the_polarity_by_itself():
    first, durs = CASES["keeloq"][0]
    assert rf_decode.decode(durs)["name"] == "KeeLoq"
    first, durs = CASES["came12"][0]
    assert rf_decode.decode(durs)["info"] == "key A5B"


def test_python_describes_unknown_and_noise():
    rnd = random.Random(7)
    d = rf_decode.decode(*reversed(unknown_fixed(rnd)))
    assert d["name"] == "OOK" and d["frames"] == 4 and d["identical"] == 3
    assert d["info"].endswith("same") and 400 <= d["te_us"] <= 440
    head, text = rf_decode.describe(d)
    assert "fixed-code" in head and "does not change" in text
    d = rf_decode.decode(*reversed(noise(rnd)))
    assert d["name"] == "OOK"
    d = rf_decode.decode([500, 500, 500], 1)
    assert d["name"] == "carrier"
    head, text = rf_decode.describe(d)
    assert "Carrier" in head


def test_describe_keeloq_and_sensor():
    d = rf_decode.decode(*reversed(CASES["keeloq"][0]))
    head, text = rf_decode.describe(d)
    assert head.startswith("KeeLoq 66-bit") and "cannot be replayed" in text and "repeats 2 times" in text
    assert rf_decode.identity(d) == 0x0ABCDEF
    d = rf_decode.decode(*reversed(CASES["nexus"][0]))
    head, text = rf_decode.describe(d)
    assert "23.4 C, 45 % humidity, channel 2" in text
    assert rf_decode.label(d) == "Nexus-TH"
    assert rf_decode.nexus_fields(d["key"])["temperature_c"] == 23.4


def test_jittered_bursts_still_decode():
    rnd = random.Random(42)
    for name, (burst, proto, bits, info) in CASES.items():
        first, durs = burst
        for _ in range(5):
            d = rf_decode.decode(jitter(durs, rnd, 0.07), first)
            assert d["name"] == proto and d["bits"] == bits, (name, d)
            assert d["info"] == info, (name, d)


def test_c_and_python_agree(decoder):
    rnd = random.Random(3)
    bursts = [burst for burst, *_ in CASES.values()]
    bursts += [(first, jitter(durs, rnd, 0.07)) for first, durs in bursts]
    bursts += [unknown_fixed(rnd), unknown_fixed(rnd, te=300, symbols=40, repeats=2), noise(rnd),
               noise(rnd, 300), (1, [500, 500, 500]), (0, []), (1, [1] * 600)]
    # whole-burst polarity flips: the decoders must not crash on either level order
    bursts += [(1 - first, durs) for first, durs in bursts[:len(CASES)]]
    expected = [rf_decode.decode(durs, first) for first, durs in bursts]
    got = run_c(decoder, bursts)
    assert len(got) == len(bursts)
    for case, (c, py) in enumerate(zip(got, expected)):
        for key in ("protocol", "name", "info", "bits", "key", "frames", "identical", "te_us", "rolling",
                    "confidence"):
            assert c[key] == py[key], (case, key, c, py)
        assert c["identity"] == rf_decode.identity(py), case
        assert c["label"] == rf_decode.label(py), case


def test_c_names_every_protocol(decoder):
    names = list(CASES)
    got = run_c(decoder, [CASES[n][0] for n in names])
    for name, d in zip(names, got):
        burst, proto, bits, info = CASES[name]
        assert (d["name"], d["bits"], d["info"]) == (proto, bits, info), (name, d)
