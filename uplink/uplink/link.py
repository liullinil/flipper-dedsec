"""BLE link to the Flipper app: find "DedSec <name>", connect (no pairing), push text frames."""
import asyncio
import logging
import threading
import time

from bleak import BleakClient, BleakScanner

RX_UUID = "de5ec001-1d00-4a1e-8b5e-0f11e7ca1000"  # PC -> Flipper (write)
TX_UUID = "de5ec002-1d00-4a1e-8b5e-0f11e7ca1000"  # Flipper -> PC (notify)
ADV_UUID = "0000ded5-0000-1000-8000-00805f9b34fb"
NAME_PREFIX = "DedSec"

log = logging.getLogger("uplink.link")


class Link:
    """Runs its own asyncio loop in a thread. `frame_source()` returns telemetry lines (sent every
    `period` s); `urgent_source()` returns lines that must go out at once (command output, update
    chunks) and is polled every few tens of ms; `on_rx(line)` receives Flipper -> PC lines."""

    def __init__(self, frame_source, on_status=None, on_rx=None, urgent_source=None, period=1.0):
        self.frame_source = frame_source
        self.urgent_source = urgent_source or (lambda: [])
        self.on_status = on_status or (lambda status, name: None)
        self.on_rx = on_rx or (lambda line: None)
        self.period = period
        self.status = "starting"
        self.device_name = ""
        self._rxbuf = ""
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._thread = threading.Thread(target=self._run, name="ble-link", daemon=True)

    # ------------------------------------------------------------------ control
    def start(self):
        self._thread.start()

    def stop(self, timeout=6.0):
        self._stop.set()
        self._thread.join(timeout)

    def set_paused(self, paused):
        (self._paused.set if paused else self._paused.clear)()

    @property
    def paused(self):
        return self._paused.is_set()

    def _set(self, status, name=None):
        if name is not None:
            self.device_name = name
        if status != self.status:
            log.info("link: %s %s", status, self.device_name)
        self.status = status
        try:
            self.on_status(status, self.device_name)
        except Exception:  # never let the UI kill the link
            log.exception("status callback failed")

    # ------------------------------------------------------------------ loop
    def _run(self):
        asyncio.run(self._main())

    @staticmethod
    def _match(dev, adv):
        name = adv.local_name or dev.name or ""
        return name.startswith(NAME_PREFIX) or ADV_UUID in [u.lower() for u in adv.service_uuids]

    async def _main(self):
        backoff = 2
        while not self._stop.is_set():
            if self._paused.is_set():
                self._set("paused")
                await asyncio.sleep(0.5)
                continue
            self._set("searching")
            try:
                dev = await BleakScanner.find_device_by_filter(self._match, timeout=8.0)
            except Exception as exc:
                log.warning("scan failed: %s", exc)
                self._set("no bluetooth")
                await asyncio.sleep(10)
                continue
            if dev is None:
                await self._nap(4)
                continue
            try:
                await self._session(dev)
                backoff = 2
            except Exception as exc:
                log.warning("link error: %s", exc)
                self._set("error")
                await self._nap(backoff)
                backoff = min(30, backoff * 2)

    async def _nap(self, seconds):
        end = time.time() + seconds
        while time.time() < end and not self._stop.is_set():
            await asyncio.sleep(0.25)

    async def _session(self, dev):
        lost = asyncio.Event()
        loop = asyncio.get_running_loop()

        def on_disconnect(_client):
            loop.call_soon_threadsafe(lost.set)

        async with BleakClient(dev, timeout=20.0, disconnected_callback=on_disconnect) as client:
            self._set("connected", dev.name or dev.address)
            chunk = max(20, (client.mtu_size or 23) - 3)
            self._rxbuf = ""

            def on_tx(_handle, data):
                self._rxbuf += bytes(data).decode("utf-8", "replace")
                while "\n" in self._rxbuf:
                    line, self._rxbuf = self._rxbuf.split("\n", 1)
                    line = line.strip("\r")
                    if line:
                        self.on_rx(line)
                if "\n" not in self._rxbuf and self._rxbuf:
                    # the Flipper sends one line per notification without a newline
                    line, self._rxbuf = self._rxbuf, ""
                    self.on_rx(line.strip("\r"))

            try:
                await client.start_notify(TX_UUID, on_tx)
            except Exception as exc:
                log.warning("cannot subscribe TX (no remote cmd): %s", exc)
            next_frame = 0.0
            while not (self._stop.is_set() or lost.is_set() or self._paused.is_set()):
                now = time.time()
                lines = list(self.urgent_source())
                if now >= next_frame:
                    lines += self.frame_source()
                    next_frame = now + self.period
                for line in lines:
                    data = (line + "\n").encode("utf-8", "replace")
                    for i in range(0, len(data), chunk):
                        await client.write_gatt_char(RX_UUID, data[i:i + chunk], response=False)
                await asyncio.sleep(0.02 if lines else 0.05)
            if client.is_connected and (self._stop.is_set() or self._paused.is_set()):
                try:
                    await client.write_gatt_char(RX_UUID, b"B\n", response=False)
                    await asyncio.sleep(0.2)
                except Exception:
                    pass
        self._set("searching" if not self._stop.is_set() else "stopped")
