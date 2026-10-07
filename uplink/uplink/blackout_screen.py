"""The Blackout lock screen and the PIN dialog (Tk, on the companion's UI thread).

One borderless, topmost window per monitor; the primary one carries the clock, the PIN field and
the POWER OFF button.  A scene picked at random animates behind them, with a random accent palette,
so the screen never looks the same twice.

This is a privacy curtain, not a Windows credential provider, and it deliberately does not fight
the operating system: it never keeps the PC awake (no execution-state request, no synthetic input)
and it never blocks Windows' own lock or power management.  The keyboard hook only swallows Alt+Tab
and Alt+F4 so a stray key cannot tear the curtain down; the Windows key, Win+L, Ctrl+Alt+Del and
sleep shortcuts all pass straight through, so the machine still sleeps and Windows still auto-locks
on their own schedule.
"""
import ctypes
import logging
import math
import os
import random
import socket
import time
from datetime import datetime

from . import blackout as bo
from . import dedsec_ui as ui

log = logging.getLogger("uplink.blackout_screen")

FPS_MS = 70
SLOW_AFTER_S = 180       # after this long the animation slows down (battery; never blocks sleep)
SLOW_FPS_MS = 200
TAGLINES = [
    "WE ARE DEDSEC", "ctOS 2.0  //  ACCESS DENIED", "THIS TERMINAL HAS GONE DARK",
    "YOUR DATA IS NOT FOR SALE", "WRENCH SAYS: TOUCH NOTHING", "BLUME CAN'T SEE YOU HERE",
    "OPERATOR AWAY  ·  FOLLOWERS +1", "HACK THE PLANET, NOT THIS PC", "NOTHING TO SEE  ·  MOVE ALONG",
    "SF BAY AREA  ·  NODE OFFLINE", "SIGNAL LOST  ·  RETRY LATER", "THE KEY IS IN A POCKET",
]
GLYPHS = "0123456789ABCDEF<>/\\|[]{}#$%&*+=-_~^"
# accent colour pairs a blackout picks from, so each one has its own mood
PALETTES = (
    (ui.CYAN, ui.MAGENTA), (ui.ORANGE, ui.CYAN), (ui.GREEN, ui.MAGENTA), (ui.YELLOW, ui.CYAN),
    (ui.MAGENTA, ui.ORANGE), (ui.CYAN, ui.GREEN),
)


def dim(color, factor):
    """A darker shade of a ``#rrggbb`` colour (factor 0..1)."""
    color = color.lstrip("#")
    r, g, b = (int(color[i:i + 2], 16) for i in (0, 2, 4))
    return "#%02x%02x%02x" % (int(r * factor), int(g * factor), int(b * factor))


