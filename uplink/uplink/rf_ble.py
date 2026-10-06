"""BLE pull client for RF Hunter event journal."""
from __future__ import annotations
import asyncio, binascii, json, os, re
from .rf_hunter import EventStore, RfEvent
from .rf_transport import decode_frame, encode_frame
RF_RX_UUID="f5510001-1d00-4a1e-8b5e-0f11e7ca1000"; RF_TX_UUID="f5510002-1d00-4a1e-8b5e-0f11e7ca1000"
RF_ADV_UUID="0000ded6-0000-1000-8000-00805f9b34fb"; RF_NAME_PREFIX="RFHunter"; MAX_RX_BUFFER=8192
class BleakRfAdapter:
 def __init__(self,client_factory=None,scanner=None,timeout=10.0): self.client_factory=client_factory; self.scanner=scanner; self.timeout=float(timeout); self.client=None; self._frames=asyncio.Queue(); self._wire=b""; self._rid=0; self._lock=asyncio.Lock()
 async def discover(self):
  scanner=self.scanner
  if scanner is None:
   from bleak import BleakScanner; scanner=BleakScanner
  def match(dev,adv):
   name=getattr(adv,"local_name",None) or getattr(dev,"name",None) or ""; uuids=[str(u).lower() for u in (getattr(adv,"service_uuids",None) or [])]; return name.startswith(RF_NAME_PREFIX) or RF_ADV_UUID in uuids
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
   items.append(reply); cursor=int(reply.get("next",cursor+1))
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
    path=store.events[eid].capture_blob; payload=b"" if not path else open(os.path.join(store.root,path),"rb").read()
    if len(payload)!=size or (binascii.crc32(payload)&0xffffffff)!=crc: raise ValueError("existing RF event payload checksum mismatch")
    stats["skipped"]+=1
   await self.request("ack",event_id=eid,size=size,crc32=crc)
   if progress: progress(stats.copy())
  return stats
 async def _read(self,eid,size,crc,store):
  staging=os.path.join(store.root,".rf-staging"); os.makedirs(staging,exist_ok=True); safe=re.sub(r"[^A-Za-z0-9_.-]","_",eid); path=os.path.join(staging,safe+".part"); offset=os.path.getsize(path) if os.path.exists(path) else 0
  if offset>size: offset=0
  with open(path,"ab" if offset else "wb") as fh:
   while offset<size:
    reply=await self.request("read",event_id=eid,offset=offset)
    if reply.get("op")!="chunk" or int(reply.get("offset",-1))!=offset: raise ValueError("RF chunk offset mismatch")
    data=bytes.fromhex(reply.get("hex",""))
    if not data or len(data)>80 or offset+len(data)>size: raise ValueError("invalid RF chunk")
    fh.write(data); fh.flush(); os.fsync(fh.fileno()); offset=int(reply.get("next",offset+len(data)))
  payload=open(path,"rb").read()
  if len(payload)!=size or (binascii.crc32(payload)&0xffffffff)!=crc: raise ValueError("RF payload checksum mismatch")
  return payload
 async def import_pending(self,store,**kwargs): return await self.sync_to(store,**kwargs)
__all__=["BleakRfAdapter","RF_ADV_UUID","RF_NAME_PREFIX"]
