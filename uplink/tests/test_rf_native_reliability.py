"""Compile the DedSec Uplink RF engine (apps/dedsec_uplink/rf_*.c) on the host
and exercise the production C code:

* journal crash recovery against a storage fake that copies on rename like the
  Unleashed firmware and can cut power in the middle of any write;
* record JSON produced from edge-case inputs (json.loads, time fields, caps);
* the RL/RR/RA line protocol driven like the PC companion, checking sizes,
  base64 chunks and binascii.crc32;
* the whole engine (worker loop, radio/NFC control, burst detection) in a
  deterministic fake world whose HAL aborts on every call order the firmware
  would furi_check() or deadlock on.
"""
import base64
import binascii
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

LINE_MAX = 243  # one BLE notification including the newline

REQUIRED_FIELDS = {
    "schema_version", "event_id", "device_uuid", "session_id", "sequence_number",
    "captured_at_utc", "captured_at_unix", "timezone_offset_minutes", "rtc_local_unix",
    "monotonic_ms", "source_type", "mode", "frequency_hz", "modulation", "bandwidth_hz",
    "duration_us", "repeat_count", "battery_pct", "fingerprint_id", "family_id",
    "classification", "classification_confidence", "upload_state",
}
SUBGHZ_FIELDS = {
    "follow_profile_id", "follow_similarity", "rssi_min_dbm", "rssi_avg_dbm", "rssi_max_dbm",
    "pulse_count", "last_duration_us", "pulse_timings_us",
}
NFC_FIELDS = {
    "nfc_technology", "nfc_protocol", "nfc_identifier", "nfc_field_duration_ms",
    "nfc_field_count", "nfc_confidence",
}


def _compiler_env():
    """Use an available host C compiler, including a normal Visual Studio setup."""
    for name in ("cc", "gcc", "clang"):
        if shutil.which(name):
            return name, os.environ.copy(), False
    if shutil.which("cl"):
        return "cl", os.environ.copy(), True
    if os.name == "nt":
        roots = [Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
                 Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))]
        for root in roots:
            for script in (root / "Microsoft Visual Studio").glob("*/*/VC/Auxiliary/Build/vcvars64.bat"):
                # This only initializes compiler environment variables.  File
                # changes remain in pytest's temporary build directory.
                command = f'call "{script}" >nul && set'
                result = subprocess.run(f'cmd /d /s /c "{command}"',
                                        capture_output=True, text=True, check=True)
                env = os.environ.copy()
                for line in result.stdout.splitlines():
                    if "=" in line:
                        key, value = line.split("=", 1)
                        env[key] = value
                compiler_path = shutil.which("cl", path=env.get("Path", env.get("PATH")))
                if compiler_path:
                    return compiler_path, env, True
    pytest.skip("host C compiler unavailable")


def _build(tmp_path_factory, name, fixture_files, source_files):
    compiler, env, msvc = _compiler_env()
    root = Path(__file__).resolve().parents[2]
    fixtures = Path(__file__).with_name("native_rf")
    source = root / "apps" / "dedsec_uplink"
    build = tmp_path_factory.mktemp(name)
    executable = build / (name + ".exe" if os.name == "nt" else name)
    files = [str(fixtures / f) for f in fixture_files] + [str(source / f) for f in source_files]
    if msvc:
        command = [compiler, "/nologo", "/std:c11", "/W3", "/D_CRT_SECURE_NO_WARNINGS",
                   f"/I{fixtures}", f"/I{source}", *files, f"/Fe:{executable}"]
    else:
        command = [compiler, "-std=c11", "-Wall", "-Wextra", "-I", str(fixtures),
                   "-I", str(source), *files, "-o", str(executable)]
    built = subprocess.run(command, env=env, cwd=build, capture_output=True, text=True)
    assert built.returncode == 0, built.stdout + built.stderr
    return executable


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    return _build(tmp_path_factory, "rf_harness", ["rf_harness.c", "storage_fake.c"],
                  ["rf_store.c", "rf_record.c", "rf_proto.c"])


@pytest.fixture(scope="module")
def engine_harness(tmp_path_factory):
    # The fake HAL declares no Sub-GHz TX and no NFC poller/listener/field-on
    # function, so this only builds while the engine stays passive.
    return _build(tmp_path_factory, "rf_engine_harness",
                  ["engine_harness.c", "engine_fake.c", "storage_fake.c"],
                  ["rf_engine.c", "rf_capture.c", "rf_store.c", "rf_record.c", "rf_proto.c"])


