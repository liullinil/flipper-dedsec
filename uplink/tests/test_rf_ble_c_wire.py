"""Golden tests for the wire emitted by apps/rf_signal_hunter.

The fake client below deliberately mirrors the C profile's line protocol:
manifest items are paged, reads are 80-byte hex chunks, and an ACK validates
the exact size/CRC before reclaiming the event.  This catches drift between
the desktop adapter and the API-88.9 FAP without requiring a physical radio.
"""
import asyncio
import binascii
import json

import pytest

from uplink.rf_ble import BleakRfAdapter, RF_ADV_UUID, RF_NAME_PREFIX, RF_MAX_CHUNK
from uplink.rf_hunter import EventStore, RfEvent


class CProfileClient:
    mtu_size = 23

    def __init__(self, records, *, advertised_name="FDedSec Flipper Zero"):
        self.records = dict(records)
        self.acked = []
        self.cb = None
        self.buf = b""
        self.advertised_name = advertised_name

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    async def start_notify(self, _uuid, callback):
        self.cb = callback

    async def stop_notify(self, _uuid):
        return None

    def _emit(self, frame):
        # Notifications can split a JSON line at arbitrary byte boundaries.
        wire = (json.dumps(frame, separators=(",", ":")) + "\n").encode()
        for pos in range(0, len(wire), 7):
            self.cb(1, wire[pos : pos + 7])

    async def write_gatt_char(self, _uuid, data, response=False):
        del response
        self.buf += bytes(data)
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            if not line:
                continue
            request = json.loads(line)
            rid = request["rid"]
            op = request["op"]
            if op == "hello":
                response = {
                    "v": 1,
                    "rid": rid,
                    "op": "hello_ack",
                    "device_uuid": "0123456789abcdef",
                    "pending": len(self.records),
                }
            elif op == "list":
                ids = sorted(self.records)
                cursor = int(request.get("cursor", 0))
                if cursor < len(ids):
                    event_id = ids[cursor]
                    payload = self.records[event_id]
                    response = {
                        "v": 1,
                        "rid": rid,
                        "op": "item",
                        "event_id": event_id,
                        "size": len(payload),
                        "crc32": binascii.crc32(payload) & 0xFFFFFFFF,
                        "next": cursor + 1,
                    }
                else:
                    response = {"v": 1, "rid": rid, "op": "end"}
            elif op == "read":
                event_id = request["event_id"]
                if event_id not in self.records:
                    response = {"v": 1, "rid": rid, "op": "error", "error": "event not found"}
                else:
                    payload = self.records[event_id]
                    offset = int(request["offset"])
                    chunk = payload[offset : offset + RF_MAX_CHUNK]
                    response = {
                        "v": 1,
                        "rid": rid,
                        "op": "chunk",
                        "event_id": event_id,
                        "offset": offset,
                        "next": offset + len(chunk),
                        "hex": chunk.hex(),
                    }
            elif op == "ack":
                event_id = request["event_id"]
                payload = self.records.get(event_id)
                valid = (
                    payload is not None
                    and int(request.get("size", -1)) == len(payload)
                    and int(request.get("crc32", -1)) == (binascii.crc32(payload) & 0xFFFFFFFF)
                )
                if not valid:
                    response = {"v": 1, "rid": rid, "op": "error", "error": "ack validation failed"}
                else:
                    self.acked.append(event_id)
                    del self.records[event_id]
                    response = {"v": 1, "rid": rid, "op": "acked", "event_id": event_id}
            else:
                response = {"v": 1, "rid": rid, "op": "error", "error": "unknown operation"}
            self._emit(response)


def _record(seq=1):
    event = RfEvent(
        "0123456789abcdef",
        "s123",
        seq,
        "2026-10-06T20:00:00Z",
        seq,
        frequency_hz=433920000,
        modulation="OOK",
        pulse_timings_us=(400, 800, 400, 800),
    )
    # The C FAP stores a newline after each JSON record.
    return event, (json.dumps(event.to_dict(), separators=(",", ":")) + "\n").encode()


def test_actual_c_profile_flow_imports_crc_and_reclaims(tmp_path):
    event, payload = _record()
    client = CProfileClient({event.event_id: payload})

    async def run():
        adapter = BleakRfAdapter(client_factory=lambda _d, timeout=0: client, timeout=1)
        await adapter.connect(object())
        store = EventStore(tmp_path)
        result = await adapter.sync_to(store)
        assert result == {"seen": 1, "imported": 1, "skipped": 0}
        assert client.acked == [event.event_id]
        assert client.records == {}
        assert store.events[event.event_id].event_id == event.event_id
        await adapter.close()

    asyncio.run(run())


def test_bad_ack_is_rejected_and_event_remains_pending(tmp_path):
    event, payload = _record()
    client = CProfileClient({event.event_id: payload})

    async def run():
        adapter = BleakRfAdapter(client_factory=lambda _d, timeout=0: client, timeout=1)
        await adapter.connect(object())
        with pytest.raises(RuntimeError, match="ack validation"):
            await adapter.request("ack", event_id=event.event_id, size=len(payload), crc32=0)
        assert event.event_id in client.records
        await adapter.close()

    asyncio.run(run())


def test_discovery_matches_c_advertisement_name_and_uuid():
    class Device:
        name = "Flipper Zero"

    class Advertisement:
        local_name = "FDedSec Flipper Zero"
        service_uuids = []

    class Scanner:
        async def find_device_by_filter(self, callback, timeout):
            assert timeout > 0
            assert callback(Device(), Advertisement()) is True
            return Device()

    async def run():
        adapter = BleakRfAdapter(scanner=Scanner(), timeout=1)
        assert await adapter.discover() is not None

    asyncio.run(run())
    assert RF_NAME_PREFIX == "DedSec"
    assert RF_ADV_UUID.endswith("ded5-0000-1000-8000-00805f9b34fb")
