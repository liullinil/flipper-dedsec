"""Async Bleak adapter for the RF Hunter framed transport.

The adapter deliberately contains no radio operations.  It only discovers the
DedSec BLE service, writes framed JSON to RX, and consumes newline-delimited
JSON notifications from TX.  Bleak is imported lazily so the store and tests
work on machines without Bluetooth support.
"""
from __future__ import annotations

import asyncio
import json
from typing import Callable, Optional

from .link import ADV_UUID, NAME_PREFIX, RX_UUID, TX_UUID
from .rf_transport import RfBleTransport, decode_frame, encode_frame


class BleakRfAdapter:
    """Request/response BLE client for an RF Hunter capable Flipper."""

    def __init__(self, client_factory=None, scanner=None, timeout: float = 10.0):
        self.client_factory = client_factory
        self.scanner = scanner
        self.timeout = timeout
        self.client = None
        self._frames: asyncio.Queue = asyncio.Queue()
        self._rxbuf = b""

    async def discover(self):
        scanner = self.scanner
        if scanner is None:
            from bleak import BleakScanner
            scanner = BleakScanner

        def match(dev, adv):
            name = getattr(adv, "local_name", None) or getattr(dev, "name", None) or ""
            uuids = [u.lower() for u in (getattr(adv, "service_uuids", None) or [])]
            return name.startswith(NAME_PREFIX) or ADV_UUID in uuids

        finder = getattr(scanner, "find_device_by_filter", None)
        if finder is None:
            devices = await scanner.discover(timeout=self.timeout)
            return next((d for d in devices if match(d, d)), None)
        return await finder(match, timeout=self.timeout)

    async def connect(self, device=None):
        if device is None:
            device = await self.discover()
        if device is None:
            raise RuntimeError("DedSec Flipper was not found")
        factory = self.client_factory
        if factory is None:
            from bleak import BleakClient
            factory = BleakClient
        self.client = factory(device, timeout=self.timeout)
        await self.client.connect()
        await self.client.start_notify(TX_UUID, self._on_notify)
        return self

    async def close(self):
        if self.client is not None:
            try:
                await self.client.stop_notify(TX_UUID)
            except Exception:
                pass
            await self.client.disconnect()
            self.client = None

    def _on_notify(self, _handle, data):
        self._rxbuf += bytes(data)
        while b"\n" in self._rxbuf:
            line, self._rxbuf = self._rxbuf.split(b"\n", 1)
            if line.strip():
                self._frames.put_nowait(decode_frame(line))

    async def request(self, frame: dict) -> dict:
        if self.client is None:
            raise RuntimeError("BLE adapter is not connected")
        payload = encode_frame(frame)
        mtu = max(20, (getattr(self.client, "mtu_size", 23) or 23) - 3)
        for pos in range(0, len(payload), mtu):
            await self.client.write_gatt_char(RX_UUID, payload[pos:pos + mtu], response=False)
        while True:
            reply = await asyncio.wait_for(self._frames.get(), self.timeout)
            if frame.get("event_id") in (None, reply.get("event_id")):
                return reply

    async def manifest(self):
        return await self.request({"v": 1, "op": "manifest"})

    async def import_events(self, frames):
        """Send frames, honoring resume offsets returned by the Flipper."""
        resume_offset = 0
        for frame in frames:
            if frame.get("op") == "event_chunk" and int(frame.get("offset", 0)) < resume_offset:
                continue
            reply = await self.request(frame)
            if frame.get("op") == "event_begin" and reply.get("op") == "resume":
                resume_offset = int(reply.get("offset", 0))
                continue
            if frame.get("op") == "event_chunk" and reply.get("op") not in ("chunk_ack", "ack"):
                raise RuntimeError("RF BLE chunk was not acknowledged")
            if frame.get("op") == "event_commit" and reply.get("op") != "ack":
                raise RuntimeError("RF BLE commit was not acknowledged")


__all__ = ["BleakRfAdapter"]
