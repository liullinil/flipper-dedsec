"""Blackout: the companion's own lock screen for the PC, driven from the Flipper.

The Flipper's SYS tab sends ``BO|1``: every window is minimised, the sound is muted and a DedSec
lock screen covers every monitor until the PIN is typed (or the Flipper sends ``BO|0``: the
Flipper is the key).  On unlock the windows come back and the sound is unmuted when it was on.

Auto-blackout (off by default, a tray toggle) does the same when the Flipper's link is lost:
the owner walked away with the Flipper in their pocket.  It arms ten minutes after the companion
starts, so after a reboot there is time to turn it off; a Flipper app that is closed on purpose
says ``BB`` first and never triggers it.

This module holds the decision logic (testable without Tk) and the Windows helpers; the screen
itself is :mod:`uplink.blackout_screen` on the UI thread.
"""
import ctypes
import hashlib
import logging
import os
import secrets
import subprocess
import threading
import time

log = logging.getLogger("uplink.blackout")

ARM_AFTER = 10 * 60      # seconds after start before auto-blackout may fire
LINK_GRACE = 45          # seconds the link may be gone before auto-blackout fires
PIN_ROUNDS = 200_000
PIN_MIN, PIN_MAX = 4, 12

AUTO_OFF, AUTO_ARMING, AUTO_ARMED = 0, 1, 2


# --------------------------------------------------------------------------- PIN
def hash_pin(pin, salt=None):
    """``{"salt": hex, "hash": hex}`` for ``pin`` (digits as typed on the lock screen)."""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), bytes.fromhex(salt), PIN_ROUNDS)
    return {"salt": salt, "hash": digest.hex()}


def check_pin(stored, pin):
    if not stored or not isinstance(stored, dict) or not pin:
        return False
    try:
        candidate = hash_pin(pin, stored["salt"])["hash"]
    except (KeyError, ValueError, TypeError):
        return False
    return secrets.compare_digest(candidate, str(stored.get("hash", "")))


def pin_problem(pin):
    """Why ``pin`` cannot be used, or ``None``."""
    if not pin or not pin.isdigit():
        return "digits only"
    if len(pin) < PIN_MIN:
        return f"at least {PIN_MIN} digits"
    if len(pin) > PIN_MAX:
        return f"at most {PIN_MAX} digits"
    return None


# --------------------------------------------------------------------------- Windows
class Desktop:
    """What the lock does to the desktop: windows away, sound off, and back again."""

    def __init__(self):
        self._minimised = []
        self._foreground = None
        self._muted_by_us = False
        self._audio = None

    # ---- windows
    def hide_windows(self):
        if os.name != "nt":
            return
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        pid = os.getpid()
        own = wintypes.DWORD()
        self._foreground = user32.GetForegroundWindow()
        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def each(hwnd, _lparam):
            try:
                if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                    return True
                if user32.GetWindowTextLengthW(hwnd) == 0:
                    return True
                if user32.GetWindowLongW(hwnd, -20) & 0x80:      # WS_EX_TOOLWINDOW
                    return True
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(own))
                if own.value == pid:
                    return True
                buf = ctypes.create_unicode_buffer(64)
                user32.GetClassNameW(hwnd, buf, 64)
                if buf.value in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
                                 "Windows.UI.Core.CoreWindow"):
                    return True
                found.append(hwnd)
            except Exception:
                pass
            return True

        user32.EnumWindows(each, 0)
        self._minimised = []
        for hwnd in found:
            if user32.ShowWindow(hwnd, 6):      # SW_MINIMIZE
                self._minimised.append(hwnd)
        log.info("blackout: %d windows minimised", len(self._minimised))

    def restore_windows(self):
        if os.name != "nt":
            return
        user32 = ctypes.windll.user32
        for hwnd in reversed(self._minimised):
            try:
                if user32.IsWindow(hwnd) and user32.IsIconic(hwnd):
                    user32.ShowWindow(hwnd, 9)   # SW_RESTORE
            except Exception:
                pass
        self._minimised = []
        if self._foreground:
            try:
                if user32.IsWindow(self._foreground):
                    user32.SetForegroundWindow(self._foreground)
            except Exception:
                pass
            self._foreground = None

    # ---- sound
    def mute(self):
        """Mute the default playback device; remembers whether it was already muted."""
        if os.name != "nt":
            return
        try:
            audio = self._endpoint()
            if audio is None:
                return
            if audio.get_mute():
                self._muted_by_us = False
                return
            audio.set_mute(True)
            self._muted_by_us = True
            log.info("blackout: sound muted")
        except Exception:
            log.debug("mute failed", exc_info=True)

    def unmute(self):
        if os.name != "nt" or not self._muted_by_us:
            return
        try:
            audio = self._endpoint()
            if audio is not None:
                audio.set_mute(False)
                log.info("blackout: sound restored")
        except Exception:
            log.debug("unmute failed", exc_info=True)
        self._muted_by_us = False

    def _endpoint(self):
        if self._audio is None:
            self._audio = EndpointVolume()
        return self._audio if self._audio.ok else None


