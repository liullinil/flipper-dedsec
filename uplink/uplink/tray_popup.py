"""DedSec-style tray menu: a borderless Tk panel instead of the plain Windows popup menu.

pystray's native menu cannot be styled, so a click on the tray icon opens this panel on the shared
UI thread (:mod:`uplink.ui`) next to the cursor: a glitching DEDSEC header, link and session
status, update progress, and the actions. The native menu stays as a fallback.
"""
import ctypes
import logging
import random
import threading
from ctypes import wintypes

from .icon import make_icon

log = logging.getLogger("uplink.tray")

BG = "#05080c"
PANEL = "#071018"
BORDER = "#27e0e8"
LINE = "#1d4050"
TEXT = "#d7edf2"
MUTED = "#4d6b75"
CYAN = "#27e0e8"
MAGENTA = "#ff2bd6"
ORANGE = "#f28a32"
RED = "#ff4f6d"
HOVER = "#0f2a33"
STATUS_COLORS = {"connected": "#38e070", "searching": "#f0b400", "paused": "#808080"}
WIDTH = 330


def _work_area(x, y):
    """Work area (without the taskbar) of the monitor under (x, y)."""
    try:
        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

        user32 = ctypes.windll.user32
        user32.MonitorFromPoint.restype = wintypes.HANDLE
        monitor = user32.MonitorFromPoint(wintypes.POINT(x, y), 2)  # nearest
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            r = info.rcWork
            return r.left, r.top, r.right, r.bottom
    except Exception:
        log.debug("work area lookup failed", exc_info=True)
    return None


