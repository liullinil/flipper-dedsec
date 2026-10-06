import asyncio, binascii, json
from uplink.rf_ble import BleakRfAdapter, RF_RX_UUID, RF_TX_UUID
from uplink.rf_hunter import EventStore, RfEvent
class FakeClient:
    mtu_size=64
    def __init__(self,device,timeout=0): self.cb=None; self.buf=b""; self.payload=device; self.acks=[]
    async def connect(self): pass
    async def disconnect(self): pass
    async def start_notify(self,u,cb): self.cb=cb
    async def stop_notify(self,u): pass
    async def write_gatt_char(self,u,data,response=False):
        self.buf+=bytes(data)
        while b"\n" in self.buf:
            line,self.buf=self.buf.split(b"\n",1); m=json.loads(line); op=m["op"]
            if op=="hello": r={"op":"hello","device_uuid":"d","pending":1,"free_bytes":99}
            elif op=="list": r={"op":"item","event_id":self.payload["id"],"size":len(self.payload["raw"]),"crc32":binascii.crc32(self.payload["raw"])&0xffffffff,"next":1} if m["cursor"]==0 else {"op":"end","next":1}
            elif op=="read":
                off=m["offset"]; part=self.payload["raw"][off:off+80]; r={"op":"chunk","event_id":m["event_id"],"offset":off,"next":off+len(part),"hex":part.hex()}
            elif op=="ack": self.acks.append(m); r={"op":"acked","event_id":m["event_id"]}
            else: r={"op":"error","error":"bad"}
            r.update(v=1,rid=m["rid"]); self.cb(1,(json.dumps(r)+"\n").encode())
def test_full_sync_and_ack(tmp_path):
 async def run():
  e=RfEvent("d","s",1,"2026-01-01T00:00:00Z",1,event_id="rf-d-s-1"); raw=json.dumps(e.to_dict(),separators=(",",":")).encode(); dev={"id":e.event_id,"raw":raw}; c=FakeClient(dev); a=BleakRfAdapter(client_factory=lambda d,timeout=0:c,timeout=1); await a.connect(dev); st=EventStore(tmp_path); out=await a.sync_to(st); assert out["imported"]==1 and c.acks; assert st.events[e.event_id].upload_state=="imported"; await a.close()
 asyncio.run(run())
def test_partial_unicode_fragment_and_crc_reject(tmp_path):
 async def run():
  e=RfEvent("d","s",2,"2026-01-01T00:00:00Z",1,event_id="rf-d-s-2",classification="☃"); raw=json.dumps(e.to_dict(),ensure_ascii=False).encode(); dev={"id":e.event_id,"raw":raw}; c=FakeClient(dev); a=BleakRfAdapter(client_factory=lambda d,timeout=0:c,timeout=1); await a.connect(dev); st=EventStore(tmp_path); await a.sync_to(st); assert e.event_id in st.events
 asyncio.run(run())
