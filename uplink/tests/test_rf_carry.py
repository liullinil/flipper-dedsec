"""FLIPPER <- PC: RfSync puts the PC's records on the Flipper (RO/RP/RW) for another PC.

CarryFlipper extends the Python model of the RF engine with the carry side of rf_proto.c; the
real C code is exercised end to end in test_rf_native_reliability.py.
"""
import base64
import json

from test_rf_sync import Clock, FakeFlipper, c_record, crc, deliver

from uplink.rf_hunter import EventStore, RfEvent
from uplink.rf_sync import PUSH_CHUNK, RfSync, push_payload

PC = "abcdef0123456789"


class CarryFlipper(FakeFlipper):
    """FakeFlipper that also takes records from a PC, like rf_proto.c / rf_store.c."""

    def __init__(self, records=(), **kwargs):
        super().__init__(records, **kwargs)
        self.peer = ""
        self.carried = {}            # event_id -> (origin, bytes)
        self.receiving = None        # [event_id, size, crc32, bytearray]
        self.lost_writes = set()     # (event_id, offset) of RW lines lost once on the way
        self.full = False

    def handle(self, line):
        parts = line.split("|")
        tag = parts[0]
        if tag == "RW" and (parts[1], int(parts[2])) in self.lost_writes:
            self.lost_writes.discard((parts[1], int(parts[2])))
            return []
        if tag not in ("RO", "RP", "RW"):
            return super().handle(line)
        self.requests.append(line)
        if self.off:
            return ["RX|off|RF sync disabled"]
        if tag == "RO":
            self.peer = parts[1]
            foreign = sum(1 for origin, _ in self.carried.values() if origin != self.peer)
            return [f"RO|{len(self.events) + foreign}|{len(self.carried)}"]
        event_id = parts[1]
        if tag == "RP":
            size, checksum = int(parts[2]), int(parts[3])
            if not self.peer:
                return [f"RX|bad|{event_id} send RO first"]
            have = self.events.get(event_id) or self.carried.get(event_id, ("", None))[1]
            if have is not None and len(have) == size and crc(have) == checksum:
                return [f"RH|{event_id}"]
            if self.full:
                return [f"RX|io|{event_id} SD card full"]
            self.receiving = [event_id, size, checksum, bytearray()]
            return [f"RG|{event_id}|0"]
        record = self.receiving
        if record is None or record[0] != event_id:
            return [f"RX|nf|{event_id} not being received"]
        if int(parts[2]) == len(record[3]):
            record[3] += base64.b64decode(parts[3])
        received = len(record[3])
        if received == record[1]:
            self.receiving = None
            if crc(bytes(record[3])) != record[2]:
                return [f"RX|bad|{event_id} size/crc32 mismatch"]
            self.carried[event_id] = (self.peer, bytes(record[3]))
        return [f"RG|{event_id}|{received}"]


def seeded_sync(tmp_path, records):
    """An RfSync whose store already holds `records` (imported earlier)."""
    root = tmp_path / "store"
    store = EventStore(root)
    for _event_id, payload in records:
        store.add(RfEvent.from_dict(json.loads(payload)), capture=payload)
    clock = Clock()
    sync = RfSync(str(root), clock=clock, wall_clock=lambda: 1791316800.0 + clock.now, pc_id=PC)
    return sync, clock


def run(sync, flipper, clock, steps=6000, step=0.05):
    for _ in range(steps):
        lines = sync.urgent_lines()
        replies = []
        for line in lines:
            replies.extend(flipper.handle(line))
        for reply in replies:
            deliver(sync, reply)
        clock.advance(step)
        status = sync.status()
        if not lines and not replies and not status["pushing"] and not status["syncing"]:
            return status
    raise AssertionError(f"RF push did not settle: {sync.status()}")


def test_push_carries_every_record_once_and_skips_what_the_flipper_has(tmp_path):
    records = [c_record(1), c_record(2, pulses=tuple(range(300, 700, 2))), c_record(3)]
    sync, clock = seeded_sync(tmp_path, records)
    flipper = CarryFlipper([records[2]])          # one of them is still pending on the Flipper
    sync.on_link(True)
    sync.push_now()
    status = run(sync, flipper, clock)
    assert (status["push_total"], status["push_sent"], status["push_present"], status["push_failed"]) \
        == (3, 2, 1, 0), status
    assert status["push_error"] == "" and status["last_push"] is not None
    requests = [line for line in flipper.requests if not line.startswith("Z|")]
    assert requests[0] == f"RO|{PC}"             # the Flipper learns who brings them first
    for event_id, payload in records[:2]:        # byte for byte what the Flipper recorded
        assert flipper.carried[event_id] == (PC, payload)
    # at most four RW lines in flight, each within one engine request line
    writes = [line for line in flipper.requests if line.startswith("RW|")]
    assert all(len(line) <= 3 + 48 + 12 + 240 for line in writes)
    sync.push_now()
    status = run(sync, flipper, clock)
    assert (status["push_sent"], status["push_present"]) == (0, 3)