class TrayPopup:
    """``snapshot()`` returns the state to show, ``items()`` the actions:
    a list of dicts {label, action, enabled, style} (style: normal / accent / danger),
    ``None`` for a separator. Both are called on the UI thread when the panel opens."""

    def __init__(self, ui, snapshot, items, on_join=None):
        self.ui = ui
        self.snapshot = snapshot
        self.items = items
        self.on_join = on_join          # "> JOIN US_" (opens the project page)
        self._win = None
        self._images = []
        self._focused = False
        self._outside = 0
        ui.on_close(self._close)

    # ------------------------------------------------------------------ any thread
    def toggle_at(self, x, y):
        self.ui.call(self._toggle, x, y)

    def close(self):
        self.ui.call(self._close)

    # ------------------------------------------------------------------ UI thread
    def _toggle(self, x, y):
        if self._win is not None:
            self._close()
            return
        self._build(x, y)

    def _close(self):
        win, self._win = self._win, None
        self._images = []
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass

    def _run(self, action):
        self._close()
        # actions may stop the UI thread (Quit) or block: never run them on it
        threading.Thread(target=action, name="tray-action", daemon=True).start()

    def _build(self, x, y):
        import tkinter as tk

        state = self.snapshot()
        win = tk.Toplevel(self.ui.root)
        self._win = win
        self._focused = False
        self._outside = 0
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(background=BORDER)
        body = tk.Frame(win, background=PANEL)
        body.pack(fill="both", expand=True, padx=1, pady=1)

        # header: hood + glitching DEDSEC wordmark
        header = tk.Canvas(body, width=WIDTH, height=62, background=BG, highlightthickness=0)
        header.pack(fill="x")
        try:
            from PIL import ImageTk
            ring = STATUS_COLORS.get(state.get("status"), "#e03030")
            hood = ImageTk.PhotoImage(make_icon(ring).resize((44, 44)), master=win)
            self._images.append(hood)
            header.create_image(10, 9, image=hood, anchor="nw")
        except Exception:
            log.debug("tray header image failed", exc_info=True)
        font = ("Segoe UI Black", 20)
        for dx, color in ((2, MAGENTA), (-2, CYAN), (0, "#f4fbff")):
            header.create_text(64 + dx, 24, text="DEDSEC", anchor="w", fill=color, font=font,
                               tags="word")
        header.create_text(66, 47, text="UPLINK  //  CONTROL NODE", anchor="w", fill=ORANGE,
                           font=("Consolas", 8, "bold"))
        self._glitch(header)

        status = tk.Frame(body, background=PANEL)
        status.pack(fill="x", padx=12, pady=(8, 4))
        dot = STATUS_COLORS.get(state.get("status"), RED)
        line = tk.Frame(status, background=PANEL)
        line.pack(fill="x")
        tk.Label(line, text="●", fg=dot, bg=PANEL, font=("Consolas", 10)).pack(side="left")
        name = state.get("name") or ""
        tk.Label(line, text=f" {str(state.get('status', '')).upper()}  {name}", fg=TEXT, bg=PANEL,
                 font=("Consolas", 9, "bold")).pack(side="left")
        for text, color in state.get("lines", []):
            tk.Label(status, text=text, fg=color or MUTED, bg=PANEL, font=("Consolas", 9),
                     anchor="w", justify="left").pack(fill="x")
        ota = state.get("ota")
        if ota:
            tag, pct = ota
            bar = "■" * (pct // 10) + "□" * (10 - pct // 10)
            tk.Label(status, text=f"FLIPPER UPDATE {tag}  {bar} {pct}%", fg=ORANGE, bg=PANEL,
                     font=("Consolas", 9, "bold"), anchor="w").pack(fill="x", pady=(2, 0))

        tk.Frame(body, background=LINE, height=1).pack(fill="x", padx=10, pady=(6, 4))
        for item in self.items():
            if item is None:
                tk.Frame(body, background=LINE, height=1).pack(fill="x", padx=10, pady=4)
                continue
            self._item(body, item)
        tk.Frame(body, background=LINE, height=1).pack(fill="x", padx=10, pady=(4, 0))
        footer = tk.Label(body, text="> JOIN US_", fg=MUTED, bg=PANEL, font=("Consolas", 9, "bold"),
                          anchor="w", cursor="hand2" if self.on_join else "arrow")
        footer.pack(fill="x", padx=12, pady=(3, 6))
        if self.on_join:
            footer.bind("<Enter>", lambda _e: footer.configure(fg=MAGENTA))
            footer.bind("<Leave>", lambda _e: footer.configure(fg=MUTED))
            footer.bind("<ButtonRelease-1>", lambda _e: self._run(self.on_join))
        self._blink(footer, True)

        win.update_idletasks()
        w, h = max(WIDTH + 2, win.winfo_reqwidth()), win.winfo_reqheight()
        area = _work_area(x, y) or (0, 0, win.winfo_screenwidth(), win.winfo_screenheight())
        left, top, right, bottom = area
        px = min(max(x - w + 12, left + 4), right - w - 4)
        py = y - h - 10 if y - h - 10 >= top + 4 else min(y + 10, bottom - h - 4)
        win.geometry(f"{w}x{h}+{px}+{py}")
        win.bind("<Escape>", lambda _e: self._close())
        win.bind("<FocusIn>", lambda _e: setattr(self, "_focused", True))
        win.bind("<FocusOut>", lambda _e: win.after(60, self._focus_check))
        win.after(30, win.focus_force)
        win.after(400, self._pointer_check)

    def _item(self, parent, item):
        import tkinter as tk

        enabled = item.get("enabled", True)
        style = item.get("style", "normal")
        color = {"accent": CYAN, "danger": RED}.get(style, TEXT) if enabled else MUTED
        row = tk.Label(parent, text=f"  ▸  {item['label']}", fg=color, bg=PANEL, anchor="w",
                       font=("Consolas", 10, "bold"), padx=6, pady=4,
                       cursor="hand2" if enabled else "arrow")
        row.pack(fill="x", padx=4)
        if not enabled:
            return

        def enter(_e):
            row.configure(bg=HOVER, fg=MAGENTA if style == "danger" else CYAN)

        def leave(_e):
            row.configure(bg=PANEL, fg=color)

        row.bind("<Enter>", enter)
        row.bind("<Leave>", leave)
        row.bind("<ButtonRelease-1>", lambda _e: self._run(item["action"]))

    def _glitch(self, canvas):
        """Short neon slices across the header, reshuffled while the panel is open."""
        if self._win is None:
            return
        try:
            canvas.delete("glitch")
            for _ in range(random.randint(1, 3)):
                y = random.randint(6, 56)
                x = random.randint(60, WIDTH - 40)
                color = random.choice((MAGENTA, CYAN, ORANGE))
                canvas.create_rectangle(x, y, x + random.randint(8, 46), y + random.choice((1, 2)),
                                        fill=color, outline="", tags="glitch")
            shift = random.choice((0, 0, 0, 1, -1))
            canvas.move("word", shift, 0)
            canvas.after(90, lambda: canvas.move("word", -shift, 0))
            canvas.after(random.randint(120, 260), lambda: self._glitch(canvas))
        except Exception:
            pass

    def _blink(self, label, on):
        if self._win is None:
            return
        try:
            label.configure(text="> JOIN US_" if on else "> JOIN US ")
            label.after(530, lambda: self._blink(label, not on))
        except Exception:
            pass

    def _focus_check(self):
        win = self._win
        if win is None:
            return
        try:
            focus = win.focus_get()
        except Exception:
            focus = None
        if focus is None or not str(focus).startswith(str(win)):
            self._close()

    def _pointer_check(self):
        """Fallback when Windows refuses focus: close once the pointer has left for a while."""
        win = self._win
        if win is None:
            return
        try:
            px, py = win.winfo_pointerxy()
            inside = (win.winfo_rootx() <= px < win.winfo_rootx() + win.winfo_width() and
                      win.winfo_rooty() <= py < win.winfo_rooty() + win.winfo_height())
            self._outside = 0 if inside else self._outside + 1
            if not self._focused and self._outside > 8:   # ~2.4 s away and never focused
                self._close()
                return
            win.after(300, self._pointer_check)
        except Exception:
            pass


def install_click_handler(icon, on_click):
    """Route left and right clicks on a pystray icon to on_click(x, y) (Windows only).

    Returns False (native menu stays) when pystray's internals differ from what we expect."""
    try:
        from pystray import _win32
        win32 = _win32.win32
        original = icon._on_notify
    except Exception:
        return False

    def on_notify(wparam, lparam):
        if lparam in (win32.WM_LBUTTONUP, win32.WM_RBUTTONUP):
            try:
                point = wintypes.POINT()
                win32.GetCursorPos(ctypes.byref(point))
                win32.SetForegroundWindow(icon._hwnd)   # lets the panel take focus
                on_click(point.x, point.y)
                return
            except Exception:
                log.exception("tray panel failed, using the plain menu")
        return original(wparam, lparam)

    handlers = getattr(icon, "_message_handlers", None)
    if not isinstance(handlers, dict) or win32.WM_NOTIFY not in handlers:
        return False
    handlers[win32.WM_NOTIFY] = on_notify   # pystray's window procedure dispatches from here
    return True
