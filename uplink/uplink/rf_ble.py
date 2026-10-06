"""BLE pull client for the RF Hunter event journal.

The Flipper profile sends newline-delimited JSON frames. Notifications may
split a frame at any byte boundary, so the adapter buffers until a complete
line is available. A capture is acknowledged only after it has been validated
and durably imported into :class:`EventStore`.
"""

from __future__ import annotations

import asyncio
import binascii
import json
import os
import re
from typing import Any, Callable, Optional

from .rf_hunter import EventStore, RfEvent
from .rf_transport import decode_frame, encode_frame

# Keep these values in lockstep with apps/rf_signal_hunter/rf_hunter_ble.h.
RF_SERVICE_UUID = "f5510000-1d00-4a1e-8b5e-0f11e7ca1000"
RF_RX_UUID = "f5510001-1d00-4a1e-8b5e-0f11e7ca1000"
RF_TX_UUID = "f5510002-1d00-4a1e-8b5e-0f11e7ca1000"
RF_ADV_UUID = "0000ded5-0000-1000-8000-00805f9b34fb"
RF_NAME_PREFIX = "DedSec"
RF_MAX_CHUNK = 80
MAX_RX_BUFFER = 8192


class BleakRfAdapter:
    """Reliable desktop reader for a connected RF Hunter FAP."""

    def __init__(self, client_factory=None, scanner=None, timeout: float = 10.0):
        self.client_factory = client_factory
        self.scanner = scanner
        self.timeout = float(timeout)
        self.client = None
        self._frames: asyncio.Queue = asyncio.Queue()
        self._wire = b""
        self._rid = 0
        self._lock = asyncio.Lock()

    async def discover(self):
        scanner = self.scanner
        if scanner is None:
            from bleak import BleakScanner

            scanner = BleakScanner

        def match(device, advertisement):
            name = (getattr(advertisement, "local_name", None)
                    or getattr(device, "name", None) or "")
            uuids = [str(value).lower() for value in
                     (getattr(advertisement, "service_uuids", None) or [])]
            return (RF_NAME_PREFIX.lower() in name.lower()
                    or RF_ADV_UUID in uuids
                    or RF_ADV_UUID.replace("0000", "") in uuids)

        finder = getattr(scanner, "find_device_by_filter", None)
        if finder:
            return await finder(match, timeout=self.timeout)
        devices = await scanner.discover(timeout=self.timeout)
        return next((device for device in devices if match(device, device)), None)

    async def connect(self, device=None):
        if device is None:
            device = await self.discover()
        if device is None:
            raise RuntimeError("RF Hunter Flipper was not found")
        factory = self.client_factory
        if factory is None:
            from bleak import BleakClient

            factory = BleakClient
        self.client = factory(device, timeout=self.timeout)
        await self.client.connect()
        await self.client.start_notify(RF_TX_UUID, self._on_notify)
        return self

    async def close(self):
        if self.client:
            try:
                await self.client.stop_notify(RF_TX_UUID)
            except Exception:
                pass
            await self.client.disconnect()
            self.client = None
        self._wire = b""
        while not self._frames.empty():
            self._frames.get_nowait()

    def _on_notify(self, _handle, data):
        self._wire += bytes(data)
        if len(self._wire) > MAX_RX_BUFFER:
            # A peer that never terminates a frame must not consume memory
            # indefinitely. The next newline starts a fresh frame.
            self._wire = b""
            return
        while b"\n" in self._wire:
            line, self._wire = self._wire.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                self._frames.put_nowait(decode_frame(line))
            except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
                # The request timeout makes malformed notifications visible.
                continue

    async def request(self, op: str, **fields) -> dict:
        async with self._lock:
            if not self.client:
                raise RuntimeError("BLE adapter is not connected")
            self._rid += 1
            rid = self._rid
            payload = encode_frame({"v": 1, "rid": rid, "op": op, **fields})
            mtu = max(20, (getattr(self.client, "mtu_size", 23) or 23) - 3)
            for offset in range(0, len(payload), mtu):
                await self.client.write_gatt_char(
                    RF_RX_UUID, payload[offset:offset + mtu], response=False)

            deadline = asyncio.get_running_loop().time() + self.timeout
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError("RF BLE response timeout")
                reply = await asyncio.wait_for(self._frames.get(), remaining)
                if reply.get("rid") != rid:
                    continue
                if reply.get("op") == "error":
                    raise RuntimeError(reply.get("error", "RF BLE error"))
                return reply

    async def _list(self) -> list[dict]:
        cursor = 0
        items = []
        while True:
            reply = await self.request("list", cursor=cursor)
            if reply.get("op") == "end":
                return items
            if reply.get("op") != "item":
                raise ValueError("expected RF item/end")
            try:
                next_cursor = int(reply["next"])
                event_id = str(reply["event_id"])
                size = int(reply["size"])
                crc32 = int(reply["crc32"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("invalid RF manifest item") from exc
            if not event_id or size < 0 or crc32 < 0:
                raise ValueError("invalid RF manifest item")
            if next_cursor <= cursor:
                raise ValueError("RF manifest cursor did not advance")
            items.append({**reply, "event_id": event_id, "size": size, "crc32": crc32})
            cursor = next_cursor

    async def hello(self):
        """Return the Flipper capability and pending-count response."""
        return await self.request("hello")

    async def sync_to(
        self,
        store: EventStore,
        progress: Optional[Callable[[dict], Any]] = None,
        device=None,
    ) -> dict:
        """Import pending Flipper records and ACK only durable imports."""
        if self.client is None:
            await self.connect(device)
        stats = {"seen": 0, "imported": 0, "skipped": 0}
        for item in await self._list():
            stats["seen"] += 1
            event_id = item["event_id"]
            size = item["size"]
            crc32 = item["crc32"]
            if event_id not in store.events:
                payload = await self._read(event_id, size, crc32, store)
                try:
                    event = RfEvent.from_dict(json.loads(payload.decode("utf-8")))
                except Exception as exc:
                    raise ValueError("invalid RF event payload") from exc
                if event.event_id != event_id:
                    raise ValueError("RF event identity mismatch")
                store.add(event, payload)
                event.upload_state = "imported"
                store._flush()
                stats["imported"] += 1
            else:
                event = store.events[event_id]
                payload = b""
                if event.capture_blob:
                    with open(os.path.join(store.root, event.capture_blob), "rb") as fh:
                        payload = fh.read()
                if (len(payload) != size
                        or (binascii.crc32(payload) & 0xFFFFFFFF) != crc32):
                    raise ValueError("existing RF event payload checksum mismatch")
                stats["skipped"] += 1
            ack = await self.request("ack", event_id=event_id, size=size, crc32=crc32)
            if ack.get("op") != "acked" or str(ack.get("event_id", "")) != event_id:
                raise ValueError("RF acknowledgement did not match event")
            if progress:
                progress(stats.copy())
        return stats

    async def _read(self, event_id: str, size: int, crc32: int, store: EventStore) -> bytes:
        staging = os.path.join(store.root, ".rf-staging")
        os.makedirs(staging, exist_ok=True)
        safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", event_id)
        path = os.path.join(staging, safe_name + ".part")
        offset = os.path.getsize(path) if os.path.exists(path) else 0
        if offset > size:
            offset = 0

        with open(path, "ab" if offset else "wb") as fh:
            while offset < size:
                reply = await self.request("read", event_id=event_id, offset=offset)
                try:
                    data = bytes.fromhex(reply.get("hex", ""))
                    reply_offset = int(reply.get("offset", -1))
                    next_offset = int(reply.get("next", -1))
                except (TypeError, ValueError) as exc:
                    raise ValueError("invalid RF chunk") from exc
                if (reply.get("op") != "chunk"
                        or str(reply.get("event_id", "")) != event_id
                        or reply_offset != offset):
                    raise ValueError("RF chunk offset mismatch")
                if not data or len(data) > RF_MAX_CHUNK or offset + len(data) > size:
                    raise ValueError("invalid RF chunk")
                if next_offset != offset + len(data):
                    raise ValueError("RF chunk next offset mismatch")
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
                offset = next_offset

        with open(path, "rb") as fh:
            payload = fh.read()
        if len(payload) != size or (binascii.crc32(payload) & 0xFFFFFFFF) != crc32:
            raise ValueError("RF payload checksum mismatch")
        try:
            os.remove(path)
        except OSError:
            pass
        return payload

    async def import_pending(self, store: EventStore, **kwargs):
        """Compatibility alias for callers that use upload terminology."""
        return await self.sync_to(store, **kwargs)


__all__ = [
    "BleakRfAdapter",
    "RF_SERVICE_UUID",
    "RF_ADV_UUID",
    "RF_NAME_PREFIX",
    "RF_MAX_CHUNK",
]
