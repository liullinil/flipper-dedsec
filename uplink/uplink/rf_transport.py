"""Small, backend-neutral BLE transport for RF Hunter event import/export.

The module speaks framed JSON dictionaries so a BLE adapter only needs to pass
UTF-8 frames in and out.  Payload chunks are base64 encoded and every transfer
is resumable by event id and byte offset.  No radio or transmit operations live
here.
"""
from __future__ import annotations

import base64
import hashlib
import json
from typing import Iterable, Optional

from .rf_hunter import EventStore, UploadReceiver

PROTOCOL_VERSION = 1
DEFAULT_CHUNK_SIZE = 192


def encode_frame(frame: dict) -> bytes:
    """Encode one protocol frame for a BLE characteristic."""
    return (json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def decode_frame(frame) -> dict:
    """Decode bytes/text or pass through a frame dictionary."""
    if isinstance(frame, dict):
        return dict(frame)
    if isinstance(frame, bytes):
        frame = frame.decode("utf-8")
    value = json.loads(frame)
    if not isinstance(value, dict):
        raise ValueError("BLE frame must be an object")
    return value


def _reply(op: str, event_id: str = "", **fields) -> dict:
    result = {"v": PROTOCOL_VERSION, "op": op}
    if event_id:
        result["event_id"] = event_id
    result.update(fields)
    return result


class RfBleTransport:
    """Desktop endpoint for passive RF event transfers.

    ``receive`` handles frames sent by a Flipper and returns a response frame.
    ``iter_upload`` produces frames for sending pending desktop events to a
    peer, which is useful for a BLE adapter or deterministic tests.
    """

    def __init__(self, store: EventStore, chunk_size: int = DEFAULT_CHUNK_SIZE,
                 receiver: Optional[UploadReceiver] = None):
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        self.store = store
        self.chunk_size = int(chunk_size)
        self.receiver = receiver or UploadReceiver(store)

    def receive(self, frame) -> dict:
        msg = decode_frame(frame)
        if int(msg.get("v", PROTOCOL_VERSION)) != PROTOCOL_VERSION:
            raise ValueError("unsupported RF BLE protocol version")
        op = msg.get("op")
        event_id = str(msg.get("event_id", ""))
        if op == "hello":
            return _reply("hello_ack", protocol=PROTOCOL_VERSION, chunk_size=self.chunk_size)
        if op == "manifest":
            return _reply("manifest_ack", events=self.store.manifest())
        if op == "event_begin":
            if not event_id:
                raise ValueError("event_begin requires event_id")
            # Existing IDs are safe to replay and can be acknowledged directly.
            if event_id in self.store.events:
                return _reply("ack", event_id, offset=0, duplicate=True)
            metadata = msg.get("metadata")
            if not isinstance(metadata, dict):
                raise ValueError("event_begin requires metadata")
            if event_id in self.receiver._metadata:
                return _reply("resume", event_id, offset=self._offset(event_id))
            actual_id = self.receiver.begin(metadata)
            if actual_id != event_id:
                raise ValueError("event id does not match metadata")
            return _reply("resume", event_id, offset=self._offset(event_id))
        if op == "event_resume":
            if event_id in self.store.events:
                return _reply("ack", event_id, duplicate=True, offset=0)
            return _reply("resume", event_id, offset=self._offset(event_id))
        if op == "event_chunk":
            data = base64.b64decode(msg.get("data", ""), validate=True)
            offset = int(msg.get("offset", -1))
            size = self.receiver.receive_chunk(event_id, offset, data)
            return _reply("chunk_ack", event_id, offset=size)
        if op == "event_commit":
            checksum = msg.get("sha256")
            committed = self.receiver.commit(event_id, checksum)
            return _reply("ack", committed, offset=0)
        raise ValueError("unknown RF BLE operation: %s" % op)

    # Common adapter spelling.
    handle_frame = receive

    def _offset(self, event_id: str) -> int:
        return len(self.receiver._buffers.get(event_id, ()))

    def iter_upload(self, event_ids: Optional[Iterable[str]] = None):
        """Yield begin/chunk/commit frames for pending events."""
        selected = event_ids if event_ids is not None else [e.event_id for e in self.store.pending()]
        for eid in selected:
            event = self.store.events.get(eid)
            if event is None or event.upload_state != "pending":
                continue
            payload = b"".join(data for _, data in self.store.chunks(eid))
            yield _reply("event_begin", eid, metadata=event.to_dict(), size=len(payload),
                         sha256=hashlib.sha256(payload).hexdigest())
            if payload:
                for offset in range(0, len(payload), self.chunk_size):
                    chunk = payload[offset:offset + self.chunk_size]
                    yield _reply("event_chunk", eid, offset=offset,
                                 data=base64.b64encode(chunk).decode("ascii"))
            yield _reply("event_commit", eid, sha256=hashlib.sha256(payload).hexdigest())

    def iter_upload_bytes(self, event_ids: Optional[Iterable[str]] = None):
        for frame in self.iter_upload(event_ids):
            yield encode_frame(frame)


# Explicit desktop naming for callers that prefer it.
DesktopRfTransport = RfBleTransport

__all__ = ["PROTOCOL_VERSION", "DEFAULT_CHUNK_SIZE", "encode_frame", "decode_frame",
           "RfBleTransport", "DesktopRfTransport"]