class EndpointVolume:
    """IAudioEndpointVolume of the default output through raw COM vtables (no comtypes)."""

    _CLSID_ENUMERATOR = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
    _IID_ENUMERATOR = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
    _IID_ENDPOINT_VOLUME = "{5CDF2C82-841E-4546-9722-0CF74078229A}"

    def __init__(self):
        self.ok = False
        self._volume = None
        try:
            self._open()
            self.ok = True
        except Exception:
            log.debug("audio endpoint unavailable", exc_info=True)

    @staticmethod
    def _guid(text):
        import uuid
        raw = uuid.UUID(text).bytes_le
        return (ctypes.c_ubyte * 16).from_buffer_copy(raw)

    @staticmethod
    def _method(obj, index, restype, *argtypes):
        vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.c_void_p))[0]
        function = ctypes.cast(vtable, ctypes.POINTER(ctypes.c_void_p))[index]
        prototype = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
        return prototype(function)

    def _open(self):
        ole32 = ctypes.windll.ole32
        ole32.CoInitializeEx(None, 2)            # apartment threaded; S_FALSE when already done
        enumerator = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(self._guid(self._CLSID_ENUMERATOR), None, 23,
                                    self._guid(self._IID_ENUMERATOR), ctypes.byref(enumerator))
        if hr != 0 or not enumerator:
            raise OSError(f"CoCreateInstance failed: {hr:#x}")
        device = ctypes.c_void_p()
        get_default = self._method(enumerator, 4, ctypes.c_long, ctypes.c_int, ctypes.c_int,
                                   ctypes.POINTER(ctypes.c_void_p))
        hr = get_default(enumerator, 0, 0, ctypes.byref(device))   # eRender, eConsole
        self._method(enumerator, 2, ctypes.c_ulong)(enumerator)    # Release
        if hr != 0 or not device:
            raise OSError(f"GetDefaultAudioEndpoint failed: {hr:#x}")
        volume = ctypes.c_void_p()
        activate = self._method(device, 3, ctypes.c_long, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p,
                                ctypes.POINTER(ctypes.c_void_p))
        hr = activate(device, self._guid(self._IID_ENDPOINT_VOLUME), 23, None, ctypes.byref(volume))
        self._method(device, 2, ctypes.c_ulong)(device)
        if hr != 0 or not volume:
            raise OSError(f"Activate(IAudioEndpointVolume) failed: {hr:#x}")
        self._volume = volume

    def get_mute(self):
        muted = ctypes.c_int()
        hr = self._method(self._volume, 15, ctypes.c_long, ctypes.POINTER(ctypes.c_int))(
            self._volume, ctypes.byref(muted))
        if hr != 0:
            raise OSError(f"GetMute failed: {hr:#x}")
        return bool(muted.value)

    def set_mute(self, on):
        hr = self._method(self._volume, 14, ctypes.c_long, ctypes.c_int, ctypes.c_void_p)(
            self._volume, 1 if on else 0, None)
        if hr != 0:
            raise OSError(f"SetMute failed: {hr:#x}")


def power_off():
    """Shut the PC down (what the lock screen's POWER OFF does after its confirmation)."""
    if os.name != "nt":
        return
    subprocess.Popen(["shutdown", "/s", "/t", "1"], creationflags=0x08000000)


