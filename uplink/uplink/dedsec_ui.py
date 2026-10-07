"""DedSec look for the companion's Tk windows: palette, glitch header, neon buttons, sections.

Only plain tk widgets (plus ttk scrollbars styled for the clam theme the UI thread selects), so the
colours are the same everywhere. Every function must run on the companion's UI thread.
"""
import random

BG = "#05080c"          # window
PANEL = "#071018"       # boxes, lists, text
FIELD = "#0b1820"       # entry fields
LINE = "#16343f"        # borders, separators
TEXT = "#d7edf2"
MUTED = "#55707a"
DIM = "#7595a0"
CYAN = "#27e0e8"
MAGENTA = "#ff2bd6"
ORANGE = "#f28a32"
YELLOW = "#ffe14d"
GREEN = "#38e070"
RED = "#ff4f6d"
HOVER = "#0f2a33"
MONO = "Consolas"


def glitch_header(parent, title, subtitle, height=64, image=None):
    """A canvas with an RGB-split wordmark and neon slices that reshuffle while it is shown."""
    import tkinter as tk

    canvas = tk.Canvas(parent, height=height, background=BG, highlightthickness=0)
    x = 12
    if image is not None:
        canvas.create_image(x, height // 2, image=image, anchor="w")
        x += 56
    font = ("Segoe UI Black", 22)
    for dx, color in ((2, MAGENTA), (-2, CYAN), (0, "#f4fbff")):
        canvas.create_text(x + dx, height // 2 - 8, text=title, anchor="w", fill=color, font=font,
                           tags="word")
    canvas.create_text(x + 2, height // 2 + 18, text=subtitle, anchor="w", fill=ORANGE,
                       font=(MONO, 9, "bold"))

    def glitch():
        try:
            if not canvas.winfo_exists():
                return
            canvas.delete("glitch")
            width = max(200, canvas.winfo_width())
            for _ in range(random.randint(1, 4)):
                y = random.randint(4, height - 4)
                gx = random.randint(x, max(x + 10, width - 60))
                canvas.create_rectangle(gx, y, gx + random.randint(8, 70), y + random.choice((1, 2)),
                                        fill=random.choice((MAGENTA, CYAN, ORANGE)), outline="",
                                        tags="glitch")
            shift = random.choice((0, 0, 0, 1, -1, 2))
            canvas.move("word", shift, 0)
            canvas.after(80, lambda: canvas.move("word", -shift, 0))
            canvas.after(random.randint(150, 420), glitch)
        except Exception:
            pass

    canvas.after(200, glitch)
    return canvas


class NeonButton:
    """A flat button with a 1 px neon frame (a Label: ttk buttons ignore colours on Windows)."""

    def __init__(self, parent, text, command, style="normal", font_size=9):
        import tkinter as tk

        self._command = command
        self._style = style
        self._enabled = True
        self._color = {"primary": MAGENTA, "accent": CYAN, "danger": RED}.get(style, TEXT)
        self._border = self._color if style != "normal" else LINE
        # a 1 px frame around the label is the border (highlight rings only show with focus)
        self.frame = tk.Frame(parent, bg=self._border, padx=1, pady=1)
        self.widget = tk.Label(self.frame, text=text, fg=self._color, bg=BG,
                               font=(MONO, font_size, "bold"), padx=10, pady=3, cursor="hand2")
        self.widget.pack()
        for target in (self.frame, self.widget):
            target.bind("<Enter>", self._enter)
            target.bind("<Leave>", self._leave)
            target.bind("<ButtonRelease-1>", self._click)

    def _enter(self, _event):
        if self._enabled:
            hot = MAGENTA if self._style in ("primary", "danger") else CYAN
            self.widget.configure(bg=HOVER, fg=hot)
            self.frame.configure(bg=hot)

    def _leave(self, _event):
        if self._enabled:
            self.widget.configure(bg=BG, fg=self._color)
            self.frame.configure(bg=self._border)

    def _click(self, _event):
        if self._enabled and self._command:
            self._command()

    # the subset of the widget API the windows use
    def pack(self, **kwargs):
        self.frame.pack(**kwargs)
        return self

    def grid(self, **kwargs):
        self.frame.grid(**kwargs)
        return self

    def configure(self, **kwargs):
        self.widget.configure(**kwargs)

    config = configure

    def cget(self, key):
        return self.widget.cget(key)

    def state(self, flags):
        """ttk-style: ['disabled'] or ['!disabled']."""
        self._enabled = "disabled" not in flags
        self.widget.configure(fg=self._color if self._enabled else MUTED,
                              cursor="hand2" if self._enabled else "arrow", bg=BG)
        self.frame.configure(bg=self._border if self._enabled else LINE)


def section(parent, title, color=CYAN):
    """A '> TITLE' header line with a thin rule under it; returns the frame."""
    import tkinter as tk

    frame = tk.Frame(parent, bg=BG)
    tk.Label(frame, text="> " + title, fg=color, bg=BG, font=(MONO, 9, "bold"), anchor="w").pack(
        side="left")
    tk.Frame(frame, bg=LINE, height=1).pack(side="left", fill="x", expand=True, padx=(8, 0), pady=(2, 0))
    return frame


def entry(parent, variable=None, width=None):
    import tkinter as tk

    kwargs = dict(bg=FIELD, fg=TEXT, insertbackground=CYAN, relief="flat", font=(MONO, 9),
                  highlightthickness=1, highlightbackground=LINE, highlightcolor=CYAN,
                  selectbackground=MAGENTA, selectforeground=BG)
    if variable is not None:
        kwargs["textvariable"] = variable
    if width:
        kwargs["width"] = width
    return tk.Entry(parent, **kwargs)


def text_box(parent, height=None):
    """A read-only text area with a dark scrollbar; returns (frame, text)."""
    import tkinter as tk
    from tkinter import ttk

    frame = tk.Frame(parent, bg=PANEL, highlightthickness=1, highlightbackground=LINE)
    text = tk.Text(frame, bg=PANEL, fg=TEXT, insertbackground=CYAN, relief="flat", wrap="word",
                   font=(MONO, 9), padx=8, pady=6, selectbackground=MAGENTA, selectforeground=BG,
                   borderwidth=0, **({"height": height} if height else {}))
    scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview, style="DedSec.Vertical.TScrollbar")
    text.configure(yscrollcommand=scroll.set, state="disabled")
    text.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")
    text.tag_configure("title", foreground=CYAN, font=(MONO, 10, "bold"))
    text.tag_configure("head", foreground=MAGENTA, font=(MONO, 9, "bold"))
    text.tag_configure("muted", foreground=DIM)
    text.tag_configure("good", foreground=GREEN, font=(MONO, 9, "bold"))
    text.tag_configure("warn", foreground=YELLOW, font=(MONO, 9, "bold"))
    return frame, text


