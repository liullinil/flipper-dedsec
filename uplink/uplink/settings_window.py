"""Desktop settings window for the DedSec Uplink companion.

The tray icon is intentionally small and transient.  This module owns the longer
form controls (terminal access, Windows integration, and updates) in a normal
Tk/ttk window.  Tkinter is imported lazily so ``--dump`` and headless builds do
not require a display.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from typing import Callable, Mapping, Optional

from . import config, hooks
from .updater import COMPANION_VERSION

log = logging.getLogger("uplink.settings")


class SettingsWindow:
    """A single-instance, non-modal settings window.

    ``actions`` contains optional no-argument callbacks supplied by the tray
    runner.  Keeping update/install operations outside this module prevents the
    window from knowing about pystray and makes it safe to test independently.
    """

    def __init__(self, feed, cfg: dict, actions: Optional[Mapping[str, Callable]] = None):
        self.feed = feed
        self.cfg = cfg
        self.actions = dict(actions or {})
        self._thread: Optional[threading.Thread] = None
        self._opening = False
        self._root = None
        self._vars = {}
        self._widgets = {}

    # ------------------------------------------------------------------ public API
    def show(self):
        """Show or focus the existing window without blocking the tray callback."""
        if self._root is not None:
            try:
                if self._root.winfo_exists():
                    self._root.deiconify()
                    self._root.lift()
                    self._root.focus_force()
                    return
            except Exception:
                self._root = None
        if self._opening:
            return
        self._opening = True
        self._thread = threading.Thread(target=self._run, name="settings-window", daemon=True)
        self._thread.start()

    def close(self):
        """Close the window if it is open (used when the tray exits)."""
        root = self._root
        if root is None:
            return
        try:
            root.after(0, root.destroy)
        except Exception:
            self._root = None

    # ------------------------------------------------------------------ Tk construction
    def _run(self):
        try:
            import tkinter as tk
            from tkinter import ttk
        except Exception as exc:  # pragma: no cover - only used on minimal Python installs
            self._opening = False
            log.warning("settings window unavailable: %s", exc)
            return

        try:
            root = tk.Tk()
        except Exception as exc:  # pragma: no cover - requires a display to exercise
            self._opening = False
            log.warning("cannot create settings window: %s", exc)
            return

        self._root = root
        self._opening = False
        bg = "#071018"
        card = "#0d1c26"
        card_alt = "#102733"
        border = "#1d4050"
        text = "#d7edf2"
        muted = "#7595a0"
        cyan = "#27e0e8"
        orange = "#f28a32"
        root.title("DEDSEC // UPLINK")
        root.geometry("700x640")
        root.minsize(620, 560)
        root.configure(background=bg)
        root.protocol("WM_DELETE_WINDOW", root.withdraw)
        try:
            root.iconname("DEDSEC // UPLINK")
        except Exception:
            pass

        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Cyber.TFrame", background=bg)
        style.configure("Card.TFrame", background=card)
        style.configure("AltCard.TFrame", background=card_alt)
        style.configure("Cyber.TLabel", background=bg, foreground=text,
                        font=("Segoe UI", 9))
        style.configure("Muted.TLabel", background=bg, foreground=muted,
                        font=("Segoe UI", 9))
        style.configure("Card.TLabel", background=card, foreground=text,
                        font=("Segoe UI", 9))
        style.configure("CardMuted.TLabel", background=card, foreground=muted,
                        font=("Segoe UI", 8))
        style.configure("Value.TLabel", background=card, foreground=cyan,
                        font=("Segoe UI", 12, "bold"))
        style.configure("VersionTitle.TLabel", background=card, foreground=muted,
                        font=("Segoe UI", 8, "bold"))
        style.configure("Cyber.TLabelframe", background=card, foreground=text,
                        bordercolor=border, lightcolor=border, darkcolor=border,
                        relief="solid", borderwidth=1)
        style.configure("Cyber.TLabelframe.Label", background=card, foreground=cyan,
                        font=("Segoe UI", 9, "bold"))
        style.configure("Cyber.TCheckbutton", background=card, foreground=text,
                        font=("Segoe UI", 9), focuscolor=card)
        style.map("Cyber.TCheckbutton", background=[("active", card)],
                  foreground=[("disabled", muted), ("active", text)])
        style.configure("Cyber.TCombobox", fieldbackground=card_alt,
                        background=card_alt, foreground=text, arrowcolor=cyan,
                        bordercolor=border, lightcolor=border, darkcolor=border)
        style.map("Cyber.TCombobox", fieldbackground=[("readonly", card_alt)],
                  foreground=[("readonly", text)])
        style.configure("Cyber.TButton", background=card_alt, foreground=text,
                        bordercolor=border, lightcolor=border, darkcolor=border,
                        padding=(12, 7), font=("Segoe UI", 9, "bold"))
        style.map("Cyber.TButton", background=[("active", border), ("disabled", card)],
                  foreground=[("disabled", muted), ("active", cyan)])
        style.configure("Action.TButton", background=orange, foreground="#071018",
                        bordercolor=orange, lightcolor=orange, darkcolor=orange,
                        padding=(15, 8), font=("Segoe UI", 9, "bold"))
        style.map("Action.TButton", background=[("active", "#ffad57"), ("disabled", card)],
                  foreground=[("disabled", muted), ("active", "#071018")])
        style.configure("Close.TButton", background=bg, foreground=muted,
                        bordercolor=border, padding=(12, 6), font=("Segoe UI", 9))
        style.map("Close.TButton", background=[("active", card_alt)], foreground=[("active", text)])
        style.configure("Badge.TLabel", background="#123b45", foreground=cyan,
                        padding=(10, 4), font=("Segoe UI", 9, "bold"))

        outer = ttk.Frame(root, padding=(20, 16, 20, 14), style="Cyber.TFrame")
        outer.grid(row=0, column=0, sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)

        header = tk.Canvas(outer, height=78, background=bg, highlightthickness=0,
                           borderwidth=0)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        self._widgets["header_canvas"] = header
        self._scan_y = 0
        header.bind("<Configure>", lambda _event: self._draw_header(header, bg, cyan, orange))
        self._draw_header(header, bg, cyan, orange)

        status = ttk.LabelFrame(outer, text="  LINK STATUS  ", padding=(12, 10),
                                style="Cyber.TLabelframe")
        status.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        status.columnconfigure(0, weight=1)
        self._vars["connection"] = tk.StringVar(value="")
        self._vars["companion"] = tk.StringVar(value="")
        self._vars["flipper"] = tk.StringVar(value="")
        self._vars["latest"] = tk.StringVar(value="")
        self._widgets["connection"] = ttk.Label(status, textvariable=self._vars["connection"],
                                                 style="Badge.TLabel", anchor="w")
        self._widgets["connection"].grid(row=0, column=0, sticky="w")

        versions = ttk.Frame(status, style="Card.TFrame")
        versions.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        for col in range(3):
            versions.columnconfigure(col, weight=1)
        for col, (label, key) in enumerate((("COMPANION", "companion"),
                                             ("FLIPPER APP", "flipper"),
                                             ("LATEST RELEASE", "latest"))):
            version_card = ttk.Frame(versions, style="AltCard.TFrame", padding=(10, 7))
            version_card.grid(row=0, column=col, sticky="ew",
                              padx=(0 if col == 0 else 4, 4 if col < 2 else 0))
            ttk.Label(version_card, text=label, style="VersionTitle.TLabel").pack(anchor="w")
            ttk.Label(version_card, textvariable=self._vars[key], style="Value.TLabel").pack(
                anchor="w", pady=(2, 0))

        controls = ttk.Frame(outer, style="Cyber.TFrame")
        controls.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        controls.columnconfigure(0, weight=1)
        controls.columnconfigure(1, weight=1)

        terminal = ttk.LabelFrame(controls, text="  TERMINAL ACCESS  ", padding=(12, 10),
                                  style="Cyber.TLabelframe")
        terminal.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        terminal.columnconfigure(1, weight=1)
        self._vars["cmd_enabled"] = tk.BooleanVar(value=bool(self.cfg.get("cmd_enabled", True)))
        ttk.Checkbutton(terminal, text="Allow commands from Flipper", variable=self._vars["cmd_enabled"],
                        command=self._apply_shell, style="Cyber.TCheckbutton").grid(
                            row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(terminal, text="Active shell", style="CardMuted.TLabel").grid(
            row=1, column=0, sticky="w", pady=(10, 0))
        self._vars["shell"] = tk.StringVar(value=self.cfg.get("shell", "cmd"))
        shell = ttk.Combobox(terminal, textvariable=self._vars["shell"],
                             values=("cmd", "powershell"), state="readonly", width=18,
                             style="Cyber.TCombobox")
        shell.grid(row=1, column=1, sticky="ew", pady=(10, 0))
        shell.bind("<<ComboboxSelected>>", lambda _event: self._apply_shell())
        self._widgets["shell"] = shell

        integration = ttk.LabelFrame(controls, text="  WINDOWS INTEGRATION  ", padding=(12, 10),
                                     style="Cyber.TLabelframe")
        integration.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        self._vars["autostart"] = tk.BooleanVar(value=config.autostart_enabled())
        self._vars["hooks"] = tk.BooleanVar(value=hooks.installed())
        ttk.Checkbutton(integration, text="Start with Windows", variable=self._vars["autostart"],
                        command=self._apply_autostart, style="Cyber.TCheckbutton").grid(
                            row=0, column=0, sticky="w")
        ttk.Checkbutton(integration, text="Install Claude Code hooks", variable=self._vars["hooks"],
                        command=self._apply_hooks, style="Cyber.TCheckbutton").grid(
                            row=1, column=0, sticky="w", pady=(10, 0))

        updates = ttk.LabelFrame(outer, text="  UPDATE CHANNEL  ", padding=(12, 10),
                                 style="Cyber.TLabelframe")
        updates.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        updates.columnconfigure(0, weight=1)
        self._vars["update_status"] = tk.StringVar(value="Checking for updates…")
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
        footer.grid(row=4, column=0, sticky="ew", pady=(4, 0))
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, text="DEDSEC // LOCAL CONTROL NODE", style="Muted.TLabel").grid(
            row=0, column=0, sticky="w")
        ttk.Button(footer, text="OPEN LOG", command=self._open_log,
                   style="Close.TButton").grid(row=0, column=1, padx=(8, 5))
        ttk.Button(footer, text="CLOSE", command=root.withdraw,
                   style="Close.TButton").grid(row=0, column=2)

        self._refresh()
        self._animate_header()
        root.after(1000, self._tick)
        try:
            root.mainloop()
        finally:
            self._root = None
            self._opening = False

    def _draw_header(self, canvas, bg, cyan, orange):
        """Paint the static wordmark and separator used by the scanline effect."""
        try:
            width = max(1, int(canvas.winfo_width()))
            height = max(1, int(canvas.winfo_height()))
            canvas.delete("all")
            canvas.create_text(16, 25, text="DEDSEC // UPLINK", anchor="w",
                               fill=cyan, font=("Segoe UI", 20, "bold"), tags="static")
            canvas.create_text(18, 52, text="LOCAL COMPANION  //  CONTROL NODE",
                               anchor="w", fill=orange, font=("Segoe UI", 8, "bold"), tags="static")
            canvas.create_line(width - 170, height - 17, width - 2, height - 17,
                               fill="#173542", width=1, tags="static")
            canvas.create_text(width - 5, height - 10, text="SECURE LINK",
                               anchor="e", fill="#4d7782", font=("Segoe UI", 7), tags="static")
        except Exception:
            log.debug("header redraw failed", exc_info=True)

    def _animate_header(self):
        """Move a low-contrast scanline and occasional glitch mark."""
        root = self._root
        canvas = self._widgets.get("header_canvas")
        if root is None or canvas is None:
            return
        try:
            width = max(1, int(canvas.winfo_width()))
            height = max(1, int(canvas.winfo_height()))
            y = self._scan_y % height
            canvas.delete("scan")
            canvas.create_line(0, y, width, y, fill="#123541", width=1, tags="scan")
            if self._scan_y % 23 == 0:
                x = width - 130 - ((self._scan_y // 23) % 5) * 8
                canvas.create_rectangle(x, y, min(width - 5, x + 19), y + 1,
                                        fill="#1e5360", outline="", tags="scan")
            self._scan_y = (self._scan_y + 3) % height
            root.after(90, self._animate_header)
        except Exception:
            log.debug("header animation stopped", exc_info=True)

    # ------------------------------------------------------------------ actions
    def _apply_shell(self):
        self.cfg["cmd_enabled"] = bool(self._vars["cmd_enabled"].get())
        shell = self._vars["shell"].get()
        self.cfg["shell"] = shell if shell in ("cmd", "powershell") else "cmd"
        config.save(self.cfg)
        # A running PTY has the old shell kind.  Restart it lazily so the next
        # command uses the selected shell, while preserving the connection.
        if getattr(self.feed, "shell", None) is not None:
            try:
                self.feed.shell.stop()
            except Exception:
                log.debug("cannot stop old shell", exc_info=True)
            self.feed.shell = None
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
        updater = self.feed.updater
        updater.check_async(force=True)
        self._vars["update_status"].set("Checking for updates…")

    def _install_companion(self):
        callback = self.actions.get("install_companion")
        if callback:
            self._vars["update_status"].set("Downloading companion update…")
            callback()

    def _install_flipper(self):
        callback = self.actions.get("install_flipper")
        if callback:
            self._vars["update_status"].set("Requesting Flipper app update…")
            callback()

    def _open_log(self):
        callback = self.actions.get("open_log")
        if callback:
            callback()
            return
        path = os.path.join(config.APP_DIR, "uplink.log")
        try:
            if hasattr(os, "startfile"):
                os.startfile(path)
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as exc:
            log.warning("cannot open log: %s", exc)

    def _notify_changed(self):
        callback = self.actions.get("on_changed")
        if callback:
            try:
                callback()
            except Exception:
                log.debug("settings refresh callback failed", exc_info=True)

    # ------------------------------------------------------------------ refresh
    def _refresh(self):
        if self._root is None:
            return
        updater = self.feed.updater
        try:
            counts = self.feed.counts()
            status_callback = self.actions.get("status")
            status = status_callback() if status_callback else getattr(self.feed, "link_status", "")
            status = status or "connected"
            # The tray status callback may already include counts.  Keep the
            # window's status concise and avoid duplicating them.
            if " · Codex " in status:
                self._vars["connection"].set(status)
            else:
                self._vars["connection"].set(
                    f"{status} · Codex {counts.get('X', 0)} · Claude {counts.get('C', 0)}")
        except Exception:
            self._vars["connection"].set("waiting for Flipper")
        self._vars["companion"].set(f"v{COMPANION_VERSION}")
        self._vars["flipper"].set(f"v{updater.flipper_version}" if updater.flipper_version else "not connected")
        latest = updater.latest_companion or updater.latest
        latest_tag = latest.get("tag") if latest else None
        self._vars["latest"].set(f"{latest_tag}" if latest_tag else "checking…")
        if updater.companion_update_available():
            self._widgets["companion_update"].configure(
                state="normal", text=f"Install companion update ({updater.latest_companion['tag']})")
        else:
            self._widgets["companion_update"].configure(state="disabled", text="Companion is up to date")
        if updater.flipper_update_available():
            self._widgets["flipper_update"].configure(
                state="normal", text=f"Install Flipper app update ({updater.latest['tag']})")
        else:
            self._widgets["flipper_update"].configure(state="disabled", text="Flipper app is up to date")
        if updater.checking:
            self._vars["update_status"].set("Checking for updates…")
        elif updater.companion_update_available() or updater.flipper_update_available():
            self._vars["update_status"].set("An update is available")
        else:
            self._vars["update_status"].set("Up to date")

    def _tick(self):
        if self._root is None:
            return
        try:
            self._refresh()
            self._root.after(1000, self._tick)
        except Exception:
            log.debug("settings refresh stopped", exc_info=True)