# --------------------------------------------------------------------------- monitors
def monitors():
    """[(x, y, w, h, primary), ...] of every display; the Tk screen when Win32 is unavailable."""
    found = []
    if os.name == "nt":
        try:
            from ctypes import wintypes
            user32 = ctypes.windll.user32

            class MONITORINFO(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                            ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

            proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                                      ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

            @proc
            def each(handle, _dc, _rect, _lparam):
                info = MONITORINFO()
                info.cbSize = ctypes.sizeof(MONITORINFO)
                if user32.GetMonitorInfoW(handle, ctypes.byref(info)):
                    r = info.rcMonitor
                    found.append((r.left, r.top, r.right - r.left, r.bottom - r.top, bool(info.dwFlags & 1)))
                return True

            user32.EnumDisplayMonitors(None, None, each, 0)
        except Exception:
            log.debug("monitor enumeration failed", exc_info=True)
    return found


# --------------------------------------------------------------------------- keyboard hook
class KeyboardGuard:
    """WH_KEYBOARD_LL hook that swallows only Alt+Tab and Alt+F4 (window switch / close).

    It intentionally lets everything else through, including the Windows key, Win+L, Ctrl+Esc and
    Ctrl+Alt+Del, so the curtain never interferes with Windows' own lock or power management."""

    SWALLOW_ALT = {0x09, 0x73}   # Tab, F4 with Alt held

    def __init__(self):
        self._hook = None
        self._proc = None

    def install(self):
        if os.name != "nt" or self._hook:
            return
        try:
            from ctypes import wintypes
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            LRESULT = ctypes.c_ssize_t
            ULONG_PTR = ctypes.c_size_t
            kernel32.GetModuleHandleW.restype = wintypes.HMODULE
            kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
            user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]

            class KBDLLHOOKSTRUCT(ctypes.Structure):
                _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                            ("flags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

            user32.CallNextHookEx.restype = LRESULT
            user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
            prototype = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
            user32.SetWindowsHookExW.restype = wintypes.HHOOK
            user32.SetWindowsHookExW.argtypes = [ctypes.c_int, prototype, wintypes.HINSTANCE, wintypes.DWORD]

            def proc(code, wparam, lparam):
                try:
                    if code == 0:
                        key = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                        vk = key.vkCode
                        alt = bool(key.flags & 0x20)
                        if alt and vk in self.SWALLOW_ALT:
                            return 1
                except Exception:
                    pass
                return user32.CallNextHookEx(None, code, wparam, lparam)

            self._proc = prototype(proc)
            self._user32 = user32
            self._hook = user32.SetWindowsHookExW(13, self._proc, kernel32.GetModuleHandleW(None), 0)
            if not self._hook:
                log.warning("keyboard guard not installed (error %d)", ctypes.get_last_error())
        except Exception:
            log.debug("keyboard guard failed", exc_info=True)

    def remove(self):
        if self._hook:
            try:
                self._user32.UnhookWindowsHookEx(self._hook)
            except Exception:
                pass
        self._hook = None
        self._proc = None


# --------------------------------------------------------------------------- scenes
class Scene:
    """Draws on a canvas of w x h; ``tick(frame)`` advances the animation.  ``a1``/``a2`` are the
    accent colours of this blackout; ``fx`` is a font size scaled to the screen."""

    name = "scene"

    def __init__(self, canvas, w, h, rnd, palette=None):
        self.c, self.w, self.h, self.rnd = canvas, w, h, rnd
        self.a1, self.a2 = palette or (ui.CYAN, ui.MAGENTA)
        self.fx = max(10, h // 70)

    def tick(self, frame):
        pass

    def wordmark(self, cx, cy, size, tag="word"):
        font = ("Segoe UI Black", size)
        for dx, color in ((max(2, size // 12), ui.MAGENTA), (-max(2, size // 12), ui.CYAN), (0, "#f4fbff")):
            self.c.create_text(cx + dx, cy, text="DEDSEC", fill=color, font=font, tags=tag)

    def slices(self, count, tag="glitch"):
        self.c.delete(tag)
        for _ in range(count):
            y = self.rnd.randint(0, self.h)
            x = self.rnd.randint(0, self.w)
            self.c.create_rectangle(x, y, x + self.rnd.randint(20, self.w // 4), y + self.rnd.choice((1, 2, 3)),
                                    fill=self.rnd.choice((self.a1, self.a2, ui.ORANGE)), outline="", tags=tag)

    def glitch_word(self, frame, every=3):
        """Jitter the DEDSEC wordmark and throw a few neon slices (the common idle animation)."""
        if frame % every == 0:
            self.slices(self.rnd.randint(1, 4))
            shift = self.rnd.choice((0, 0, 0, 2, -2, 4))
            self.c.move("word", shift, 0)
            self.c.after(60, lambda: self.c.move("word", -shift, 0))


class WordmarkScene(Scene):
    name = "wordmark"

    def __init__(self, canvas, w, h, rnd, palette=None):
        super().__init__(canvas, w, h, rnd, palette)
        self.size = max(40, h // 6)
        self.wordmark(w // 2, h * 0.36, self.size)
        self.c.create_text(w // 2, h * 0.36 + self.size * 0.9, text="//  BLACKOUT  //", fill=ui.ORANGE,
                           font=(ui.MONO, max(12, h // 40), "bold"), tags="sub")
        self.scan = self.c.create_rectangle(0, 0, w, 2, fill="#123a44", outline="")
        for i in range(0, h, 4):
            self.c.create_line(0, i, w, i, fill="#070c10", tags="lines")

    def tick(self, frame):
        if frame % 3 == 0:
            self.slices(self.rnd.randint(1, 5))
            shift = self.rnd.choice((0, 0, 0, 2, -2, 4))
            self.c.move("word", shift, 0)
            self.c.after(60, lambda: self.c.move("word", -shift, 0))
        y = (frame * 6) % self.h
        self.c.coords(self.scan, 0, y, self.w, y + 2)


class RainScene(Scene):
    name = "rain"

    def __init__(self, canvas, w, h, rnd, palette=None):
        super().__init__(canvas, w, h, rnd, palette)
        self.font_px = max(12, h // 50)
        self.cols = []
        step = int(self.font_px * 1.3)
        for x in range(step // 2, w, step):
            if rnd.random() < 0.6:
                length = rnd.randint(6, 22)
                item = self.c.create_text(x, rnd.randint(-h, 0), text="", fill=rnd.choice(("#0f6b73", "#148e98", "#0b4c52")),
                                          font=(ui.MONO, self.font_px), anchor="n", justify="center")
                self.cols.append([item, rnd.uniform(3, 11), length])
        self.size = max(40, h // 7)
        self.wordmark(w // 2, h * 0.34, self.size)

    def tick(self, frame):
        for col in self.cols:
            item, speed, length = col
            x, y = self.c.coords(item)
            y += speed
            if y > self.h:
                y = -length * self.font_px * 1.1
                col[1] = self.rnd.uniform(3, 11)
            if frame % 2 == 0:
                self.c.itemconfigure(item, text="\n".join(self.rnd.choice(GLYPHS) for _ in range(length)))
            self.c.coords(item, x, y)
        if frame % 5 == 0:
            self.slices(self.rnd.randint(0, 3))


class ProfilerScene(Scene):
    name = "profiler"
    LINES = ["UNKNOWN OPERATOR", "OCCUPATION: NONE OF YOUR BUSINESS", "INCOME: $0 WHILE AWAY",
             "THREAT: THE PIN", "FOLLOWERS: +1", "STATUS: NOT HERE", "ctOS LINK: SEVERED",
             "NOTE: KEY IN POCKET"]

    def __init__(self, canvas, w, h, rnd, palette=None):
        super().__init__(canvas, w, h, rnd, palette)
        self.size = max(32, h // 9)
        self.wordmark(w // 2, h * 0.24, self.size)
        self.box = [w * 0.3, h * 0.42, w * 0.7, h * 0.72]
        self.frame_id = None
        self.texts = []
        self.target = list(self.box)
        for i in range(4):
            self.texts.append(self.c.create_text(w * 0.5, h * 0.5 + i * (h // 32), text="", fill=ui.CYAN,
                                                 font=(ui.MONO, max(11, h // 60), "bold"), anchor="n"))
        self.dots = [self.c.create_oval(0, 0, 0, 0, fill=ui.MAGENTA, outline="") for _ in range(6)]

    def tick(self, frame):
        if frame % 40 == 0:
            self.target = [self.rnd.uniform(0.1, 0.55) * self.w, self.rnd.uniform(0.38, 0.6) * self.h, 0, 0]
            self.target[2] = self.target[0] + self.rnd.uniform(0.25, 0.4) * self.w
            self.target[3] = self.target[1] + self.rnd.uniform(0.18, 0.3) * self.h
            lines = self.rnd.sample(self.LINES, 4)
            for item, text in zip(self.texts, lines):
                self.c.itemconfigure(item, text=text)
        for i in range(4):
            self.box[i] += (self.target[i] - self.box[i]) * 0.18
        x0, y0, x1, y1 = self.box
        self.c.delete("reticle")
        corner = max(10, self.w // 60)
        for (x, y, dx, dy) in ((x0, y0, 1, 1), (x1, y0, -1, 1), (x0, y1, 1, -1), (x1, y1, -1, -1)):
            self.c.create_line(x, y, x + dx * corner, y, fill=ui.CYAN, width=3, tags="reticle")
            self.c.create_line(x, y, x, y + dy * corner, fill=ui.CYAN, width=3, tags="reticle")
        self.c.create_line(x0, y0 + ((frame * 4) % max(1, int(y1 - y0))), x1, y0 + ((frame * 4) % max(1, int(y1 - y0))),
                           fill="#1a5f68", tags="reticle")
        for i, item in enumerate(self.texts):
            self.c.coords(item, (x0 + x1) / 2, y1 + 8 + i * (self.h // 32))
        for dot in self.dots:
            if self.rnd.random() < 0.1:
                x, y = self.rnd.uniform(0, self.w), self.rnd.uniform(0, self.h)
                self.c.coords(dot, x - 3, y - 3, x + 3, y + 3)
        if frame % 4 == 0:
            self.slices(self.rnd.randint(0, 3))


class SkylineScene(Scene):
    name = "skyline"

    def __init__(self, canvas, w, h, rnd, palette=None):
        super().__init__(canvas, w, h, rnd, palette)
        base = h * 0.78
        x = 0
        self.windows = []
        while x < w:
            bw = rnd.randint(max(20, w // 50), max(40, w // 14))
            bh = rnd.randint(int(h * 0.08), int(h * 0.42))
            self.c.create_rectangle(x, base - bh, x + bw, base, fill="#060d12", outline="#0f2a33")
            for wy in range(int(base - bh) + 6, int(base) - 6, 10):
                for wx in range(x + 4, x + bw - 4, 8):
                    if rnd.random() < 0.25:
                        self.windows.append(self.c.create_rectangle(wx, wy, wx + 3, wy + 4, fill="#1b6f7a", outline=""))
            x += bw + rnd.randint(2, 10)
        self.c.create_line(0, base, w, base, fill=ui.CYAN)
        for i in range(1, 9):
            y = base + i * i * (h - base) / 81
            self.c.create_line(0, y, w, y, fill="#0f2a33")
        for i in range(-10, 11):
            self.c.create_line(w / 2 + i * w / 20, base, w / 2 + i * w / 2, h, fill="#0f2a33")
        self.size = max(40, h // 7)
        self.wordmark(w // 2, h * 0.3, self.size)
        self.scan = self.c.create_line(0, 0, 0, h, fill="#2bd8e0", width=2)

    def tick(self, frame):
        x = (frame * 5) % self.w
        self.c.coords(self.scan, x, 0, x, self.h)
        if frame % 6 == 0:
            for item in self.rnd.sample(self.windows, min(8, len(self.windows))):
                self.c.itemconfigure(item, fill=self.rnd.choice(("#1b6f7a", "#0b2f35", ui.ORANGE, "#27e0e8")))
        if frame % 7 == 0:
            self.slices(self.rnd.randint(0, 2))


class CircuitScene(Scene):
    """PCB traces on a grid with pulses running along them."""
    name = "circuit"

    def __init__(self, canvas, w, h, rnd, palette=None):
        super().__init__(canvas, w, h, rnd, palette)
        grid = max(24, w // 60)
        self.traces = []
        trace = dim(self.a1, 0.35)
        for _ in range(28):
            x, y = rnd.randrange(0, max(grid, w), grid), rnd.randrange(0, max(grid, h), grid)
            points = [(x, y)]
            for _ in range(rnd.randint(3, 8)):
                if rnd.random() < 0.5:
                    x = max(0, min(w, x + rnd.choice((-1, 1)) * grid * rnd.randint(1, 6)))
                else:
                    y = max(0, min(h, y + rnd.choice((-1, 1)) * grid * rnd.randint(1, 5)))
                points.append((x, y))
            self.c.create_line(*[v for p in points for v in p], fill=trace, width=2)
            self.c.create_oval(points[-1][0] - 4, points[-1][1] - 4, points[-1][0] + 4, points[-1][1] + 4,
                               fill=trace, outline="")
            self.traces.append(points)
        for _ in range(5):
            x, y = rnd.randrange(0, max(grid, w - grid * 3), grid), rnd.randrange(0, max(grid, h - grid * 2), grid)
            self.c.create_rectangle(x, y, x + grid * 3, y + grid * 2, fill="#081219", outline=self.a1)
            self.c.create_text(x + grid * 1.5, y + grid, text=rnd.choice(("ctOS", "BLUME", "CPU", "RF", "SoC")),
                               fill=dim(self.a1, 0.7), font=(ui.MONO, self.fx, "bold"))
        self.pulses = [[rnd.randrange(len(self.traces)), rnd.random(),
                        self.c.create_oval(0, 0, 0, 0, fill=self.a2, outline="")] for _ in range(14)]
        self.wordmark(w // 2, h * 0.32, max(40, h // 7))

    def _point(self, points, t):
        segs = len(points) - 1
        pos = t * segs
        i = min(segs - 1, int(pos))
        f = pos - i
        (x0, y0), (x1, y1) = points[i], points[i + 1]
        return x0 + (x1 - x0) * f, y0 + (y1 - y0) * f

    def tick(self, frame):
        for pulse in self.pulses:
            pulse[1] += 0.02
            if pulse[1] >= 1.0:
                pulse[0], pulse[1] = self.rnd.randrange(len(self.traces)), 0.0
            x, y = self._point(self.traces[pulse[0]], pulse[1])
            self.c.coords(pulse[2], x - 4, y - 4, x + 4, y + 4)
        self.glitch_word(frame, every=9)


class RadarScene(Scene):
    """A sweep with fading blips, labelled with the RF Hunter's bands."""
    name = "radar"

    def __init__(self, canvas, w, h, rnd, palette=None):
        super().__init__(canvas, w, h, rnd, palette)
        self.cx, self.cy, self.r = w * 0.5, h * 0.52, min(w, h) * 0.36
        ring = dim(self.a1, 0.35)
        for k in (0.25, 0.5, 0.75, 1.0):
            rr = self.r * k
            self.c.create_oval(self.cx - rr, self.cy - rr, self.cx + rr, self.cy + rr, outline=ring)
        for a in range(0, 360, 30):
            rad = math.radians(a)
            self.c.create_line(self.cx, self.cy, self.cx + self.r * math.cos(rad), self.cy + self.r * math.sin(rad),
                               fill="#0b1f27")
        for text, k, a in (("315", 0.3, 200), ("433.92", 0.55, 320), ("868", 0.85, 60), ("NFC", 0.16, 120)):
            rad = math.radians(a)
            self.c.create_text(self.cx + self.r * k * math.cos(rad), self.cy + self.r * k * math.sin(rad),
                               text=text, fill=dim(self.a1, 0.6), font=(ui.MONO, self.fx))
        self.sweep = self.c.create_line(0, 0, 0, 0, fill=self.a1, width=3)
        self.trail = [self.c.create_line(0, 0, 0, 0, fill=dim(self.a1, 0.6 - i * 0.1), width=2) for i in range(5)]
        self.blips = []
        self.angle = 0.0
        size = max(26, h // 12)
        self.wordmark(w // 2, h * 0.1, size)
        self.c.create_text(w // 2, h * 0.1 + size * 0.85, text="SCANNING  ·  NO OPERATOR IN RANGE",
                           fill=ui.ORANGE, font=(ui.MONO, self.fx, "bold"))

    def tick(self, frame):
        self.angle = (self.angle + 3) % 360
        for i, item in enumerate([self.sweep] + self.trail):
            rad = math.radians(self.angle - i * 2.5)
            self.c.coords(item, self.cx, self.cy, self.cx + self.r * math.cos(rad), self.cy + self.r * math.sin(rad))
        if self.rnd.random() < 0.08:
            rad, k = math.radians(self.angle), self.rnd.uniform(0.1, 0.98)
            x, y = self.cx + self.r * k * math.cos(rad), self.cy + self.r * k * math.sin(rad)
            self.blips.append([self.c.create_oval(x - 5, y - 5, x + 5, y + 5, fill=self.a2, outline=""), 0])
        for blip in list(self.blips):
            blip[1] += 1
            if blip[1] > 110:
                self.c.delete(blip[0])
                self.blips.remove(blip)
            elif blip[1] % 20 == 0:
                self.c.itemconfigure(blip[0], fill=dim(self.a2, max(0.15, 1 - blip[1] / 120)))
        self.glitch_word(frame, every=12)


class SpectrumScene(Scene):
    """A spectrum analyzer: a few steady signals over a noise floor that breathes."""
    name = "spectrum"

    def __init__(self, canvas, w, h, rnd, palette=None):
        super().__init__(canvas, w, h, rnd, palette)
        self.n = 72
        self.x0, self.x1 = w * 0.08, w * 0.92
        self.base, self.height = h * 0.78, h * 0.38
        self.bw = (self.x1 - self.x0) / self.n
        self.bars = [self.c.create_rectangle(0, 0, 0, 0, fill=dim(self.a1, 0.7), outline="") for _ in range(self.n)]
        self.peaks = [self.c.create_line(0, 0, 0, 0, fill=self.a2, width=2) for _ in range(self.n)]
        self.peak = [0.0] * self.n
        self.signals = {rnd.randrange(self.n): rnd.uniform(0.5, 0.95) for _ in range(4)}
        self.c.create_line(self.x0, self.base, self.x1, self.base, fill=self.a1)
        for k in range(1, 5):
            y = self.base - self.height * k / 4
            self.c.create_line(self.x0, y, self.x1, y, fill="#0b1f27")
        size = max(34, h // 9)
        self.wordmark(w // 2, h * 0.16, size)
        self.c.create_text(w // 2, h * 0.16 + size * 0.85, text="RF HUNTER  ·  PASSIVE  ·  NOTHING TRANSMITTED",
                           fill=ui.ORANGE, font=(ui.MONO, self.fx, "bold"))

    def tick(self, frame):
        breath = 0.08 + 0.04 * math.sin(frame / 9.0)
        for i in range(self.n):
            level = breath + self.rnd.random() * 0.12
            if i in self.signals and (frame // 25 + i) % 4 != 0:
                level = self.signals[i] + self.rnd.uniform(-0.05, 0.05)
            if self.rnd.random() < 0.01:
                level += self.rnd.uniform(0.2, 0.6)
            level = max(0.02, min(1.0, level))
            x = self.x0 + i * self.bw
            self.c.coords(self.bars[i], x + 1, self.base - self.height * level, x + self.bw - 1, self.base)
            self.peak[i] = max(level, self.peak[i] - 0.01)
            py = self.base - self.height * self.peak[i]
            self.c.coords(self.peaks[i], x + 1, py, x + self.bw - 1, py)
        self.glitch_word(frame, every=10)


class NetworkScene(Scene):
    """The ctOS node map: nodes, links, packets, one node pulsing."""
    name = "network"

    def __init__(self, canvas, w, h, rnd, palette=None):
        super().__init__(canvas, w, h, rnd, palette)
        self.nodes = [(rnd.uniform(w * 0.06, w * 0.94), rnd.uniform(h * 0.1, h * 0.72)) for _ in range(26)]
        self.edges = []
        link = dim(self.a1, 0.35)
        for i, (x, y) in enumerate(self.nodes):
            near = sorted(range(len(self.nodes)),
                          key=lambda j: (self.nodes[j][0] - x) ** 2 + (self.nodes[j][1] - y) ** 2)
            for j in near[1:3]:
                self.edges.append((i, j))
                self.c.create_line(x, y, self.nodes[j][0], self.nodes[j][1], fill=link)
        for i, (x, y) in enumerate(self.nodes):
            r = 4 if rnd.random() < 0.8 else 7
            self.c.create_oval(x - r, y - r, x + r, y + r, fill=dim(self.a1, 0.8), outline="")
            if rnd.random() < 0.3:
                self.c.create_text(x + 10, y - 10, text="NODE %02X" % rnd.randrange(256), anchor="w",
                                   fill=dim(self.a1, 0.6), font=(ui.MONO, max(9, self.fx - 1)))
        self.packets = [[rnd.randrange(len(self.edges)), rnd.random(), rnd.choice((1, -1)),
                         self.c.create_oval(0, 0, 0, 0, fill=self.a2, outline="")] for _ in range(12)]
        self.pulse = self.c.create_oval(0, 0, 0, 0, outline=self.a2, width=2)
        self.pulse_node = rnd.randrange(len(self.nodes))
        self.wordmark(w // 2, h * 0.84, max(36, h // 8))

    def tick(self, frame):
        for packet in self.packets:
            packet[1] += 0.015
            if packet[1] >= 1.0:
                packet[0], packet[1], packet[2] = self.rnd.randrange(len(self.edges)), 0.0, self.rnd.choice((1, -1))
            i, j = self.edges[packet[0]]
            if packet[2] < 0:
                i, j = j, i
            (x0, y0), (x1, y1) = self.nodes[i], self.nodes[j]
            x, y = x0 + (x1 - x0) * packet[1], y0 + (y1 - y0) * packet[1]
            self.c.coords(packet[3], x - 3, y - 3, x + 3, y + 3)
        r = 6 + (frame % 30)
        if frame % 30 == 0:
            self.pulse_node = self.rnd.randrange(len(self.nodes))
        x, y = self.nodes[self.pulse_node]
        self.c.coords(self.pulse, x - r, y - r, x + r, y + r)
        self.c.itemconfigure(self.pulse, outline=dim(self.a2, max(0.1, 1 - (frame % 30) / 30)))
        self.glitch_word(frame, every=12)


SCENES = (WordmarkScene, RainScene, ProfilerScene, SkylineScene, CircuitScene, RadarScene,
          SpectrumScene, NetworkScene)


# --------------------------------------------------------------------------- the screen
class LockScreen:
    """Built by :class:`uplink.blackout.Blackout` on the UI thread; ``close()`` tears it down."""

    def __init__(self, root, blackout, scene=None):
        import tkinter as tk

        self.root = root
        self.blackout = blackout
        self.rnd = random.Random()
        self.windows = []
        self.canvases = []
        self.scenes = []
        self.frame = 0
        self.pin = ""
        self.alive = True
        self.wait_until = 0.0
        self.power_armed_until = 0.0
        self.guard = KeyboardGuard()
        screens = monitors() or [(0, 0, root.winfo_screenwidth(), root.winfo_screenheight(), True)]
        if not any(s[4] for s in screens):
            screens[0] = screens[0][:4] + (True,)
        scene_cls = scene or self.rnd.choice(SCENES)
        palette = self.rnd.choice(PALETTES)
        for x, y, w, h, primary in screens:
            win = tk.Toplevel(root)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            win.configure(background=ui.BG, cursor="none" if not primary else "arrow")
            win.geometry(f"{w}x{h}+{x}+{y}")
            canvas = tk.Canvas(win, width=w, height=h, bg=ui.BG, highlightthickness=0)
            canvas.pack(fill="both", expand=True)
            self.windows.append(win)
            self.canvases.append(canvas)
            self.scenes.append(scene_cls(canvas, w, h, self.rnd, palette))
            if primary:
                self._overlay(win, canvas, w, h)
        self.guard.install()
        self.root.after(50, self._focus)
        self.root.after(FPS_MS, self._tick)
        self.root.after(500, self._keep_on_top)
        log.info("lock screen up: %s on %d display(s)", scene_cls.name, len(self.windows))

    # ---- the primary window's controls
    def _overlay(self, win, canvas, w, h):
        import tkinter as tk

        self.canvas = canvas
        big = max(28, h // 14)
        self.clock = canvas.create_text(w - 24, 18, text="", fill="#f4fbff", anchor="ne",
                                        font=("Segoe UI Black", big))
        self.date = canvas.create_text(w - 24, 18 + big * 1.5, text="", fill=ui.ORANGE, anchor="ne",
                                       font=(ui.MONO, max(11, h // 60), "bold"))
        host = socket.gethostname().upper()
        canvas.create_text(24, 18, text=f"NODE  {host}", fill=ui.DIM, anchor="nw", font=(ui.MONO, max(11, h // 60), "bold"))
        self.reason = canvas.create_text(24, 18 + max(11, h // 60) * 1.8, text="", fill=ui.MUTED, anchor="nw",
                                         font=(ui.MONO, max(10, h // 70)))
        self.ticker = canvas.create_text(w // 2, h - 28, text="", fill=ui.MUTED, anchor="s",
                                         font=(ui.MONO, max(11, h // 60), "bold"))
        self.ticker_index = self.rnd.randrange(len(TAGLINES))
        # PIN field
        panel = tk.Frame(win, bg=ui.PANEL, highlightthickness=1, highlightbackground=ui.LINE)
        panel.place(relx=0.5, rely=0.8, anchor="center")
        tk.Label(panel, text="ENTER PIN", fg=ui.CYAN, bg=ui.PANEL, font=(ui.MONO, max(10, h // 70), "bold")).pack(
            padx=24, pady=(10, 2))
        self.dots = tk.Label(panel, text="", fg="#f4fbff", bg=ui.PANEL, font=("Segoe UI Black", max(18, h // 30)),
                             width=12)
        self.dots.pack(padx=24, pady=(0, 2))
        self.message = tk.Label(panel, text="", fg=ui.RED, bg=ui.PANEL, font=(ui.MONO, max(9, h // 80), "bold"))
        self.message.pack(padx=24, pady=(0, 6))
        buttons = tk.Frame(panel, bg=ui.PANEL)
        buttons.pack(padx=24, pady=(0, 12))
        self.power = ui.NeonButton(buttons, "POWER OFF", self._power, style="danger", font_size=max(9, h // 80))
        self.power.pack(side="left")
        hint = "the Flipper unlocks too: OK on its SYS tab" if self.blackout.pin_set() else \
            "no PIN set: Enter unlocks  ·  set one in the tray panel"
        tk.Label(panel, text=hint, fg=ui.MUTED, bg=ui.PANEL, font=(ui.MONO, max(8, h // 90))).pack(padx=24, pady=(0, 10))
        self._refresh_dots()
        for w_ in (win, canvas, panel):
            w_.bind("<Key>", self._key)
        win.bind("<Button-1>", lambda _e: self._focus())
        self.primary = win

    def _focus(self):
        if not self.alive:
            return
        try:
            self.primary.focus_force()
            self.primary.focus_set()
        except Exception:
            pass

    def _keep_on_top(self):
        if not self.alive:
            return
        for win in self.windows:
            try:
                win.lift()
                win.attributes("-topmost", True)
            except Exception:
                pass
        try:
            if self.primary.focus_displayof() is None:
                self._focus()
        except Exception:
            pass
        self.root.after(500, self._keep_on_top)

    def _tick(self):
        if not self.alive:
            return
        self.frame += 1
        for scene in self.scenes:
            try:
                scene.tick(self.frame)
            except Exception:
                log.debug("scene tick failed", exc_info=True)
        if self.frame % 7 == 0:
            now = datetime.now()
            self.canvas.itemconfigure(self.clock, text=now.strftime("%H:%M"))
            self.canvas.itemconfigure(self.date, text=now.strftime("%a %d %b %Y").upper())
            since = int(time.time() - self.blackout.locked_at)
            why = {"flipper": "BLACKOUT FROM THE FLIPPER", "link lost": "FLIPPER LINK LOST  ·  AUTO BLACKOUT",
                   "tray": "BLACKOUT FROM THE TRAY"}.get(self.blackout.reason, "BLACKOUT")
            self.canvas.itemconfigure(self.reason, text=f"{why}  ·  {since // 60:02d}:{since % 60:02d}")
        if self.frame % 60 == 0:
            self.ticker_index = (self.ticker_index + 1) % len(TAGLINES)
            self.canvas.itemconfigure(self.ticker, text="> " + TAGLINES[self.ticker_index] + "_")
        if self.power_armed_until and time.time() > self.power_armed_until:
            self.power_armed_until = 0.0
            self.power.configure(text="POWER OFF")
        # after a few idle minutes the animation runs slower, so an unattended blackout does not
        # keep a core busy or drain the battery (it never prevents sleep in the first place)
        delay = SLOW_FPS_MS if time.time() - self.blackout.locked_at > SLOW_AFTER_S else FPS_MS
        self.root.after(delay, self._tick)

    # ---- PIN
    def _refresh_dots(self):
        self.dots.configure(text="● " * len(self.pin) if self.pin else "_ _ _ _")

    def _key(self, event):
        if not self.alive:
            return "break"
        if time.time() < self.wait_until:
            return "break"
        if event.keysym in ("Return", "KP_Enter"):
            ok, wait = self.blackout.try_pin(self.pin)
            self.pin = ""
            self._refresh_dots()
            if not ok:
                self.message.configure(text="ACCESS DENIED")
                self.wait_until = time.time() + wait
                self._shake()
                self.root.after(1500, lambda: self.message.configure(text=""))
            return "break"
        if event.keysym == "BackSpace":
            self.pin = self.pin[:-1]
        elif event.keysym == "Escape":
            self.pin = ""
        elif event.char and event.char.isdigit() and len(self.pin) < bo.PIN_MAX:
            self.pin += event.char
        self._refresh_dots()
        return "break"

    def _shake(self):
        try:
            for scene in self.scenes:
                scene.slices(12)
        except Exception:
            pass

    def _power(self):
        now = time.time()
        if now < self.power_armed_until:
            log.warning("POWER OFF confirmed on the lock screen")
            self.power.configure(text="SHUTTING DOWN...")
            try:
                bo.power_off()
            except Exception:
                log.exception("shutdown failed")
            return
        self.power_armed_until = now + 4.0
        self.power.configure(text="CLICK AGAIN TO POWER OFF")

    def close(self):
        self.alive = False
        self.guard.remove()
        for win in self.windows:
            try:
                win.destroy()
            except Exception:
                pass
        self.windows = []
        self.canvases = []
        self.scenes = []


# --------------------------------------------------------------------------- PIN dialog
class PinDialog:
    """Set or change the Blackout PIN (the tray panel opens it).  ``on_done(ok)``."""

    def __init__(self, root, blackout, on_done=None, title="BLACKOUT PIN"):
        import tkinter as tk

        self.blackout = blackout
        self.on_done = on_done
        self.win = tk.Toplevel(root)
        win = self.win
        win.title("DedSec Uplink — Blackout PIN")
        win.configure(background=ui.BG)
        win.attributes("-topmost", True)
        win.resizable(False, False)
        ui.glitch_header(win, "DEDSEC", title, height=56).pack(fill="x", padx=10, pady=(8, 0))
        body = tk.Frame(win, bg=ui.BG)
        body.pack(fill="x", padx=18, pady=(6, 4))
        self.fields = {}
        rows = [("CURRENT PIN", "current")] if blackout.pin_set() else []
        rows += [("NEW PIN", "new"), ("REPEAT", "repeat")]
        for label, key in rows:
            tk.Label(body, text=label, fg=ui.DIM, bg=ui.BG, font=(ui.MONO, 9, "bold"), anchor="w").pack(fill="x")
            entry = ui.entry(body, width=24)
            entry.configure(show="•")
            entry.pack(fill="x", pady=(0, 6), ipady=3)
            self.fields[key] = entry
        tk.Label(body, text=f"{bo.PIN_MIN}-{bo.PIN_MAX} digits; the lock screen takes it from the keyboard",
                 fg=ui.MUTED, bg=ui.BG, font=(ui.MONO, 8), anchor="w", justify="left").pack(fill="x")
        self.error = tk.Label(body, text="", fg=ui.RED, bg=ui.BG, font=(ui.MONO, 9, "bold"), anchor="w")
        self.error.pack(fill="x", pady=(4, 0))
        buttons = tk.Frame(win, bg=ui.BG)
        buttons.pack(fill="x", padx=18, pady=(4, 12))
        ui.NeonButton(buttons, "SAVE", self._save, style="primary").pack(side="left")
        ui.NeonButton(buttons, "CANCEL", self._cancel).pack(side="left", padx=(8, 0))
        win.bind("<Return>", lambda _e: self._save())
        win.bind("<Escape>", lambda _e: self._cancel())
        win.protocol("WM_DELETE_WINDOW", self._cancel)
        win.update_idletasks()
        x = (win.winfo_screenwidth() - win.winfo_reqwidth()) // 2
        y = (win.winfo_screenheight() - win.winfo_reqheight()) // 2
        win.geometry(f"+{x}+{y}")
        first = self.fields.get("current") or self.fields["new"]
        win.after(50, lambda: (win.focus_force(), first.focus_set()))

    def _save(self):
        new = self.fields["new"].get().strip()
        repeat = self.fields["repeat"].get().strip()
        if "current" in self.fields and not self.blackout.check_pin(self.fields["current"].get().strip()):
            self.error.configure(text="the current PIN is wrong")
            return
        problem = bo.pin_problem(new)
        if problem:
            self.error.configure(text=problem)
            return
        if new != repeat:
            self.error.configure(text="the two PINs differ")
            return
        self.blackout.set_pin(new)
        self._finish(True)

    def _cancel(self):
        self._finish(False)

    def _finish(self, ok):
        try:
            self.win.destroy()
        except Exception:
            pass
        if self.on_done:
            try:
                self.on_done(ok)
            except Exception:
                log.exception("PIN dialog callback failed")