def test_lost_chunks_are_sent_again_from_where_the_flipper_stopped(tmp_path):
    event_id, payload = c_record(1, pulses=tuple(range(300, 900, 3)))
    assert len(payload) > 6 * PUSH_CHUNK
    sync, clock = seeded_sync(tmp_path, [(event_id, payload)])
    flipper = CarryFlipper()
    flipper.lost_writes = {(event_id, 2 * PUSH_CHUNK), (event_id, 5 * PUSH_CHUNK)}
    sync.on_link(True)
    sync.push_now()
    status = run(sync, flipper, clock)
    assert status["push_sent"] == 1 and status["push_failed"] == 0, status
    assert flipper.carried[event_id] == (PC, payload)
    sent = [int(line.split("|")[2]) for line in flipper.requests if line.startswith("RW|")]
    assert sent.count(2 * PUSH_CHUNK) == 1 and sent.count(5 * PUSH_CHUNK) == 1   # lost ones arrive once


def test_a_full_sd_card_stops_the_push(tmp_path):
    sync, clock = seeded_sync(tmp_path, [c_record(1), c_record(2)])
    flipper = CarryFlipper()
    flipper.full = True
    sync.on_link(True)
    sync.push_now()
    status = run(sync, flipper, clock)
    assert not status["pushing"] and "cannot store" in status["push_error"]
    assert len([line for line in flipper.requests if line.startswith("RP|")]) == 1


def test_an_old_flipper_app_cannot_carry_but_still_imports(tmp_path):
    sync, clock = seeded_sync(tmp_path, [c_record(1)])
    flipper = FakeFlipper([c_record(2)])          # before 1.3.0: RO is an unknown request
    sync.on_link(True)
    sync.push_now()
    status = run(sync, flipper, clock)
    assert not status["pushing"] and "too old" in status["push_error"]
    assert not [line for line in flipper.requests if line.startswith("RP|")]
    deliver(sync, flipper.status_line())          # the import round still works
    status = run(sync, flipper, clock)
    assert status["imported"] == 1 and status["failed"] == 0 and status["last_error"] == ""


def test_push_waits_for_the_import_round_in_progress(tmp_path):
    sync, clock = seeded_sync(tmp_path, [c_record(1)])
    flipper = CarryFlipper([c_record(2)])
    sync.on_link(True)
    deliver(sync, flipper.status_line())          # pending > 0: an import round starts
    assert sync.status()["syncing"]
    sync.push_now()
    status = run(sync, flipper, clock)
    assert status["imported"] == 1
    # both records go: the one imported just now is on the Flipper only until its ACK
    assert (status["push_sent"], status["push_total"]) == (2, 2), status
    tags = [line.split("|")[0] for line in flipper.requests]
    assert tags.index("RP") > tags.index("RA")


def test_link_loss_ends_the_push_and_a_new_one_finishes_the_job(tmp_path):
    records = [c_record(seq, pulses=tuple(range(300, 700, 2))) for seq in (1, 2)]
    sync, clock = seeded_sync(tmp_path, records)
    flipper = CarryFlipper()
    sync.on_link(True)
    sync.push_now()
    for _ in range(3):                            # a few polls: the first record is on its way
        for line in sync.urgent_lines():
            for reply in flipper.handle(line):
                deliver(sync, reply)
    sync.on_link(False)
    status = sync.status()
    assert not status["pushing"] and "link lost" in status["push_error"]
    flipper.receiving = None                      # the Flipper dropped the half record (idle)
    sync.on_link(True)
    sync.push_now()
    status = run(sync, flipper, clock)
    assert status["push_failed"] == 0 and status["push_error"] == ""
    assert {event_id: data for event_id, (_, data) in flipper.carried.items()} == dict(records)


def test_push_payload_is_the_flipper_record_or_its_json(tmp_path):
    event_id, payload = c_record(1)
    store = EventStore(tmp_path / "store")
    store.add(RfEvent.from_dict(json.loads(payload)), capture=payload)
    assert push_payload(store, store.events[event_id]) == payload
    data = json.loads(c_record(2)[1])
    store.add(RfEvent.from_dict(data))            # imported without the raw record
    other = push_payload(store, store.events[data["event_id"]])
    assert other.startswith(b"{") and other.endswith(b"}\n")
    assert json.loads(other)["event_id"] == data["event_id"]


def test_push_without_a_flipper_says_so(tmp_path):
    sync, _clock = seeded_sync(tmp_path, [c_record(1)])
    sync.on_link(False)
    sync.push_now()
    status = sync.status()
    assert not status["pushing"] and status["push_error"] == "Flipper not connected"