def test_native_rf_journal_recovers_from_power_cuts_and_faults(harness):
    ran = subprocess.run([str(harness), "store"], capture_output=True, text=True, timeout=300)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert "RF store tests passed" in ran.stdout


def _check_record(text, written):
    assert text.endswith("}\n") and text.count("\n") == 1
    data = json.loads(text)
    assert REQUIRED_FIELDS <= data.keys()
    assert data["schema_version"] == 1 and data["upload_state"] == "pending"
    assert data["family_id"] is None
    assert -14 * 60 <= data["timezone_offset_minutes"] <= 14 * 60
    when = datetime.strptime(data["captured_at_utc"], "%Y-%m-%dT%H:%M:%SZ")
    assert int(when.replace(tzinfo=timezone.utc).timestamp()) == data["captured_at_unix"]
    expected = data["rtc_local_unix"] - data["timezone_offset_minutes"] * 60
    assert data["captured_at_unix"] == min(max(expected, 0), 2 ** 32 - 1)
    if data["source_type"] == "subghz":
        assert SUBGHZ_FIELDS <= data.keys()
        assert data["modulation"] == "OOK"
        assert len(data["pulse_timings_us"]) == written
        assert data["pulse_count"] >= written
        assert 0.0 <= data["follow_similarity"] <= 1.0
        assert data["rssi_min_dbm"] <= data["rssi_avg_dbm"] <= data["rssi_max_dbm"]
    else:
        assert data["source_type"] == "nfc" and NFC_FIELDS <= data.keys()
        assert data["frequency_hz"] == 13560000
    return data


def test_native_rf_records_are_valid_json_within_the_buffer(harness):
    ran = subprocess.run([str(harness), "record"], capture_output=True, timeout=60)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    lines = ran.stdout.decode("utf-8").replace("\r\n", "\n").split("\n")
    cases = {}
    for index in range(0, len(lines) - 1, 2):
        header = lines[index].split()
        assert header[0] == "#case"
        cases[header[1]] = (int(header[2]), int(header[3]), int(header[4]), lines[index + 1] + "\n")
    assert set(cases) == {"typical", "worst", "follow512", "carrier", "nfc", "tiny"}
    for name, (length, written, pulse_count, text) in cases.items():
        if name == "tiny":
            assert length == 0
            continue
        assert len(text.encode("utf-8")) == length < 4096
        data = _check_record(text, written)
        if data["source_type"] == "subghz":
            assert data["pulse_count"] == pulse_count
    worst = json.loads(cases["worst"][3])
    assert 0 < len(worst["pulse_timings_us"]) < 512  # capped to what fits
    assert set(worst["pulse_timings_us"]) == {2147483647}
    assert worst["follow_profile_id"] == "local-ffffffff"
    assert cases["worst"][0] > 4096 - 16
    follow = json.loads(cases["follow512"][3])
    assert len(follow["pulse_timings_us"]) == 512 and follow["pulse_count"] == 600
    assert follow["follow_similarity"] == pytest.approx(0.876)
    assert follow["timezone_offset_minutes"] == 330
    carrier = json.loads(cases["carrier"][3])
    assert carrier["pulse_timings_us"] == [] and carrier["follow_profile_id"] == ""
    assert carrier["captured_at_utc"] == "1970-01-01T00:00:00Z"
    nfc = json.loads(cases["nfc"][3])
    assert nfc["duration_us"] == 2 ** 32 - 1 and nfc["nfc_field_duration_ms"] == 2 ** 32 - 1
    assert nfc["fingerprint_id"] == "local-nfc-field" and nfc["mode"] == "NFC"


class _Flipper:
    """The protocol REPL: PC lines in, Flipper lines out."""

    def __init__(self, executable):
        self.process = subprocess.Popen([str(executable), "proto"], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, bufsize=1)
        ready = self.recv().split()
        assert ready[0] == "#ready"
        self.pending = int(ready[1])
        self.device = ready[2]

    def send(self, line):
        self.process.stdin.write(line + "\n")
        self.process.stdin.flush()

    def recv(self):
        line = self.process.stdout.readline()
        assert line.endswith("\n"), "harness stopped: " + self.process.stderr.read()
        line = line.rstrip("\r\n")
        assert not line.startswith("#overlong"), line
        if not line.startswith("#"):  # harness lines are not protocol lines
            assert len(line.encode("utf-8")) + 1 <= LINE_MAX, line
        return line

    def ask(self, line):
        self.send(line)
        return self.recv()

    def close(self):
        self.send("#quit")
        self.process.stdin.close()
        assert self.process.wait(timeout=30) == 0


