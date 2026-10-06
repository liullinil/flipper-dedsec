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
            from tkinter import messagebox, ttk
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
        root.title("DedSec Uplink Settings")
        root.geometry("560x500")
        root.minsize(500, 420)
        root.protocol("WM_DELETE_WINDOW", root.withdraw)
        try:
            root.iconname("DedSec Uplink")
        except Exception:
            pass

        outer = ttk.Frame(root, padding=18)
        outer.grid(row=0, column=0, sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)

        title = ttk.Label(outer, text="DedSec Uplink", font=("Segoe UI", 16, "bold"))
        title.grid(row=0, column=0, sticky="w")
        ttk.Label(outer, text="Companion preferences and connection status",
                  foreground="#666666").grid(row=1, column=0, sticky="w", pady=(0, 14))

        status = ttk.LabelFrame(outer, text="Status", padding=10)
        status.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        status.columnconfigure(1, weight=1)
        self._vars["connection"] = tk.StringVar(value="")
        self._vars["companion"] = tk.StringVar(value="")
        self._vars["flipper"] = tk.StringVar(value="")
        self._vars["latest"] = tk.StringVar(value="")
        for row, (label, key) in enumerate((("Connection", "connection"),
                                             ("Companion", "companion"),
                                             ("Flipper app", "flipper"),
                                             ("Latest release", "latest"))):
            ttk.Label(status, text=label + ":").grid(row=row, column=0, sticky="w", padx=(0, 12))
            ttk.Label(status, textvariable=self._vars[key]).grid(row=row, column=1, sticky="w")

        terminal = ttk.LabelFrame(outer, text="Terminal access", padding=10)
        terminal.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        terminal.columnconfigure(1, weight=1)
        self._vars["cmd_enabled"] = tk.BooleanVar(value=bool(self.cfg.get("cmd_enabled", True)))
        ttk.Checkbutton(terminal, text="Allow commands from Flipper", variable=self._vars["cmd_enabled"],
                        command=self._apply_shell).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(terminal, text="Shell:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self._vars["shell"] = tk.StringVar(value=self.cfg.get("shell", "cmd"))
        shell = ttk.Combobox(terminal, textvariable=self._vars["shell"],
                             values=("cmd", "powershell"), state="readonly", width=18)
        shell.grid(row=1, column=1, sticky="w", pady=(8, 0))
        shell.bind("<<ComboboxSelected>>", lambda _event: self._apply_shell())
        self._widgets["shell"] = shell

        integration = ttk.LabelFrame(outer, text="Windows integration", padding=10)
        integration.grid(row=4, column=0, sticky="ew", pady=(0, 10))
        self._vars["autostart"] = tk.BooleanVar(value=config.autostart_enabled())
        self._vars["hooks"] = tk.BooleanVar(value=hooks.installed())
        ttk.Checkbutton(integration, text="Start with Windows", variable=self._vars["autostart"],
                        command=self._apply_autostart).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(integration, text="Install Claude Code hooks", variable=self._vars["hooks"],
                        command=self._apply_hooks).grid(row=1, column=0, sticky="w", pady=(6, 0))

        updates = ttk.LabelFrame(outer, text="Updates", padding=10)
        updates.grid(row=5, column=0, sticky="ew", pady=(0, 10))
        updates.columnconfigure(0, weight=1)
        self._vars["update_status"] = tk.StringVar(value="Checking for updates…")
        ttk.Label(updates, textvariable=self._vars["update_status"]).grid(row=0, column=0, sticky="w")
        buttons = ttk.Frame(updates)
        buttons.grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Button(buttons, text="Check now", command=self._check_updates).grid(row=0, column=0, padx=(0, 6))
        self._widgets["companion_update"] = ttk.Button(
            buttons, text="Install companion update", command=self._install_companion)
        self._widgets["companion_update"].grid(row=0, column=1, padx=(0, 6))
        self._widgets["flipper_update"] = ttk.Button(
            buttons, text="Install Flipper app update", command=self._install_flipper)
        self._widgets["flipper_update"].grid(row=0, column=2)

        footer = ttk.Frame(outer)
        footer.grid(row=6, column=0, sticky="ew", pady=(4, 0))
        ttk.Button(footer, text="Open log", command=self._open_log).pack(side="left")
        ttk.Button(footer, text="Close", command=root.withdraw).pack(side="right")

        self._refresh()
        root.after(1000, self._tick)
        try:
            root.mainloop()
        finally:
            self._root = None
            self._opening = False

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