def style_scrollbars(widget):
    from tkinter import ttk

    style = ttk.Style(widget)
    for orient in ("Vertical", "Horizontal"):
        style.configure(f"DedSec.{orient}.TScrollbar", troughcolor=PANEL, background=LINE,
                        bordercolor=PANEL, lightcolor=LINE, darkcolor=LINE, arrowcolor=CYAN,
                        gripcount=0, relief="flat")
        style.map(f"DedSec.{orient}.TScrollbar", background=[("active", HOVER)])


def style_treeview(widget):
    """Dark ``DedSec.Treeview`` rows with a magenta selection (tag colours stay visible)."""
    from tkinter import ttk

    style = ttk.Style(widget)
    style.configure("DedSec.Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                    bordercolor=PANEL, lightcolor=PANEL, darkcolor=PANEL, borderwidth=0, rowheight=22,
                    font=(MONO, 9))
    style.map("DedSec.Treeview", background=[("selected", MAGENTA)], foreground=[("selected", BG)])
    style.configure("DedSec.Treeview.Heading", background=BG, foreground=CYAN, relief="flat",
                    bordercolor=LINE, lightcolor=BG, darkcolor=BG, font=(MONO, 8, "bold"), padding=(6, 4))
    style.map("DedSec.Treeview.Heading", background=[("active", HOVER)], foreground=[("active", MAGENTA)])
    style.layout("DedSec.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])


class Chip:
    """A small toggle: lit magenta while on. ``command`` runs on a click."""

    def __init__(self, parent, text, command):
        import tkinter as tk

        self.on = False
        self.widget = tk.Label(parent, text=text, font=(MONO, 8, "bold"), padx=8, pady=2, cursor="hand2",
                               bg=BG, fg=DIM, highlightthickness=1, highlightbackground=LINE)
        self.widget.bind("<ButtonRelease-1>", lambda _event: command())

    def set(self, on):
        self.on = bool(on)
        self.widget.configure(bg=MAGENTA if self.on else BG, fg=BG if self.on else DIM,
                              highlightbackground=MAGENTA if self.on else LINE)

    def pack(self, **kwargs):
        self.widget.pack(**kwargs)
        return self