def _sync_round(flipper):
    """The companion's loop from the integration contract, section 2."""
    imported = {}
    cursor = 0
    while True:
        reply = flipper.ask(f"RL|{cursor}")
        if reply == "RE":
            return imported
        tag, nxt, event_id, size, crc = reply.split("|")
        assert tag == "RI" and int(nxt) == cursor + 1
        size, crc = int(size), int(crc)
        data = b""
        while len(data) < size:
            offsets = [len(data) + 120 * k for k in range(4) if len(data) + 120 * k < size]
            for offset in offsets:  # pipelined, at most 4 in flight
                flipper.send(f"RR|{event_id}|{offset}")
            for offset in offsets:
                tag, rid, roffset, payload = flipper.recv().split("|")
                assert (tag, rid, int(roffset)) == ("RD", event_id, offset)
                chunk = base64.b64decode(payload, validate=True)
                assert len(chunk) == min(120, size - offset)
                assert len(data) == offset
                data += chunk
        assert binascii.crc32(data) == crc
        record = _check_record(data.decode("utf-8"), len(json.loads(data).get("pulse_timings_us", [])))
        assert record["event_id"] == event_id
        imported[event_id] = data
        assert flipper.ask(f"RA|{event_id}|{size}|{crc}") == f"RK|{event_id}"
        # The record left events/: the cursor stays where it is.


def test_native_rf_sync_protocol_matches_the_contract(harness):
    flipper = _Flipper(harness)
    try:
        assert flipper.pending == 4
        assert flipper.ask("#stats") == "#stats 4 4"
        first = flipper.ask("RL|0").split("|")
        assert first[0] == "RI" and first[1] == "1"
        first_id, first_size, first_crc = first[2], int(first[3]), int(first[4])
        assert first_id.startswith(f"rf-{flipper.device}-")
        assert flipper.ask("RL|3").split("|")[1] == "4"
        assert flipper.ask("RL|4") == "RE"
        assert flipper.ask("RL|4294967294") == "RE"
        for line in ("RL", "RL|", "RL|x", "RL|-1", "RL|4294967296", "RL|1|2", "RR|only-id",
                     "RR|../etc|0", f"RR|{first_id}|x", "RA|id|1", f"RA|{first_id}|1|2|3",
                     "RL|1|2|3|4|5", "RQ|1", "R"):
            reply = flipper.ask(line)
            assert reply.startswith("RX|bad|"), (line, reply)
        assert flipper.ask("RR|ghost-1|0").startswith("RX|nf|ghost-1 ")
        assert flipper.ask("RA|ghost-1|10|20").startswith("RX|nf|ghost-1 ")
        assert flipper.ask(f"RR|{first_id}|{first_size + 1}").startswith(f"RX|bad|{first_id} ")
        assert flipper.ask(f"RR|{first_id}|{first_size}") == f"RD|{first_id}|{first_size}|"
        assert flipper.ask(f"RA|{first_id}|{first_size}|{(first_crc + 1) % 2 ** 32}").startswith(
            f"RX|bad|{first_id} ")
        assert flipper.ask(f"RA|{first_id}|{first_size + 1}|{first_crc}").startswith("RX|bad|")
        assert flipper.ask("#stats") == "#stats 4 4"

        # ACK deletes the records.
        assert flipper.ask("#keep 0") == "#ok"
        imported = _sync_round(flipper)
        assert len(imported) == 4 and first_id in imported
        assert flipper.ask("#stats") == "#stats 0 0"
        crc = binascii.crc32(imported[first_id])
        # A lost RK: repeating the ACK is answered again.
        assert flipper.ask(f"RA|{first_id}|{len(imported[first_id])}|{crc}") == f"RK|{first_id}"
        assert flipper.ask("RL|0") == "RE"

        # ACK keeps a copy in uploaded/.
        assert flipper.ask("#seed sffeeddccbbaa99") == "#ok"
        assert flipper.ask("#stats") == "#stats 4 4"
        assert flipper.ask("#keep 1") == "#ok"
        kept = _sync_round(flipper)
        assert len(kept) == 4 and not set(kept) & set(imported)
        assert flipper.ask("#stats") == "#stats 0 4"
        for event_id, data in kept.items():
            assert flipper.ask(f"#uploaded {event_id}") == "#file " + data.hex()
        assert flipper.ask("#reboot") == "#ok"
        assert flipper.ask("#stats") == "#stats 0 4"
        event_id, data = next(iter(kept.items()))
        crc = binascii.crc32(data)
        assert flipper.ask(f"RA|{event_id}|{len(data)}|{crc}") == f"RK|{event_id}"
        assert flipper.ask(f"RA|{event_id}|{len(data)}|{(crc + 1) % 2 ** 32}").startswith("RX|bad|")
        assert flipper.ask("RL|0") == "RE"
    finally:
        flipper.close()


