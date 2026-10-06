"""Self-update of the Flipper app over BLE.

Checks the latest GitHub release of liullinil/flipper-dedsec. When the Flipper reports an older
version (V|x.y.z) we advertise it (N|tag|size); when the Flipper asks (U|tag) we download the
.fap and stream it as base64 chunks inside a small ack window:

    UB|tag|size|crc32   -> Flipper opens <its path>.new, answers UA|0
    UD|offset|base64    -> Flipper writes, answers UA|<bytes written>
    UE|tag              -> Flipper checks size + CRC-32, swaps the file in and relaunches

Lost chunks are re-sent (go-back-N) after a short silence; a long stall aborts the transfer.
"""
import base64
import json
import logging
import re
import threading
import time
import urllib.request
import zlib

REPO = "liullinil/flipper-dedsec"
ASSET = "dedsec_uplink.fap"
COMPANION_ASSET = "DedSecUplink.exe"
COMPANION_VERSION = "1.1.0"
CHECK_EVERY = 30 * 60      # seconds between release checks
CHUNK = 192                # raw bytes per chunk (256 base64 chars, fits the Flipper's line buffer)
WINDOW = 4                 # chunks in flight
RETRANSMIT_AFTER = 2.0     # seconds without ack progress -> resend from the last ack
GIVE_UP_AFTER = 20.0       # seconds without any ack -> abort

log = logging.getLogger("uplink.update")


def parse_version(text):
    nums = [int(x) for x in re.findall(r"\d+", text or "")[:3]]
    return tuple(nums + [0] * (3 - len(nums)))


