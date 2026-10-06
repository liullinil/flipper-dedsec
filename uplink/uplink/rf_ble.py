"""BLE pull client for RF Hunter event journal."""
from __future__ import annotations
import asyncio, binascii, json, os, re
from .rf_hunter import EventStore, RfEvent
from .rf_transport import decode_frame, encode_frame

# Keep these values in lockstep with apps/rf_signal_hunter/rf_hunter_ble.h.
# The profile advertises the 16-bit RF Hunter marker (0xDED5) while the GATT
# service itself uses the 128-bit UUID below.  Older builds used 0xDED6 and a
# ``RFHunter`` name prefix, which made a real Flipper invisible to the client:
# the C profile advertises ``<first-char>DedSec <device>``.
RF_SERVICE_UUID = "f5510000-1d00-4a1e-8b5e-0f11e7ca1000"
RF_RX_UUID = "f5510001-1d00-4a1e-8b5e-0f11e7ca1000"
RF_TX_UUID = "f5510002-1d00-4a1e-8b5e-0f11e7ca1000"
RF_ADV_UUID = "0000ded5-0000-1000-8000-00805f9b34fb"
RF_NAME_PREFIX = "DedSec"
RF_MAX_CHUNK = 80
MAX_RX_BUFFER = 8192
class BleakRfAdapter:
 def __init__(self,client_factory=None,scanner=None,timeout=10.0): self.client_factory=client_factory; self.scanner=scanner; self.timeout=float(timeout); self.client=None; self._frames=asyncio.Queue(); self._wire=b""; self._rid=0; self._lock=asyncio.Lock()
 async def discover(self):
  scanner=self.scanner
  if scanner is None:
   from bleak import BleakScanner; scanner=BleakScanner
  def match(dev,adv):
   name=getattr(adv,"local_name",None) or getattr(dev,"name",None) or ""; uuids=[str(u).lower() for u in (getattr(adv,"service_uuids",None) or [])]; return RF_NAME_PREFIX.lower() in name.lower() or RF_ADV_UUID in uuids
  finder=getattr(scanner,"find_device_by_filter",None)
  if finder: return await finder(match,timeout=self.timeout)
  return next((d for d in await scanner.discover(timeout=self.timeout) if match(d,d)),None)
 async def connect(self,device=None):
  if device is None: device=await self.discover()
  if device is None: raise RuntimeError("RFHunter Flipper was not found")
  factory=self.client_factory
  if factory is None:
   from bleak import BleakClient; factory=BleakClient
  self.client=factory(device,timeout=self.timeout); await self.client.connect(); await self.client.start_notify(RF_TX_UUID,self._on_notify); return self
 async def close(self):
  if self.client:
   try: await self.client.stop_notify(RF_TX_UUID)
   except Exception: pass
   await self.client.disconnect(); self.client=None
  self._wire=b""
  while not self._frames.empty(): self._frames.get_nowait()
 def _on_notify(self,_handle,data):
  self._wire+=bytes(data)
  if len(self._wire)>MAX_RX_BUFFER: self._wire=b""; return
  while b"\n" in self._wire:
   line,self._wire=self._wire.split(b"\n",1)
   if line.strip():
    try: self._frames.put_nowait(decode_frame(line))
    except (UnicodeDecodeError,ValueError,json.JSONDecodeError): pass
 async def request(self,op,**fields):
  async with self._lock:
   if not self.client: raise RuntimeError("BLE adapter is not connected")
   self._rid+=1; rid=self._rid; payload=encode_frame({"v":1,"rid":rid,"op":op,**fields}); mtu=max(20,(getattr(self.client,"mtu_size",23) or 23)-3)
   for pos in range(0,len(payload),mtu): await self.client.write_gatt_char(RF_RX_UUID,payload[pos:pos+mtu],response=False)
   deadline=asyncio.get_running_loop().time()+self.timeout
   while True:
    left=deadline-asyncio.get_running_loop().time()
    if left<=0: raise TimeoutError("RF BLE response timeout")
    reply=await asyncio.wait_for(self._frames.get(),left)
    if reply.get("rid")!=rid: continue
    if reply.get("op")=="error": raise RuntimeError(reply.get("error","RF BLE error"))
    return reply
 async def _list(self):
  cursor=0; items=[]
  while True:
   reply=await self.request("list",cursor=cursor)
   if reply.get("op")=="end": return items
   if reply.get("op")!="item": raise ValueError("expected RF item/end")
   try: next_cursor=int(reply["next"])
   except (KeyError,TypeError,ValueError) as exc: raise ValueError("RF manifest item has no cursor") from exc
   if next_cursor<=cursor: raise ValueError("RF manifest cursor did not advance")
   try:
    if not str(reply["event_id"]): raise ValueError("empty event id")
    if int(reply["size"])<0 or int(reply["crc32"])<0: raise ValueError("negative event size/checksum")
   except (KeyError,TypeError,ValueError) as exc: raise ValueError("invalid RF manifest item") from exc
   items.append(reply); cursor=next_cursor
 async def hello(self):
  """Return the Flipper capability/space response."""
  return await self.request("hello")
 async def sync_to(self,store:EventStore,progress=None,device=None):
  if self.client is None: await self.connect(device)
  stats={"seen":0,"imported":0,"skipped":0}
  for item in await self._list():
   stats["seen"]+=1; eid=str(item["event_id"]); size=int(item["size"]); crc=int(item["crc32"])
   if eid not in store.events:
    payload=await self._read(eid,size,crc,store)
    try: event=RfEvent.from_dict(json.loads(payload.decode("utf-8")))
    except Exception as exc: raise ValueError("invalid RF event payload") from exc
    if event.event_id!=eid: raise ValueError("RF event identity mismatch")
    store.add(event,payload); event.upload_state="imported"; store._flush(); stats["imported"]+=1
   else:
    path=store.events[eid].capture_blob
    if path:
     with open(os.path.join(store.root,path),"rb") as fh: payload=fh.read()
    else: payload=b""
    if len(payload)!=size or (binascii.crc32(payload)&0xffffffff)!=crc: raise ValueError("existing RF event payload checksum mismatch")
    stats["skipped"]+=1
   ack=await self.request("ack",event_id=eid,size=size,crc32=crc)
   if ack.get("op")!="acked" or str(ack.get("event_id",""))!=eid:
    raise ValueError("RF acknowledgement did not match event")
   if progress: progress(stats.copy())
  return stats
 async def _read(self,eid,size,crc,store):
  staging=os.path.join(store.root,".rf-staging"); os.makedirs(staging,exist_ok=True); safe=re.sub(r"[^A-Za-z0-9_.-]","_",eid); path=os.path.join(staging,safe+".part"); offset=os.path.getsize(path) if os.path.exists(path) else 0
  if offset>size: offset=0
  with open(path,"ab" if offset else "wb") as fh:
   while offset<size:
    reply=await self.request("read",event_id=eid,offset=offset)
    if reply.get("op")!="chunk" or str(reply.get("event_id",""))!=eid or int(reply.get("offset",-1))!=offset: raise ValueError("RF chunk offset mismatch")
    try: data=bytes.fromhex(reply.get("hex",""))
    except (TypeError,ValueError) as exc: raise ValueError("invalid RF chunk encoding") from exc
    if not data or len(data)>RF_MAX_CHUNK or offset+len(data)>size: raise ValueError("invalid RF chunk")
    next_offset=int(reply.get("next",-1))
    if next_offset!=offset+len(data): raise ValueError("RF chunk next offset mismatch")
    fh.write(data); fh.flush(); os.fsync(fh.fileno()); offset=next_offset
  with open(path,"rb") as fh: payload=fh.read()
  if len(payload)!=size or (binascii.crc32(payload)&0xffffffff)!=crc: raise ValueError("RF payload checksum mismatch")
  # A completed staging file is no longer needed.  Removing it prevents a
  # later event with the same local filename from accidentally resuming from
  # stale bytes while preserving interrupted transfers on all earlier exits.
  try: os.remove(path)
  except OSError: pass
  return payload
 async def import_pending(self,store,**kwargs): return await self.sync_to(store,**kwargs)
__all__=["BleakRfAdapter","RF_SERVICE_UUID","RF_ADV_UUID","RF_NAME_PREFIX","RF_MAX_CHUNK"]