def test_native_rf_engine_scenarios_keep_the_hal_contract(engine_harness):
    ran = subprocess.run([str(engine_harness)], capture_output=True, timeout=600)
    stdout = ran.stdout.decode("utf-8").replace("\r\n", "\n")
    assert ran.returncode == 0, stdout[-2000:] + ran.stderr.decode("utf-8", "replace")
    assert "RF engine scenarios passed" in stdout
    lines = stdout.split("\n")
    records = {}
    for index, line in enumerate(lines):
        if line.startswith("#record "):
            text = lines[index + 1] + "\n"
            data = _check_record(text, len(json.loads(text).get("pulse_timings_us", [])))
            records.setdefault(line.split()[1], []).append(data)
    assert {name: len(found) for name, found in records.items()} == {
        "bursts": 2, "noise": 1, "noisy_floor": 1, "nfc": 2, "nfc_busy": 1, "follow": 3, "storage_full": 1,
        "gaps": 5, "churn": 1}
    for found in records.values():
        ids = [data["event_id"] for data in found]
        assert len(set(ids)) == len(ids)
        for data in found:
            assert data["event_id"] == "rf-{}-{}-{}".format(
                data["device_uuid"], data["session_id"], data["sequence_number"])
            assert len(data["event_id"]) <= 48 and data["timezone_offset_minutes"] == 180
    follow = records["follow"]
    assert [data["mode"] for data in follow] == ["CAPTURE", "FOLLOW", "FOLLOW"]
    assert follow[1]["follow_profile_id"] == "local-" + follow[0]["fingerprint_id"][6:]