class Updater:
    def __init__(self, repo=REPO):
        self.repo = repo
        self.lock = threading.Lock()
        self.flipper_version = None
        self.latest = None          # {"tag", "url", "size"}
        self.latest_companion = None
        self.last_check = 0.0
        self.checking = False
        self.on_change = None
        self.pending_flipper_request = ""
        self._reset()

    def _reset(self):
        self.active = False
        self.data = b""
        self.tag = ""
        self.began = self.ready = self.ended = False
        self.sent = self.acked = 0
        self.began_t = self.progress_t = 0.0

    # ------------------------------------------------------------------ release check
    def check_async(self, force=False):
        if self.checking or (not force and time.time() - self.last_check < CHECK_EVERY):
            return
        self.checking = True
        threading.Thread(target=self._check, name="release-check", daemon=True).start()

    def _check(self):
        try:
            req = urllib.request.Request(
                f"https://api.github.com/repos/{self.repo}/releases/latest",
                headers={"User-Agent": "dedsec-uplink", "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                rel = json.load(resp)
            assets = {a.get("name"): a for a in rel.get("assets", [])}
            asset = assets.get(ASSET)
            companion = assets.get(COMPANION_ASSET)
            if asset:
                with self.lock:
                    self.latest = {"tag": rel["tag_name"], "url": asset["browser_download_url"],
                                   "size": asset["size"]}
                log.info("latest release %s (%d bytes)", rel["tag_name"], asset["size"])
            if companion:
                with self.lock:
                    self.latest_companion = {
                        "tag": rel["tag_name"], "url": companion["browser_download_url"],
                        "size": companion["size"]}
        except Exception as exc:
            log.warning("release check failed: %s", exc)
        finally:
            self.last_check = time.time()
            self.checking = False
            if self.on_change:
                try:
                    self.on_change()
                except Exception:
                    log.debug("update menu refresh failed", exc_info=True)

    def set_flipper_version(self, version):
        if version != self.flipper_version:
            log.info("flipper app version %s", version)
        self.flipper_version = version

    def advert(self):
        """N|tag|size while the Flipper runs an older version."""
        with self.lock:
            latest = self.latest
            if self.active or not latest or not self.flipper_version:
                return []
            if parse_version(latest["tag"]) <= parse_version(self.flipper_version):
                return []
            return [f"N|{latest['tag']}|{latest['size']}"]

    def flipper_update_available(self):
        with self.lock:
            return bool(self.latest and self.flipper_version and
                        parse_version(self.latest["tag"]) > parse_version(self.flipper_version))

    def companion_update_available(self):
        with self.lock:
            return bool(self.latest_companion and
                        parse_version(self.latest_companion["tag"]) > parse_version(COMPANION_VERSION))

    def version_status(self):
        with self.lock:
            flipper = self.flipper_version or "?"
            latest = self.latest["tag"] if self.latest else "?"
            companion_latest = self.latest_companion["tag"] if self.latest_companion else latest
            return f"Companion v{COMPANION_VERSION} · Flipper v{flipper} · latest {companion_latest}"

    def request_flipper_update(self):
        with self.lock:
            latest = self.latest
            version = self.flipper_version
            if not latest or not version or parse_version(latest["tag"]) <= parse_version(version):
                return False
            self.pending_flipper_request = latest["tag"]
        threading.Thread(target=self.request, args=(latest["tag"],), daemon=True).start()
        return True

    def install_companion_async(self, target_path, callback):
        """Download the release EXE and call callback(temp_path, error)."""
        with self.lock:
            latest = dict(self.latest_companion or {})
        if not latest:
            callback(None, "no companion release is known")
            return

        def worker():
            temp_path = target_path + ".new"
            try:
                req = urllib.request.Request(latest["url"], headers={"User-Agent": "dedsec-uplink"})
                with urllib.request.urlopen(req, timeout=120) as resp:
                    data = resp.read()
                if len(data) != latest["size"]:
                    raise ValueError(f"download size {len(data)} != {latest['size']}")
                with open(temp_path, "wb") as fh:
                    fh.write(data)
                callback(temp_path, None)
            except Exception as exc:
                log.warning("companion update download failed: %s", exc)
                callback(None, str(exc))

        threading.Thread(target=worker, name="companion-update", daemon=True).start()

    # ------------------------------------------------------------------ transfer
    def request(self, tag):
        """Flipper asked for an update (runs in a worker thread: it downloads)."""
        with self.lock:
            latest = self.latest
            if self.active:
                return
        if not latest:
            log.warning("update requested but no release known")
            return
        try:
            req = urllib.request.Request(latest["url"], headers={"User-Agent": "dedsec-uplink"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
        except Exception as exc:
            log.warning("download failed: %s", exc)
            return
        if len(data) != latest["size"]:
            log.warning("download size %d != %d", len(data), latest["size"])
            return
        with self.lock:
            self._reset()
            self.active = True
            self.data = data
            self.tag = latest["tag"]
        log.info("sending %s to the flipper: %d bytes, crc %08x",
                 self.tag, len(data), zlib.crc32(data) & 0xFFFFFFFF)

    def on_ack(self, written):
        with self.lock:
            if not self.active:
                return
            if written < 0:
                log.warning("flipper rejected the update")
                self._reset()
                return
            now = time.time()
            self.ready = True
            if written > self.acked:
                self.acked = written
                self.progress_t = now
            if not self.progress_t:
                self.progress_t = now

    def urgent(self):
        """Lines to send right now (called every ~20-50 ms by the link)."""
        out = []
        with self.lock:
            if self.pending_flipper_request:
                out.append(f"U|{self.pending_flipper_request}")
                self.pending_flipper_request = ""
            if not self.active:
                return out
            now = time.time()
            size = len(self.data)
            if not self.began:
                out.append(f"UB|{self.tag}|{size}|{zlib.crc32(self.data) & 0xFFFFFFFF}")
                self.began, self.began_t = True, now
                return out
            if not self.ready:
                if now - self.began_t > 5:          # no answer to UB: try again
                    self.began = False
                return out
            if now - self.progress_t > GIVE_UP_AFTER:
                log.warning("update stalled at %d/%d, giving up", self.acked, size)
                self._reset()
                return out
            if self.sent > self.acked and now - self.progress_t > RETRANSMIT_AFTER:
                self.sent = self.acked               # go-back-N
                self.progress_t = now
            while self.sent < size and self.sent - self.acked < WINDOW * CHUNK:
                chunk = self.data[self.sent:self.sent + CHUNK]
                out.append(f"UD|{self.sent}|{base64.b64encode(chunk).decode('ascii')}")
                self.sent += len(chunk)
            if self.acked >= size and not self.ended:
                out.append(f"UE|{self.tag}")
                self.ended = True
                log.info("update %s delivered", self.tag)
                self._reset()
        return out

    def status(self):
        with self.lock:
            if self.active and self.data:
                return f"updating {self.tag} {self.acked * 100 // len(self.data)}%"
            if self.latest:
                return f"latest {self.latest['tag']}"
            return ""
