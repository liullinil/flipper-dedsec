"""RF journal import over the DedSec Uplink BLE link.

The RF Hunter is a tab of the DedSec Uplink Flipper app and its journal
records travel over the companion's existing link (see
``docs/rf_integration_contract.md``, sections 2 and 3).  :class:`RfSync` is a
passive protocol engine and never opens a BLE connection itself:

* the link calls :meth:`RfSync.handle_line` with every Flipper line already
  split on ``|`` (BLE thread);
* the link polls :meth:`RfSync.urgent_lines` every 20-50 ms and sends the
  returned lines (link loop);
* the companion reports connection changes with :meth:`RfSync.on_link`;
* the UI calls :meth:`RfSync.status` and :meth:`RfSync.sync_now`.

Import rules:

* one ``RL`` at a time; up to four ``RR`` in flight per record, matched by
  offset, re-requested after a gap or a 3 s timeout; a record is given up
  after 20 s;
* size and CRC-32 are verified, the record is committed durably (fsync of the
  record and of its capture blob) and only then ``RA`` is sent; the round
  waits for ``RK`` and lists the same cursor again (the record left
  ``events/``);
* a record that cannot be imported is skipped with ``cursor = next``, counted
  as failed and reported in ``last_error``; it stays on the Flipper.  A
  record already in the store is acknowledged without a download when the
  stored copy matches, gets its missing capture attached first when it has
  none, and is reported as a conflict (never acknowledged) when it differs;
* store I/O runs inside :meth:`urgent_lines` without holding the lock, so the
  BLE thread and the UI never wait for an fsync.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime
from typing import Callable, Optional

from .rf_hunter import EventStore, RfEvent, default_store_root, validate_event_id

log = logging.getLogger("uplink.rf_sync")

RF_TAGS = ("R", "RI", "RE", "RD", "RK", "RX")
READ_CHUNK = 120            # the Flipper returns at most this many bytes per RR
MAX_INFLIGHT_READS = 4
REQUEST_TIMEOUT = 3.0       # seconds to wait for RI/RE, RD or RK
RECORD_TIMEOUT = 20.0       # give one record up after this long
MAX_ATTEMPTS = 3            # RL / RA sends before giving up
MAX_READ_ERRORS = 8         # RX io replies tolerated while reading one record
MAX_RECORD_BYTES = 1 << 20  # sanity limit for the size announced by RI
AUTO_RETRY_BACKOFF = 30.0   # pause automatic rounds after a round-level failure
CLOCK_INTERVAL = 3600.0     # resend Z| while the link stays up

IDLE, LIST, CHECK, READ, COMMIT, ACK = "idle", "list", "check", "read", "commit", "ack"


def _crc32(data: bytes) -> int:
    return binascii.crc32(data) & 0xFFFFFFFF


def _same_observation(stored: RfEvent, incoming: RfEvent) -> bool:
    """True when two records describe the same capture (identity and time)."""
    return ((stored.device_uuid, stored.session_id, int(stored.sequence_number))
            == (incoming.device_uuid, incoming.session_id, int(incoming.sequence_number))
            and stored.captured_at_utc == incoming.captured_at_utc)


class RfSync:
    """Pull the Flipper RF journal into an :class:`EventStore` (thread-safe)."""

    def __init__(self, store_root: str, *, clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time, send_clock: bool = True):
        self.store_root = os.fspath(store_root)
        self._clock = clock
        self._wall_clock = wall_clock
        self._send_clock = bool(send_clock)
        self._lock = threading.RLock()
        self._out: deque = deque()
        try:
            os.makedirs(self.store_root, exist_ok=True)
        except OSError as exc:
            log.warning("cannot create RF store %s: %s", self.store_root, exc)
        # Only this object writes to the folder, always from urgent_lines();
        # the analyzer opens its own read-only EventStore on the same folder.
        self.store = EventStore(self.store_root)
        if self.store.skipped_records:
            log.warning("RF store %s: %d malformed record(s) skipped", self.store_root,
                        self.store.skipped_records)
        self._handlers = {"R": self._on_status, "RI": self._on_item, "RE": self._on_end,
                          "RD": self._on_data, "RK": self._on_acked, "RX": self._on_error}
        self._status = {
            "pending": None, "stored": None, "free_kb": None, "state": None, "errors": None,
            "imported": 0, "failed": 0, "skipped": 0, "conflicts": 0,
            "last_error": "", "syncing": False, "last_sync": None, "link_up": False,
        }
        self._link_up = False
        self._link_known = False
        self._phase = IDLE
        self._gen = 0
        self._busy = False
        self._manual = False
        self._want_round = False
        self._auto_after = 0.0
        self._clock_sent_at: Optional[float] = None
        self._cursor = 0
        self._item: Optional[dict] = None
        self._sent_at = 0.0
        self._attempts = 0
        self._buf = bytearray()
        self._mask = bytearray()
        self._inflight: dict = {}
        self._record_started = 0.0
        self._restarted = False
        self._read_errors = 0
        self._payload = b""
        self._round_failed = 0
        self._round_acked: set = set()
        self._vanished: set = set()
        self._rejected: dict = {}
        self._malformed = 0
        # RX carries no request id.  Replies arrive in request order, so count
        # the RR replies still owed by a record we left; those RD/RX lines are
        # stale and must not be read as the answer to a later RL or RA.
        self._rr_outstanding = 0
        self._stale = 0
        self._stuck_ack = ""

    # ================================================================== public API
    def handle_line(self, parts) -> bool:
        """Consume one Flipper line split on ``|``; True if it was an RF line."""
        if isinstance(parts, str):
            parts = parts.split("|")
        if not parts:
            return False
        handler = self._handlers.get(str(parts[0]).strip())
        if handler is None:
            return False
        fields = [str(part).strip() for part in parts[1:]]
        with self._lock:
            if not self._link_known:
                # on_link() has not been wired/called yet: a line proves the link is up.
                self._link_up = True
                self._status["link_up"] = True
            try:
                handler(fields, self._clock())
            except Exception:  # a malformed line must never break the BLE callback
                self._malformed += 1
                log.exception("RF line %s failed", parts[0])
        return True

    def urgent_lines(self) -> list:
        """Lines to send now; also drives timeouts and the durable commit."""
        with self._lock:
            self._tick(self._clock())
            job = self._claim_job()
            lines = self._drain()
        if job is None:
            return lines
        try:
            result = self._run_job(job)
        except Exception as exc:  # defensive: never let a store error kill the link loop
            log.exception("RF store job failed")
            result = ("io", f"RF store error: {exc}")
        with self._lock:
            self._busy = False
            self._apply_job(job, result, self._clock())
            lines.extend(self._drain())
        return lines

    def on_link(self, up: bool) -> None:
        """Link up/down; a round in progress is aborted when the link drops."""
        with self._lock:
            self._link_known = True
            changed = bool(up) != self._link_up
            if up:
                if changed:
                    self._clock_sent_at = None  # tell the Flipper the PC clock again
                self._link_up = True
            else:
                self._link_up = False
                self._out.clear()  # requests for the old connection must not leak
                if self._phase != IDLE:
                    self._abort_round(self._clock(), "link lost during RF sync")
            if changed:
                self._stale = 0  # replies of the old connection will never arrive
            self._status["link_up"] = self._link_up

    def sync_now(self) -> None:
        """Start a round even if ``pending`` looks 0 (also retries rejected records)."""
        with self._lock:
            self._auto_after = 0.0
            if self._phase != IDLE or not self._link_up:
                self._want_round = True  # start as soon as the link is up and idle
                return
            self._start_round(self._clock(), manual=True)

    def status(self) -> dict:
        """Snapshot for the UI.

        Keys: ``pending``, ``stored``, ``free_kb``, ``state``, ``errors`` (from the
        last ``R|`` line, ``None`` until one arrived), ``imported``, ``failed``
        (session totals), ``last_error``, ``syncing``, ``last_sync`` (UTC epoch
        seconds of the last completed round or ``None``), plus ``skipped``
        (already imported, acknowledged again), ``conflicts``, ``link_up``,
        ``current_event``/``current_progress`` and ``store_root``.
        """
        with self._lock:
            result = dict(self._status)
            result["syncing"] = self._phase != IDLE
            result["store_root"] = self.store_root
            result["current_event"] = ""
            result["current_progress"] = ""
            if self._item is not None and self._phase in (READ, COMMIT, ACK):
                size = self._item["size"]
                received = size - self._mask.count(0) if self._phase == READ else size
                result["current_event"] = self._item["event_id"]
                result["current_progress"] = f"{received}/{size}"
            return result

    # ================================================================== Flipper lines
    def _bad_line(self, tag: str, fields: list) -> None:
        self._malformed += 1
        log.debug("ignored malformed RF line %s|%s", tag, "|".join(fields)[:120])

    def _on_status(self, fields: list, now: float) -> None:
        """``R|pending|stored|free_kb|state|errors``"""
        try:
            pending, stored, free_kb, state, errors = (int(value) for value in fields[:5])
        except ValueError:
            self._bad_line("R", fields)
            return
        self._status.update(pending=pending, stored=stored, free_kb=free_kb, state=state, errors=errors)
        if pending > 0 and self._phase == IDLE and self._link_up and now >= self._auto_after:
            self._start_round(now, manual=False)

    def _on_item(self, fields: list, now: float) -> None:
        """``RI|next|event_id|size|crc32`` (reply to RL)."""
        if self._phase != LIST:
            return  # a late duplicate of an earlier reply
        try:
            next_cursor = int(fields[0])
        except (IndexError, ValueError):
            self._bad_line("RI", fields)
            return  # the RL timeout asks again
        if next_cursor <= self._cursor:
            self._abort_round(now, f"Flipper list cursor did not advance ({self._cursor} -> {next_cursor})",
                              backoff=True)
            return
        item = {"cursor": self._cursor, "next": next_cursor,
                "event_id": fields[1] if len(fields) > 1 else "?", "size": 0, "crc32": 0}
        try:
            item["event_id"] = validate_event_id(fields[1])
            item["size"] = int(fields[2])
            item["crc32"] = int(fields[3])
        except (IndexError, ValueError):
            self._stale = 0
            self._item = item
            self._fail_item(now, f"invalid Flipper list entry at cursor {self._cursor}: "
                                 f"{'|'.join(fields)[:80]}")
            return
        event_id = item["event_id"]
        if not 0 < item["size"] <= MAX_RECORD_BYTES or not 0 <= item["crc32"] <= 0xFFFFFFFF:
            self._stale = 0
            self._item = item
            self._fail_item(now, f"record {event_id} announces an invalid size/CRC "
                                 f"({item['size']}, {item['crc32']})", permanent=True)
            return
        if event_id in self._round_acked:
            # A stale reply to an earlier RL, or a record the Flipper failed to
            # remove: keep waiting; repeated replies end in the RL timeout.
            log.debug("ignoring RI for already acknowledged %s", event_id)
            self._stuck_ack = event_id
            return
        self._stale = 0  # in-order replies: everything owed before this one is gone
        if not self._manual and self._rejected.get(event_id) == (item["size"], item["crc32"]):
            # Unchanged record that failed before: skip without downloading it
            # again; "Sync now" retries it.
            self._round_failed += 1
            self._cursor = next_cursor
            self._send_list(now)
            return
        self._item = item
        self._phase = CHECK  # the store lookup runs in urgent_lines()

    def _on_end(self, _fields: list, now: float) -> None:
        """``RE``: no record at the requested cursor - the round is complete."""
        if self._phase == LIST:
            self._stale = 0
            self._finish_round(now)

    def _on_data(self, fields: list, now: float) -> None:
        """``RD|event_id|offset|base64``"""
        del now
        item = self._item
        if item is None or not fields or fields[0] != item["event_id"]:
            if self._stale:
                self._stale -= 1  # late reply to a read of a record we left
            return
        self._rr_outstanding = max(0, self._rr_outstanding - 1)
        if self._phase != READ or len(fields) < 3:
            return  # a duplicate read answered after the record was complete
        try:
            offset = int(fields[1])
            data = base64.b64decode(fields[2], validate=True)
        except ValueError:  # includes binascii.Error
            self._bad_line("RD", fields)
            return
        if not data or offset < 0 or offset + len(data) > item["size"]:
            self._bad_line("RD", fields)
            return
        self._buf[offset:offset + len(data)] = data
        self._mask[offset:offset + len(data)] = b"\x01" * len(data)
        self._inflight.pop(offset, None)

    def _on_acked(self, fields: list, now: float) -> None:
        """``RK|event_id``: the record left ``events/`` on the Flipper."""
        item = self._item
        if self._phase == ACK and item is not None and fields and fields[0] == item["event_id"]:
            self._stale = 0
            self._record_done(now)

    def _on_error(self, fields: list, now: float) -> None:
        """``RX|code|text``: nf, bad, io or off."""
        code = fields[0].lower() if fields else ""
        text = "|".join(fields[1:]).strip()
        detail = f"{code}: {text}" if text else (code or "error")
        phase, item = self._phase, self._item
        if phase == IDLE:
            return
        if phase != READ:
            # Replies come in request order, so reads sent before this phase's
            # request are answered first.
            if self._rr_outstanding:
                self._rr_outstanding -= 1  # a duplicate read of the current record
                return
            if self._stale:
                self._stale -= 1  # a read of a record we already left
                return
        if code == "off":
            self._abort_round(now, "RF sync is disabled on the Flipper", backoff=True)
        elif phase == LIST:
            self._retry_list(now, f"Flipper could not list records ({detail})")
        elif phase == READ and item is not None:
            self._rr_outstanding = max(0, self._rr_outstanding - 1)
            if self._inflight:
                # Replies come in request order: the oldest open read failed.
                # Asking again right away beats waiting for its timeout.
                del self._inflight[next(iter(self._inflight))]
            if code == "nf" and item["event_id"] not in self._vanished:
                # Deleted on the Flipper while we read it: the next record moved
                # to this directory index, so list the same cursor again (once).
                self._vanished.add(item["event_id"])
                self._clear_record()
                self._send_list(now)
            elif code in ("nf", "bad"):
                self._fail_item(now, f"Flipper refused to read {item['event_id']} ({detail})")
            else:
                self._read_errors += 1
                if self._read_errors > MAX_READ_ERRORS:
                    self._fail_item(now, f"reading {item['event_id']} failed ({detail})")
                # otherwise the missing chunk is re-requested after its timeout
        elif phase == ACK and item is not None:
            if code == "nf":
                # Already gone from events/ (an earlier RA whose RK was lost).
                self._record_done(now)
            elif code == "bad":
                self._fail_item(now, f"Flipper rejected the acknowledgement of {item['event_id']} ({detail})")
            else:
                self._resend_ack(now, f"acknowledging {item['event_id']} failed ({detail})")
        # CHECK/COMMIT: no request is outstanding, so the error is stale.

    # ================================================================== round control
    def _start_round(self, now: float, manual: bool) -> None:
        self._gen += 1
        self._manual = manual
        if manual:
            self._rejected.clear()  # an explicit sync retries every record
        self._want_round = False
        self._cursor = 0
        self._round_failed = 0
        self._round_acked = set()
        self._vanished = set()
        self._clear_record()
        self._status["syncing"] = True
        log.info("RF sync round started (%s)", "manual" if manual else "pending on Flipper")
        self._send_list(now)

    def _finish_round(self, now: float) -> None:
        del now
        self._gen += 1
        self._phase = IDLE
        self._clear_record()
        self._status["syncing"] = False
        self._status["last_sync"] = self._wall_clock()
        if not self._round_failed:
            self._status["last_error"] = ""
        log.info("RF sync round done: imported %d, failed %d (session totals)",
                 self._status["imported"], self._status["failed"])

    def _abort_round(self, now: float, message: str, backoff: bool = False) -> None:
        self._gen += 1
        self._phase = IDLE
        self._clear_record()
        self._status["syncing"] = False
        if message:
            self._set_error(message)
        if backoff:
            self._auto_after = now + AUTO_RETRY_BACKOFF

    def _set_error(self, message: str) -> None:
        self._status["last_error"] = message
        log.warning("RF sync: %s", message)

    def _clear_record(self) -> None:
        # Replies still owed by this record's reads are stale from now on.
        self._stale += self._rr_outstanding
        self._rr_outstanding = 0
        self._item = None
        self._buf = bytearray()
        self._mask = bytearray()
        self._inflight = {}
        self._payload = b""
        self._restarted = False
        self._read_errors = 0

    def _send_list(self, now: float) -> None:
        self._phase = LIST
        self._attempts = 1
        self._sent_at = now
        self._stuck_ack = ""
        self._out.append(f"RL|{self._cursor}")

    def _retry_list(self, now: float, reason: str) -> None:
        if self._attempts >= MAX_ATTEMPTS:
            if self._stuck_ack:
                reason = f"the Flipper still lists acknowledged record {self._stuck_ack}"
            self._abort_round(now, f"{reason}; giving up the round", backoff=True)
            return
        self._attempts += 1
        self._sent_at = now
        self._out.append(f"RL|{self._cursor}")

    def _start_read(self, now: float) -> None:
        size = self._item["size"]
        self._phase = READ
        self._buf = bytearray(size)
        self._mask = bytearray(size)
        self._inflight = {}  # offset -> send time, in send order
        self._record_started = now
        self._restarted = False
        self._read_errors = 0

    def _send_ack(self, now: float) -> None:
        item = self._item
        self._phase = ACK
        self._attempts = 1
        self._sent_at = now
        self._out.append(f"RA|{item['event_id']}|{item['size']}|{item['crc32']}")

    def _resend_ack(self, now: float, reason: str) -> None:
        if self._attempts >= MAX_ATTEMPTS:
            self._fail_item(now, reason)
            return
        item = self._item
        self._attempts += 1
        self._sent_at = now
        self._out.append(f"RA|{item['event_id']}|{item['size']}|{item['crc32']}")

    def _record_done(self, now: float) -> None:
        item = self._item
        if item.get("outcome") == "verified":
            self._status["skipped"] += 1
        else:
            self._status["imported"] += 1
        self._round_acked.add(item["event_id"])
        self._rejected.pop(item["event_id"], None)
        self._clear_record()
        self._send_list(now)  # the record left events/: same cursor

    def _fail_item(self, now: float, message: str, permanent: bool = False, conflict: bool = False) -> None:
        item = self._item or {}
        self._status["failed"] += 1
        if conflict:
            self._status["conflicts"] += 1
        self._round_failed += 1
        self._set_error(message)
        if permanent and item.get("size"):
            self._rejected[item["event_id"]] = (item["size"], item["crc32"])
        next_cursor = item.get("next")
        self._clear_record()
        if next_cursor is None or next_cursor <= self._cursor:
            self._abort_round(now, message, backoff=True)
            return
        self._cursor = next_cursor  # skip the record; it stays on the Flipper
        self._send_list(now)

    # ================================================================== timers
    def _clock_line(self) -> str:
        utc = int(self._wall_clock())
        try:
            offset = datetime.fromtimestamp(utc).astimezone().utcoffset()
            minutes = int(offset.total_seconds() // 60) if offset is not None else 0
        except (OverflowError, OSError, ValueError):
            minutes = 0
        return f"Z|{utc}|{minutes}"

    def _tick(self, now: float) -> None:
        if (self._link_up and self._send_clock
                and (self._clock_sent_at is None or now - self._clock_sent_at >= CLOCK_INTERVAL)):
            self._out.appendleft(self._clock_line())  # the clock goes before any request
            self._clock_sent_at = now
        phase = self._phase
        if phase == IDLE:
            if self._want_round and self._link_up:
                self._start_round(now, manual=True)
        elif phase == LIST:
            if now - self._sent_at >= REQUEST_TIMEOUT:
                self._retry_list(now, f"no reply to RL|{self._cursor}")
        elif phase == READ:
            self._tick_read(now)
        elif phase == ACK:
            if now - self._sent_at >= REQUEST_TIMEOUT:
                self._resend_ack(now, f"no RK for {self._item['event_id']}")

    def _tick_read(self, now: float) -> None:
        item = self._item
        event_id = item["event_id"]
        if self._mask.find(0) < 0:
            payload = bytes(self._buf)
            if _crc32(payload) == item["crc32"]:
                self._payload = payload
                self._phase = COMMIT  # urgent_lines() commits it right away
                return
            if self._restarted:
                self._fail_item(now, f"checksum mismatch for {event_id} (size {item['size']})", permanent=True)
                return
            # One fresh download: a chunk may have come from an older request.
            log.warning("RF sync: checksum mismatch for %s, downloading it again", event_id)
            self._start_read(now)
            self._restarted = True
        if now - self._record_started >= RECORD_TIMEOUT:
            received = item["size"] - self._mask.count(0)
            self._fail_item(now, f"timed out reading {event_id} ({received}/{item['size']} bytes)")
            return
        for offset, sent in list(self._inflight.items()):
            if now - sent >= REQUEST_TIMEOUT:
                del self._inflight[offset]  # lost or late: ask again below
        position = 0
        while len(self._inflight) < MAX_INFLIGHT_READS:
            position = self._mask.find(0, position)
            if position < 0:
                break
            covered = [offset + READ_CHUNK for offset in self._inflight
                       if offset <= position < offset + READ_CHUNK]
            if covered:
                position = max(covered)
                continue
            self._inflight[position] = now
            self._rr_outstanding += 1
            self._out.append(f"RR|{event_id}|{position}")
            position += READ_CHUNK

    def _drain(self) -> list:
        lines = list(self._out)
        self._out.clear()
        return lines

    # ================================================================== store jobs
    def _claim_job(self) -> Optional[tuple]:
        if self._busy or self._phase not in (CHECK, COMMIT) or self._item is None:
            return None
        self._busy = True
        return (self._phase, self._gen, dict(self._item), self._payload)

    def _run_job(self, job: tuple) -> tuple:
        """Store I/O for one record; runs without the lock."""
        phase, _gen, item, payload = job
        event_id = item["event_id"]
        store = self.store
        if phase == CHECK:
            existing = store.events.get(event_id)
            if existing is None:
                return ("new", "")
            stored = store.read_capture(existing)
            if not stored:
                return ("attach", "")
            if len(stored) == item["size"] and _crc32(stored) == item["crc32"]:
                return ("verified", "")
            return ("conflict", f"conflict: {event_id} on the Flipper differs from the imported copy "
                                f"(not acknowledged)")
        try:
            event = RfEvent.from_dict(json.loads(payload.decode("utf-8-sig")))
        except Exception as exc:
            return ("reject", f"malformed record {event_id}: {exc}")
        if event.event_id != event_id:
            return ("reject", f"record {event_id} contains event_id {event.event_id}")
        try:
            existing = store.events.get(event_id)
            if existing is None:
                event.capture_blob = ""  # the desktop decides where the raw bytes live
                event.upload_state = "imported"
                store.add(event, capture=payload)
                return ("imported", "")
            stored = store.read_capture(existing)
            if stored:
                if stored == payload:
                    return ("attached", "")
                return ("conflict", f"conflict: {event_id} on the Flipper differs from the imported copy "
                                    f"(not acknowledged)")
            if not _same_observation(existing, event):
                return ("conflict", f"conflict: {event_id} on the Flipper does not match the stored "
                                    f"observation (not acknowledged)")
            store.attach_capture(event_id, payload)
            return ("attached", "")
        except OSError as exc:
            return ("io", f"cannot write the RF store {self.store_root}: {exc}")
        except ValueError as exc:
            return ("reject", f"cannot import {event_id}: {exc}")

    def _apply_job(self, job: tuple, result: tuple, now: float) -> None:
        phase, gen, _item, _payload = job
        if gen != self._gen or self._phase != phase or self._item is None:
            return  # the round ended meanwhile (link loss); a commit stays valid
        action, detail = result
        if phase == CHECK:
            if action == "verified":
                self._item["outcome"] = "verified"
                self._send_ack(now)
            elif action in ("new", "attach"):
                self._item["outcome"] = "imported" if action == "new" else "attached"
                self._start_read(now)
                self._tick_read(now)  # first reads go out with this poll
            elif action == "conflict":
                self._fail_item(now, detail, permanent=True, conflict=True)
            else:
                self._abort_round(now, detail, backoff=True)
        else:
            if action in ("imported", "attached"):
                self._item["outcome"] = action
                self._send_ack(now)
            elif action == "conflict":
                self._fail_item(now, detail, permanent=True, conflict=True)
            elif action == "reject":
                self._fail_item(now, detail, permanent=True)
            else:
                # The desktop cannot store records (disk full, permissions):
                # stop the round instead of downloading everything for nothing.
                self._abort_round(now, detail, backoff=True)


__all__ = ["RfSync", "default_store_root", "RF_TAGS", "READ_CHUNK", "MAX_INFLIGHT_READS",
           "REQUEST_TIMEOUT", "RECORD_TIMEOUT"]
