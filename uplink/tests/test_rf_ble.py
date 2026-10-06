import asyncio
import base64

from uplink.rf_ble import BleakRfAdapter


class FakeClient:
    mtu_size = 23

    def __init__(self, device, timeout=0):
        self.writes = []
        self.writes_seen = []
        self.buffer = b""
        self.callback = None

    async def connect(self): pass
    async def disconnect(self): pass
    async def start_notify(self, uuid, callback): self.callback = callback
    async def stop_notify(self, uuid): pass

    async def write_gatt_char(self, uuid, data, response=False):
        self.writes.append(bytes(data))
        self.writes_seen.append(bytes(data))
        self.buffer += bytes(data)
        if b"\n" not in self.buffer:
            return
        frame, self.buffer = self.buffer.split(b"\n", 1)
        frame = frame.decode().strip()
        import json
        msg = json.loads(frame)
        self.writes.clear()
        op = "resume" if msg["op"] == "event_begin" else "chunk_ack" if msg["op"] == "event_chunk" else "ack"
        reply = {"v": 1, "op": op, "event_id": msg.get("event_id", ""), "offset": 2}
        self.callback(1, (json.dumps(reply) + "\n").encode())


def test_bleak_adapter_writes_and_consumes_ack():
    async def run():
        adapter = BleakRfAdapter(client_factory=FakeClient, timeout=1)
        await adapter.connect("device")
        reply = await adapter.request({"v": 1, "op": "event_chunk", "event_id": "e", "offset": 0,
                                       "data": base64.b64encode(b"x").decode()})
        assert reply["op"] == "chunk_ack"
        assert adapter.client.writes_seen
        await adapter.close()
    asyncio.run(run())