# --------------------------------------------------------------------------- controller
class Blackout:
    """The state: active or not, the auto-blackout arming, and what the Flipper said.

    ``ui`` is the companion's :class:`uplink.ui.UiThread` (``call(fn)``), ``cfg``/``save`` the
    config dict and its writer, ``desktop`` a :class:`Desktop` (a fake in tests) and
    ``screen_factory(blackout)`` builds the lock screen on the UI thread.  ``clock`` is
    injectable for tests."""

    def __init__(self, ui, cfg, save, desktop=None, screen_factory=None, clock=time.time):
        self.ui = ui
        self.cfg = cfg
        self.save = save
        self.desktop = desktop or Desktop()
        self.screen_factory = screen_factory
        self.clock = clock
        self.started = clock()
        self.active = False
        self.reason = ""
        self.link_up = False
        self.link_lost_at = None
        self.flipper_said_bye = False
        self.fails = 0
        self.locked_at = 0.0
        self.unlocked_at = 0.0
        self._screen = None
        self._lock = threading.Lock()
        self.on_change = None           # callable(): the tray wants to redraw

    # ------------------------------------------------------------------ settings
    @property
    def auto_enabled(self):
        return bool(self.cfg.get("blackout_auto"))

    def set_auto(self, on):
        self.cfg["blackout_auto"] = bool(on)
        self.save(self.cfg)
        log.info("auto-blackout %s", "on" if on else "off")
        self._changed()

    def auto_state(self):
        if not self.auto_enabled:
            return AUTO_OFF
        return AUTO_ARMED if self.clock() - self.started >= ARM_AFTER else AUTO_ARMING

    def arming_left(self):
        """Seconds until auto-blackout is armed (0 when armed)."""
        return max(0, int(ARM_AFTER - (self.clock() - self.started)))

    def pin_set(self):
        return isinstance(self.cfg.get("blackout_pin"), dict) and "hash" in self.cfg["blackout_pin"]

    def set_pin(self, pin):
        self.cfg["blackout_pin"] = hash_pin(pin)
        self.save(self.cfg)
        log.info("blackout PIN set")

    def check_pin(self, pin):
        return check_pin(self.cfg.get("blackout_pin"), pin)

    # ------------------------------------------------------------------ the link (BLE thread)
    def on_link(self, status):
        up = status == "connected"
        if up:
            self.link_up = True
            self.link_lost_at = None
            self.flipper_said_bye = False
        elif self.link_up:
            self.link_up = False
            # a paused uplink is the user's doing, not a Flipper that walked away
            self.link_lost_at = None if status == "paused" else self.clock()

    def handle_line(self, parts):
        """Flipper -> PC: ``BO|1`` / ``BO|0`` / ``BB``.  True when the line was ours."""
        tag = parts[0]
        if tag == "BB":
            self.flipper_said_bye = True
            self.link_lost_at = None
            log.info("the Flipper app is closing on purpose")
            return True
        if tag == "BO" and len(parts) >= 2:
            if parts[1] == "1":
                self.lock("flipper")
            elif parts[1] == "0":
                self.unlock("flipper")
            return True
        return False

    def frame_line(self):
        """The ``BO|active|auto`` line of every frame."""
        return f"BO|{1 if self.active else 0}|{self.auto_state()}"

    def tick(self):
        """Once a second: auto-blackout when the Flipper has been gone long enough."""
        if self.active or not self.auto_enabled or self.auto_state() != AUTO_ARMED:
            return False
        if self.link_lost_at is None or self.flipper_said_bye:
            return False
        if self.clock() - self.link_lost_at < LINK_GRACE:
            return False
        self.lock("link lost")
        return True

    # ------------------------------------------------------------------ lock / unlock (any thread)
    def lock(self, reason):
        with self._lock:
            if self.active:
                return False
            self.active = True
            self.reason = reason
            self.locked_at = self.clock()
            self.fails = 0
        log.info("BLACKOUT (%s)", reason)
        self._changed()
        self.ui.call(self._show)
        return True

    def unlock(self, how):
        with self._lock:
            if not self.active:
                return False
            self.active = False
            self.unlocked_at = self.clock()
        log.info("blackout over (%s)", how)
        self._changed()
        self.ui.call(self._hide)
        return True

    def try_pin(self, pin):
        """The lock screen's attempt.  Returns (ok, seconds to wait before the next try)."""
        if self.check_pin(pin) or (not self.pin_set() and pin == ""):
            self.unlock("pin")
            return True, 0.0
        self.fails += 1
        return False, min(10.0, 1.5 * max(0, self.fails - 2))

    # ------------------------------------------------------------------ UI thread
    def _show(self):
        try:
            self.desktop.hide_windows()
            self.desktop.mute()
        except Exception:
            log.exception("blackout: desktop actions failed")
        if self.screen_factory is not None and self._screen is None:
            try:
                self._screen = self.screen_factory(self)
            except Exception:
                log.exception("blackout: cannot build the lock screen")

    def _hide(self):
        screen, self._screen = self._screen, None
        if screen is not None:
            try:
                screen.close()
            except Exception:
                log.exception("blackout: cannot close the lock screen")
        try:
            self.desktop.unmute()
            self.desktop.restore_windows()
        except Exception:
            log.exception("blackout: desktop restore failed")

    def shutdown(self):
        """The companion quits: never leave the desktop hidden."""
        if self.active:
            self.unlock("quit")

    def _changed(self):
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                log.debug("blackout change callback failed", exc_info=True)
