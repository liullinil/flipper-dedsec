"""Desktop settings window for the DedSec Uplink companion.

The tray icon is small and transient; this window holds the longer controls (terminal access,
Windows integration, updates, RF Hunter import). It is a Toplevel on the shared UI thread
(:mod:`uplink.ui`): every Tk call happens there, other threads only call :meth:`show`/:meth:`close`.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from typing import Callable, Mapping, Optional

from . import config, hooks
from .updater import COMPANION_VERSION

log = logging.getLogger("uplink.settings")

BG = "#071018"
CARD = "#0d1c26"
CARD_ALT = "#102733"
BORDER = "#1d4050"
TEXT = "#d7edf2"
MUTED = "#7595a0"
CYAN = "#27e0e8"
ORANGE = "#f28a32"


def apply_style(style):
    """The DedSec look for ttk widgets (shared by every companion window)."""
    try:
        style.theme_use("clam")
    except Exception:
        pass
    style.configure("Cyber.TFrame", background=BG)
    style.configure("Card.TFrame", background=CARD)
    style.configure("AltCard.TFrame", background=CARD_ALT)
    style.configure("Cyber.TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 9))
    style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 9))
    style.configure("Card.TLabel", background=CARD, foreground=TEXT, font=("Segoe UI", 9))
    style.configure("CardMuted.TLabel", background=CARD, foreground=MUTED, font=("Segoe UI", 8))
    style.configure("Value.TLabel", background=CARD, foreground=CYAN, font=("Segoe UI", 12, "bold"))
    style.configure("VersionTitle.TLabel", background=CARD, foreground=MUTED,
                    font=("Segoe UI", 8, "bold"))
    style.configure("Cyber.TLabelframe", background=CARD, foreground=TEXT, bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER, relief="solid", borderwidth=1)
    style.configure("Cyber.TLabelframe.Label", background=CARD, foreground=CYAN,
                    font=("Segoe UI", 9, "bold"))
    style.configure("Cyber.TCheckbutton", background=CARD, foreground=TEXT, font=("Segoe UI", 9),
                    focuscolor=CARD)
    style.map("Cyber.TCheckbutton", background=[("active", CARD)],
              foreground=[("disabled", MUTED), ("active", TEXT)])
    style.configure("Cyber.TCombobox", fieldbackground=CARD_ALT, background=CARD_ALT,
                    foreground=TEXT, arrowcolor=CYAN, bordercolor=BORDER, lightcolor=BORDER,
                    darkcolor=BORDER)
    style.map("Cyber.TCombobox", fieldbackground=[("readonly", CARD_ALT)],
              foreground=[("readonly", TEXT)])
    style.configure("Cyber.TButton", background=CARD_ALT, foreground=TEXT, bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER, padding=(12, 7),
                    font=("Segoe UI", 9, "bold"))
    style.map("Cyber.TButton", background=[("active", BORDER), ("disabled", CARD)],
              foreground=[("disabled", MUTED), ("active", CYAN)])
    style.configure("Action.TButton", background=ORANGE, foreground=BG, bordercolor=ORANGE,
                    lightcolor=ORANGE, darkcolor=ORANGE, padding=(15, 8),
                    font=("Segoe UI", 9, "bold"))
    style.map("Action.TButton", background=[("active", "#ffad57"), ("disabled", CARD)],
              foreground=[("disabled", MUTED), ("active", BG)])
    style.configure("Close.TButton", background=BG, foreground=MUTED, bordercolor=BORDER,
                    padding=(12, 6), font=("Segoe UI", 9))
    style.map("Close.TButton", background=[("active", CARD_ALT)], foreground=[("active", TEXT)])
    style.configure("Badge.TLabel", background="#123b45", foreground=CYAN, padding=(10, 4),
                    font=("Segoe UI", 9, "bold"))


class SettingsWindow:
    """A single, non-modal settings window on the shared UI thread.

    ``actions`` holds optional no-argument callbacks from the tray runner (check/install updates,
    open the log, open the RF analyzer, current status text, refresh the tray menu).
    """

    def __init__(self, ui, feed, cfg: dict, actions: Optional[Mapping[str, Callable]] = None):
        self.ui = ui
        self.feed = feed
        self.cfg = cfg
        self.actions = dict(actions or {})
        self._win = None
        self._vars = {}
        self._widgets = {}
        self._note = ""
        self._note_until = 0.0
        self._scan_y = 0
        ui.on_close(self._destroy)

    # ------------------------------------------------------------------ public API (any thread)
    def show(self):
        self.ui.call(self._show)

    def close(self):
        self.ui.call(self._destroy)

    # ------------------------------------------------------------------ Tk thread
    def _show(self):
        win = self._win
        if win is not None:
            try:
                if win.winfo_exists():
                    win.deiconify()
                    win.lift()
                    win.focus_force()
                    return
            except Exception:
                pass
            self._destroy()
        self._build()

    def _destroy(self):
        win, self._win = self._win, None
        self._vars = {}
        self._widgets = {}
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass

    def _build(self):
        import tkinter as tk
        from tkinter import ttk

        win = tk.Toplevel(self.ui.root)
        self._win = win
        win.title("DEDSEC // UPLINK")
        win.geometry("700x720")
        win.minsize(620, 620)
        win.configure(background=BG)
        win.protocol("WM_DELETE_WINDOW", win.withdraw)
        apply_style(ttk.Style(win))

        def var(kind, value):
            v = kind(master=win, value=value)
            return v

        outer = ttk.Frame(win, padding=(20, 16, 20, 14), style="Cyber.TFrame")
        outer.grid(row=0, column=0, sticky="nsew")
        win.columnconfigure(0, weight=1)
        win.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)

        header = tk.Canvas(outer, height=78, background=BG, highlightthickness=0, borderwidth=0)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        self._widgets["header_canvas"] = header
        header.bind("<Configure>", lambda _event: self._draw_header(header))
        self._draw_header(header)

        status = ttk.LabelFrame(outer, text="  LINK STATUS  ", padding=(12, 10),
                                style="Cyber.TLabelframe")
        status.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        status.columnconfigure(0, weight=1)
        for key in ("connection", "companion", "flipper", "latest"):
            self._vars[key] = var(tk.StringVar, "")
        self._widgets["connection"] = ttk.Label(status, textvariable=self._vars["connection"],
                                                style="Badge.TLabel", anchor="w")
        self._widgets["connection"].grid(row=0, column=0, sticky="w")
        versions = ttk.Frame(status, style="Card.TFrame")
        versions.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        for col in range(3):
            versions.columnconfigure(col, weight=1)
        for col, (label, key) in enumerate((("COMPANION", "companion"), ("FLIPPER APP", "flipper"),
                                             ("LATEST RELEASE", "latest"))):
            card = ttk.Frame(versions, style="AltCard.TFrame", padding=(10, 7))
            card.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else 4, 4 if col < 2 else 0))
            ttk.Label(card, text=label, style="VersionTitle.TLabel").pack(anchor="w")
            ttk.Label(card, textvariable=self._vars[key], style="Value.TLabel").pack(anchor="w",
                                                                                  pady=(2, 0))

        controls = ttk.Frame(outer, style="Cyber.TFrame")
        controls.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        controls.columnconfigure(0, weight=1)
        controls.columnconfigure(1, weight=1)

        terminal = ttk.LabelFrame(controls, text="  TERMINAL ACCESS  ", padding=(12, 10),
                                  style="Cyber.TLabelframe")
        terminal.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        terminal.columnconfigure(1, weight=1)
        self._vars["cmd_enabled"] = var(tk.BooleanVar, bool(self.cfg.get("cmd_enabled", True)))
        ttk.Checkbutton(terminal, text="Allow commands from Flipper",
                        variable=self._vars["cmd_enabled"], command=self._apply_shell,
                        style="Cyber.TCheckbutton").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(terminal, text="Active shell", style="CardMuted.TLabel").grid(
            row=1, column=0, sticky="w", pady=(10, 0))
        self._vars["shell"] = var(tk.StringVar, self.cfg.get("shell", "cmd"))
        shell = ttk.Combobox(terminal, textvariable=self._vars["shell"], values=("cmd", "powershell"),
                             state="readonly", width=18, style="Cyber.TCombobox")
        shell.grid(row=1, column=1, sticky="ew", pady=(10, 0))
        shell.bind("<<ComboboxSelected>>", lambda _event: self._apply_shell())

        integration = ttk.LabelFrame(controls, text="  WINDOWS INTEGRATION  ", padding=(12, 10),
                                     style="Cyber.TLabelframe")
        integration.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        self._vars["autostart"] = var(tk.BooleanVar, config.autostart_enabled())
        self._vars["hooks"] = var(tk.BooleanVar, hooks.installed())
        ttk.Checkbutton(integration, text="Start with Windows", variable=self._vars["autostart"],
                        command=self._apply_autostart, style="Cyber.TCheckbutton").grid(
                            row=0, column=0, sticky="w")
        ttk.Checkbutton(integration, text="Install Claude Code hooks", variable=self._vars["hooks"],
                        command=self._apply_hooks, style="Cyber.TCheckbutton").grid(
                            row=1, column=0, sticky="w", pady=(10, 0))

        rf = ttk.LabelFrame(outer, text="  RF HUNTER  ", padding=(12, 10), style="Cyber.TLabelframe")
        rf.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        rf.columnconfigure(0, weight=1)
        self._vars["rf_status"] = var(tk.StringVar, "")
        ttk.Label(rf, textvariable=self._vars["rf_status"], style="Card.TLabel").grid(
            row=0, column=0, sticky="w")
        rf_buttons = ttk.Frame(rf, style="Card.TFrame")
        rf_buttons.grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Button(rf_buttons, text="OPEN RF ANALYZER", command=self._open_analyzer,
                   style="Action.TButton").grid(row=0, column=0, padx=(0, 7))
        self._widgets["rf_sync"] = ttk.Button(rf_buttons, text="Import from Flipper now",
                                              command=self._rf_sync_now, style="Cyber.TButton")
        self._widgets["rf_sync"].grid(row=0, column=1)

        updates = ttk.LabelFrame(outer, text="  UPDATE CHANNEL  ", padding=(12, 10),
                                 style="Cyber.TLabelframe")
        updates.grid(row=4, column=0, sticky="ew", pady=(0, 10))
        updates.columnconfigure(0, weight=1)
        self._vars["update_status"] = var(tk.StringVar, "Checking for updates…")
        ttk.Label(updates, textvariable=self._vars["update_status"], style="Card.TLabel").grid(
            row=0, column=0, sticky="w")
        buttons = ttk.Frame(updates, style="Card.TFrame")
        buttons.grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Button(buttons, text="CHECK NOW", command=self._check_updates,
                   style="Action.TButton").grid(row=0, column=0, padx=(0, 7))
        self._widgets["companion_update"] = ttk.Button(
            buttons, text="Install companion update", command=self._install_companion,
            style="Cyber.TButton")
        self._widgets["companion_update"].grid(row=0, column=1, padx=(0, 7))
        self._widgets["flipper_update"] = ttk.Button(
            buttons, text="Install Flipper app update", command=self._install_flipper,
            style="Cyber.TButton")
        self._widgets["flipper_update"].grid(row=0, column=2)

        footer = ttk.Frame(outer, style="Cyber.TFrame")
        footer.grid(row=5, column=0, sticky="ew", pady=(4, 0))
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, text="DEDSEC // LOCAL CONTROL NODE", style="Muted.TLabel").grid(
            row=0, column=0, sticky="w")
        ttk.Button(footer, text="OPEN LOG", command=self._open_log, style="Close.TButton").grid(
            row=0, column=1, padx=(8, 5))
        ttk.Button(footer, text="CLOSE", command=win.withdraw, style="Close.TButton").grid(
            row=0, column=2)

        self._refresh()
        self._animate_header()
        win.after(1000, self._tick)

    def _draw_header(self, canvas):
        """Paint the static wordmark and separator used by the scanline effect."""
        try:
            width = max(1, int(canvas.winfo_width()))
            height = max(1, int(canvas.winfo_height()))
            canvas.delete("all")
            canvas.create_text(16, 25, text="DEDSEC // UPLINK", anchor="w", fill=CYAN,
                               font=("Segoe UI", 20, "bold"), tags="static")
            canvas.create_text(18, 52, text="LOCAL COMPANION  //  CONTROL NODE", anchor="w",
                               fill=ORANGE, font=("Segoe UI", 8, "bold"), tags="static")
            canvas.create_line(width - 170, height - 17, width - 2, height - 17, fill="#173542",
                               width=1, tags="static")
            canvas.create_text(width - 5, height - 10, text="SECURE LINK", anchor="e",
                               fill="#4d7782", font=("Segoe UI", 7), tags="static")
        except Exception:
            log.debug("header redraw failed", exc_info=True)

    def _visible(self):
        try:
            return self._win is not None and self._win.state() != "withdrawn"
        except Exception:
            return False

    def _animate_header(self):
        """Move a low-contrast scanline and an occasional glitch mark (only while visible)."""
        win = self._win
        canvas = self._widgets.get("header_canvas")
        if win is None or canvas is None:
            return
        try:
            if self._visible():
                width = max(1, int(canvas.winfo_width()))
                height = max(1, int(canvas.winfo_height()))
                y = self._scan_y % height
                canvas.delete("scan")
                canvas.create_line(0, y, width, y, fill="#123541", width=1, tags="scan")
                if self._scan_y % 23 == 0:
                    x = width - 130 - ((self._scan_y // 23) % 5) * 8
                    canvas.create_rectangle(x, y, min(width - 5, x + 19), y + 1, fill="#1e5360",
                                            outline="", tags="scan")
                self._scan_y = (self._scan_y + 3) % height
            win.after(90 if self._visible() else 500, self._animate_header)
        except Exception:
            log.debug("header animation stopped", exc_info=True)

    # ------------------------------------------------------------------ actions (Tk thread)
    def _set_note(self, text, seconds=8.0):
        self._note = text
        self._note_until = time.time() + seconds
        if "update_status" in self._vars:
            self._vars["update_status"].set(text)

    def _apply_shell(self):
        self.cfg["cmd_enabled"] = bool(self._vars["cmd_enabled"].get())
        shell = self._vars["shell"].get()
        self.cfg["shell"] = shell if shell in ("cmd", "powershell") else "cmd"
        config.save(self.cfg)
        # the running shell has the old kind: restart it off the UI thread (it may take a moment)
        reset = getattr(self.feed, "reset_shell", None)
        if reset:
            threading.Thread(target=reset, name="shell-reset", daemon=True).start()
        self._notify_changed()

    def _apply_autostart(self):
        try:
            config.set_autostart(bool(self._vars["autostart"].get()))
        except Exception as exc:
            log.warning("cannot change autostart: %s", exc)
            self._vars["autostart"].set(config.autostart_enabled())
        self._notify_changed()

    def _apply_hooks(self):
        try:
            hooks.install(remove=not bool(self._vars["hooks"].get()))
        except Exception as exc:
            log.warning("cannot change Claude hooks: %s", exc)
            self._vars["hooks"].set(hooks.installed())
        self._notify_changed()

    def _check_updates(self):
        self.feed.updater.check_async(force=True)
        self._set_note("Checking for updates…", 3.0)

    def _install_companion(self):
        callback = self.actions.get("install_companion")
        if callback:
            self._set_note("Downloading the companion update… it restarts when ready", 60.0)
            callback()

    def _install_flipper(self):
        callback = self.actions.get("install_flipper")
        if callback:
            self._set_note("Sending the update to the Flipper… watch its screen", 30.0)
            callback()

    def _open_analyzer(self):
        callback = self.actions.get("open_analyzer")
        if callback:
            callback()

    def _rf_sync_now(self):
        sync = getattr(self.feed, "rf_sync", None)
        if sync is not None:
            sync.sync_now()

    def _open_log(self):
        callback = self.actions.get("open_log")
        if callback:
            callback()
            return
        path = os.path.join(config.APP_DIR, "uplink.log")
        try:
            os.startfile(path)
        except Exception as exc:
            log.warning("cannot open log: %s", exc)

    def _notify_changed(self):
        callback = self.actions.get("on_changed")
        if callback:
            try:
                callback()
            except Exception:
                log.debug("settings refresh callback failed", exc_info=True)

    # ------------------------------------------------------------------ refresh (Tk thread)
    def _rf_text(self):
        sync = getattr(self.feed, "rf_sync", None)
        if sync is None:
            return "RF import is not available"
        try:
            st = sync.status()
        except Exception:
            return "RF import status unavailable"
        parts = []
        if st.get("pending") is not None and st.get("state") is not None:
            parts.append(f"On the Flipper: {st.get('pending', 0)} waiting")
        else:
            parts.append("Flipper RF tab not seen yet")
        parts.append(f"imported {st.get('imported', 0)}")
        if st.get("failed"):
            parts.append(f"failed {st['failed']}")
        if st.get("syncing"):
            parts.append("importing…")
        elif st.get("last_sync"):
            parts.append("last import " + time.strftime("%H:%M", time.localtime(st["last_sync"])))
        text = "  ·  ".join(parts)
        if st.get("last_error"):
            text += f"\nLast error: {st['last_error']}"
        return text

    def _refresh(self):
        if self._win is None or not self._vars:
            return
        updater = self.feed.updater
        status_callback = self.actions.get("status")
        try:
            self._vars["connection"].set(status_callback() if status_callback else "")
        except Exception:
            self._vars["connection"].set("waiting for the Flipper")
        self._vars["companion"].set(f"v{COMPANION_VERSION}")
        self._vars["flipper"].set(
            f"v{updater.flipper_version}" if updater.flipper_version else "not connected")
        latest = updater.latest_companion or updater.latest
        self._vars["latest"].set(latest.get("tag") if latest else "checking…")
        self._vars["rf_status"].set(self._rf_text())
        frozen = getattr(sys, "frozen", False)
        if not frozen:
            self._widgets["companion_update"].configure(
                state="disabled", text="Self-update works in the .exe")
        elif updater.companion_update_available():
            self._widgets["companion_update"].configure(
                state="normal", text=f"Install companion update ({updater.latest_companion['tag']})")
        else:
            self._widgets["companion_update"].configure(state="disabled", text="Companion is up to date")
        if updater.flipper_update_available():
            self._widgets["flipper_update"].configure(
                state="normal", text=f"Install Flipper app update ({updater.latest['tag']})")
        else:
            self._widgets["flipper_update"].configure(state="disabled", text="Flipper app is up to date")
        if time.time() < self._note_until:
            self._vars["update_status"].set(self._note)
        elif updater.checking:
            self._vars["update_status"].set("Checking for updates…")
        elif updater.companion_update_available() or updater.flipper_update_available():
            self._vars["update_status"].set("An update is available")
        else:
            self._vars["update_status"].set("Up to date")

    def _tick(self):
        if self._win is None:
            return
        try:
            if self._visible():
                self._refresh()
            self._win.after(1000, self._tick)
        except Exception:
            log.debug("settings refresh stopped", exc_info=True)