def test_companion_rf_sync_imports_from_the_real_flipper_journal(harness, tmp_path):
    """End to end across both sides: the companion's RfSync (Python) against the Flipper's
    rf_proto + rf_store (the real C code, on a fake SD card)."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from uplink.rf_hunter import EventStore
    from uplink.rf_sync import RfSync

    flipper = _Flipper(harness)
    try:
        assert flipper.ask("#keep 0") == "#ok"
        sync = RfSync(str(tmp_path / "store"), send_clock=False)
        sync.on_link(True)
        sync.handle_line(f"R|{flipper.pending}|{flipper.pending}|1024|0|0".split("|"))
        sync.sync_now()
        for _ in range(2000):
            lines = sync.urgent_lines()
            for line in lines:
                reply = flipper.ask(line)
                assert sync.handle_line(reply.split("|")), reply
            status = sync.status()
            if not lines and not status["syncing"] and status["imported"] >= 4:
                break
        status = sync.status()
        assert status["imported"] == 4 and status["failed"] == 0, status
        assert flipper.ask("#stats") == "#stats 0 0"   # every record acknowledged and removed
        store = EventStore(str(tmp_path / "store"), read_only=True)
        assert len(store.events) == 4
        sources = sorted(event.source_type for event in store.events.values())
        assert sources == ["nfc", "subghz", "subghz", "subghz"]
        # the long capture keeps as many timings as fit in one 4 KiB record
        longest = max(store.events.values(), key=lambda event: len(event.pulse_timings_us))
        assert len(longest.pulse_timings_us) >= 300, len(longest.pulse_timings_us)
        assert longest.frequency_hz == 315000000
    finally:
        flipper.close()


def _drive(sync, flipper, done, steps=5000):
    """Play the link loop between an RfSync and the protocol REPL until done(status)."""
    for _ in range(steps):
        lines = sync.urgent_lines()
        for line in lines:
            reply = flipper.ask(line)
            assert sync.handle_line(reply.split("|")), reply
        status = sync.status()
        if not lines and done(status):
            return status
    raise AssertionError(f"did not settle: {sync.status()}")


def test_records_carried_by_the_flipper_reach_another_pc(harness, tmp_path):
    """PC A puts its records on the Flipper (the real C journal), PC B imports them, and PC A
    never gets its own carried records back."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from uplink.rf_hunter import EventStore
    from uplink.rf_sync import RfSync

    flipper = _Flipper(harness)
    try:
        assert flipper.ask("#keep 0") == "#ok"
        pc_a = RfSync(str(tmp_path / "a"), send_clock=False, pc_id="aaaaaaaa11111111")
        pc_a.on_link(True)
        pc_a.sync_now()
        _drive(pc_a, flipper, lambda st: not st["syncing"] and st["imported"] == 4)
        assert flipper.ask("#stats") == "#stats 0 0"

        pc_a.push_now()
        status = _drive(pc_a, flipper, lambda st: not st["pushing"])
        assert (status["push_total"], status["push_sent"], status["push_failed"]) == (4, 4, 0), status
        assert flipper.ask("#carry") == "#carry 4 0"   # carried, but not for the PC that brought them
        pc_a.push_now()                                 # a second push has nothing to send
        status = _drive(pc_a, flipper, lambda st: not st["pushing"])
        assert (status["push_sent"], status["push_present"]) == (0, 4), status
        pc_a.sync_now()
        status = _drive(pc_a, flipper, lambda st: not st["syncing"])
        assert status["imported"] == 4 and status["pending"] == 0 and status["carry"] == 4

        pc_b = RfSync(str(tmp_path / "b"), send_clock=False, pc_id="bbbbbbbb22222222")
        pc_b.on_link(True)
        pc_b.sync_now()
        status = _drive(pc_b, flipper, lambda st: not st["syncing"] and st["imported"] == 4)
        assert status["failed"] == 0, status
        assert flipper.ask("#carry") == "#carry 0 0"   # imported and removed from the Flipper
        store_a = EventStore(str(tmp_path / "a"), read_only=True)
        store_b = EventStore(str(tmp_path / "b"), read_only=True)
        assert sorted(store_b.events) == sorted(store_a.events)
        for event_id in store_a.events:   # byte for byte what the Flipper recorded
            assert store_b.read_capture(event_id) == store_a.read_capture(event_id)
    finally:
        flipper.close()


def test_carry_protocol_lines(harness):
    """RO/RP/RW as the companion sends them: replies, gaps, repeats, refusals."""
    flipper = _Flipper(harness)
    try:
        record = b'{"event_id":"pc-1","schema_version":1}\n'
        crc = binascii.crc32(record)
        assert flipper.ask(f"RP|pc-1|{len(record)}|{crc}") == "RX|bad|pc-1 send RO first"
        assert flipper.ask("RO|not-hex") .startswith("RX|bad|")
        assert flipper.ask("RO|0123456789abcdef") == "RO|4|0"
        assert flipper.ask(f"RP|pc-1|{len(record)}|{crc}") == "RG|pc-1|0"
        b64 = base64.b64encode
        assert flipper.ask(f"RW|pc-1|20|{b64(record[20:]).decode()}") == "RG|pc-1|0"   # a gap
        assert flipper.ask(f"RW|pc-1|0|{b64(record[:20]).decode()}") == "RG|pc-1|20"
        assert flipper.ask(f"RW|pc-1|0|{b64(record[:20]).decode()}") == "RG|pc-1|20"  # a repeat
        assert flipper.ask("RW|pc-1|20|!!!!").startswith("RX|bad|pc-1 ")
        assert flipper.ask(f"RW|pc-1|20|{b64(record[20:]).decode()}") == f"RG|pc-1|{len(record)}"
        assert flipper.ask(f"RP|pc-1|{len(record)}|{crc}") == "RH|pc-1"
        assert flipper.ask("#carry") == "#carry 1 4"
        too_long = "A" * 244   # 183 bytes, more than one RW line may carry
        assert flipper.ask(f"RP|pc-2|{len(record)}|{crc}") == "RG|pc-2|0"
        assert flipper.ask(f"RW|pc-2|0|{too_long}").startswith("RX|bad|pc-2 ")
        assert flipper.ask("#idle") == "#ok"            # the PC went quiet: the half copy is gone
        assert flipper.ask(f"RW|pc-2|0|{b64(record[:10]).decode()}") == "RX|nf|pc-2 not being received"
        assert flipper.ask("#carry") == "#carry 1 4"
    finally:
        flipper.close()
