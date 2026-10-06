"""DedSec Uplink: tray companion that feeds the Flipper app over BLE and runs its commands."""
import argparse
import ctypes
import logging
import logging.handlers
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import tempfile
from collections import deque

from . import config, hooks
from .claude import ClaudeWatcher
from .codex import CodexWatcher
from .common import ascii_text
from .link import Link
from .shell import Shell
from .session_views import SessionViews
from .settings_window import SettingsWindow
from .sysmon import SysMon
from .updater import Updater

APP_DIR = config.APP_DIR
LOG_PATH = os.path.join(APP_DIR, "uplink.log")
MAX_ROWS = 10
SESSION_PERIOD = 2.0
OUTBOX_PER_FRAME = 24   # command-output lines flushed to the Flipper per send cycle

log = logging.getLogger("uplink")


class Feed:
    """Builds the frame for the Flipper and runs remote commands (see uplink.c for the format)."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.sys = SysMon()
        self.codex = CodexWatcher()
        self.claude = ClaudeWatcher()
        self.views = SessionViews(os.path.join(config.APP_DIR, "session_acks.json"))
        self.host = ascii_text(socket.gethostname(), 20)
        self.rows = {"X": [], "C": []}
        self.last_poll = 0.0
        self.lock = threading.Lock()
        self.outbox = deque(maxlen=2000)
        self.outbox_lock = threading.Lock()
        self.commands = queue.Queue()
        self.shell = None
        self.updater = Updater()
        self.updater.check_async(force=True)
        self.worker = threading.Thread(target=self._command_worker, name="cmd-worker", daemon=True)
        self.worker.start()

    # ------------------------------------------------------------------ sessions
    def poll_sessions(self):
        try:
            codex = self.codex.poll()
        except Exception:
            log.exception("codex poll failed")
            codex = self.rows["X"]
        try:
            claude = self.claude.poll()
        except Exception:
            log.exception("claude poll failed")
            claude = self.rows["C"]
        with self.lock:
            codex = self.views.update("X", codex)
            claude = self.views.update("C", claude)
            self.rows = {"X": codex[:MAX_ROWS], "C": claude[:MAX_ROWS]}
        self.last_poll = time.time()

    def frame(self):
        if time.time() - self.last_poll >= SESSION_PERIOD:
            self.poll_sessions()
        s = self.sys.sample()
        lines = [
            f"H|{self.host}",
            "S|{cpu}|{ram}|{dsk}|{rd}|{wr}|{up}|{dn}|{used_mb}|{total_mb}".format(**s),
        ]
        now = time.time()
        with self.lock:
            for kind, rows in self.rows.items():
                lines.append(f"L|{kind}|{len(rows)}")
                for i, r in enumerate(rows):
                    lines.append(f"I|{kind}|{i}|{r.key}|{r.state}|{r.done}|{r.total}|"
                                 f"{r.age(now)}|{r.attn}|{r.name}|{r.detail}")
        self.updater.check_async()
        lines += self.updater.advert()
        if self.shell:
            self.shell.poll_timeout()
        return lines

    def urgent(self):
        """Lines that go out immediately: update chunks first, then command output."""
        lines = self.updater.urgent()
        with self.outbox_lock:
            for _ in range(min(OUTBOX_PER_FRAME, len(self.outbox))):
                lines.append(self.outbox.popleft())
        return lines

    def counts(self):
        with self.lock:
            return {k: len(v) for k, v in self.rows.items()}

    # ------------------------------------------------------------------ remote cmd
    def _emit(self, line):
        with self.outbox_lock:
            self.outbox.append(line)

    def _get_shell(self):
        if self.shell is None:
            self.shell = Shell(
                self.cfg.get("shell", "cmd"),
                on_output=lambda seq, text: self._emit(f"O|{seq}|{text}"),
                on_exit=lambda seq, code: self._emit(f"X|{seq}|{code}"),
                on_cwd=lambda cwd: self._emit(f"W|{cwd}"))
        return self.shell

    def handle_rx(self, line):
        """Flipper -> PC line (command requests). Runs on the BLE thread; keep it light."""
        parts = line.split("|")
        tag = parts[0]
        if tag == "C" and len(parts) >= 3:
            seq = parts[1]
            command = "|".join(parts[2:])
            if not self.cfg.get("cmd_enabled", True):
                self._emit(f"O|{seq}|remote shell is disabled on the PC")
                self._emit(f"X|{seq}|-1")
                return
            self.commands.put((seq, command))
        elif tag == "T" and len(parts) >= 3:
            # Text entered while an interactive command (for example Codex) owns the PTY.
            seq = parts[1]
            text = "|".join(parts[2:])
            self._get_shell().write_input(seq, text)
        elif tag == "K" and len(parts) >= 2 and self.shell:
            self.shell.cancel(parts[1])
        elif tag == "V" and len(parts) >= 2:
            self.updater.set_flipper_version(parts[1])
        elif tag == "U" and len(parts) >= 2:
            log.info("flipper asked for update %s", parts[1])
            threading.Thread(target=self.updater.request, args=(parts[1],), daemon=True).start()
        elif tag == "UA" and len(parts) >= 2:
            try:
                self.updater.on_ack(int(parts[1]))
            except ValueError:
                pass

    def _command_worker(self):
        while True:
            seq, command = self.commands.get()
            try:
                log.info("exec seq=%s: %s", seq, command)
                self._get_shell().run(seq, command)
            except Exception:
                log.exception("command failed")
                self._emit(f"X|{seq}|-1")

    def shutdown(self):
        if self.shell:
            self.shell.stop()


# ---------------------------------------------------------------------------- setup
def setup_logging(console):
    os.makedirs(APP_DIR, exist_ok=True)
    handlers = [logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=512 * 1024,
                                                     backupCount=2, encoding="utf-8")]
    if console and sys.stdout is not None:      # the windowed .exe has no console
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def single_instance():
    if os.name != "nt":
        return True
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\DedSecUplinkCompanion")
    return ctypes.windll.kernel32.GetLastError() != 183


def dump(feed):
    feed.poll_sessions()
    time.sleep(1.0)
    for line in feed.frame():
        print(line)


def run_console(feed):
    link = Link(feed.frame, on_status=lambda st, name: print(f"[link] {st} {name}"),
                on_rx=feed.handle_rx, urgent_source=feed.urgent)
    link.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        link.stop()
        feed.shutdown()


def run_tray(feed, cfg):
    import pystray
    from .icon import make_icon

    colors = {"connected": "#38e070", "searching": "#f0b400", "paused": "#808080"}
    state = {"status": "starting", "name": ""}

    def status_text(_item=None):
        c = feed.counts()
        tail = f" {state['name']}" if state["status"] == "connected" else ""
        return f"{state['status']}{tail} | codex {c['X']} | claude {c['C']}"

    def version_text(_item=None):
        return feed.updater.version_status()

    def update_label(_item=None):
        if feed.updater.companion_update_available():
            return f"Install companion update ({feed.updater.latest_companion['tag']})"
        return "Install companion update"

    def flipper_update_label(_item=None):
        if feed.updater.flipper_update_available():
            return f"Install Flipper app update ({feed.updater.latest['tag']})"
        return "Install Flipper app update"

    def on_status(status, name):
        state.update(status=status, name=name)
        icon.icon = make_icon(colors.get(status, "#e03030"))
        icon.title = f"DedSec Uplink: {status_text()}"
        icon.update_menu()

    def check_updates(_icon, _item):
        feed.updater.check_async(force=True)

    def install_flipper_update(_icon, _item):
        if feed.updater.request_flipper_update():
            log.info("requested Flipper app update")

    def launch_companion_update(temp_path, error):
        if error:
            log.warning("companion update unavailable: %s", error)
            return
        if not getattr(sys, "frozen", False):
            log.warning("companion self-update is only available in the packaged EXE")
            return
        target = sys.executable
        # A separate PowerShell helper waits for this process to exit, replaces the locked
        # executable, and starts the new version. The old tray process then quits normally.
        def quote(value):
            return "'" + str(value).replace("'", "''") + "'"
        script = os.path.join(tempfile.gettempdir(), "DedSecUplink-apply-update.ps1")
        body = (
            "$p = Get-Process -Id %d -ErrorAction SilentlyContinue; "
            "if ($p) { $p.WaitForExit() }; "
            "Move-Item -LiteralPath %s -Destination %s -Force; "
            "Start-Process -FilePath %s -WindowStyle Hidden; "
            "Remove-Item -LiteralPath $MyInvocation.MyCommand.Path -Force"
        ) % (os.getpid(), quote(temp_path), quote(target), quote(target))
        try:
            with open(script, "w", encoding="utf-8") as fh:
                fh.write(body)
            subprocess.Popen(
                ["powershell.exe", "-NoLogo", "-NoProfile", "-WindowStyle", "Hidden",
                 "-ExecutionPolicy", "Bypass", "-File", script],
                creationflags=0x08000000,
            )
            icon.stop()
        except Exception:
            log.exception("cannot start companion updater")

    def install_companion_update(_icon, _item):
        if not feed.updater.companion_update_available():
            return
        feed.updater.install_companion_async(sys.executable, launch_companion_update)

    link = Link(feed.frame, on_status=on_status, on_rx=feed.handle_rx, urgent_source=feed.urgent)

    def toggle_pause(_icon, _item):
        link.set_paused(not link.paused)

    def toggle_cmd(_icon, _item):
        cfg["cmd_enabled"] = not cfg.get("cmd_enabled", True)
        config.save(cfg)
        log.info("remote shell %s", "enabled" if cfg["cmd_enabled"] else "disabled")

    def toggle_autostart(_icon, _item):
        config.set_autostart(not config.autostart_enabled())
        icon.update_menu()

    def toggle_hooks(_icon, _item):
        hooks.install(remove=hooks.installed())
        log.info("claude hooks %s", "installed" if hooks.installed() else "removed")
        icon.update_menu()

    def open_log(_icon, _item):
        subprocess.Popen(["notepad.exe", LOG_PATH])

    def quit_app(_icon, _item):
        link.stop()
        feed.shutdown()
        if settings_window:
            settings_window.close()
        icon.stop()

    # Long-lived settings live in a normal window; the tray menu remains useful
    # for status and launching that window without burying every preference in
    # a nested menu.  Callbacks deliberately have no pystray arguments so the
    # same actions can be used by Tk buttons.
    settings_window = None

    def open_settings(_icon=None, _item=None):
        if settings_window:
            settings_window.show()

    icon = pystray.Icon(
        "dedsec_uplink", make_icon("#f0b400"), "DedSec Uplink",
        menu=pystray.Menu(
            pystray.MenuItem(status_text, None, enabled=False),
            pystray.MenuItem(version_text, None, enabled=False),
            pystray.MenuItem("Open settings…", open_settings),
            pystray.MenuItem("Pause uplink", toggle_pause, checked=lambda _i: link.paused),
            pystray.MenuItem("Quit", quit_app),
        ))

    settings_window = SettingsWindow(
        feed,
        cfg,
        actions={
            "check_updates": lambda: check_updates(None, None),
            "install_companion": lambda: install_companion_update(None, None),
            "install_flipper": lambda: install_flipper_update(None, None),
            "open_log": lambda: open_log(None, None),
            "status": status_text,
            "on_changed": icon.update_menu,
        },
    )

    def setup(ic):
        ic.visible = True
        feed.updater.on_change = ic.update_menu
        link.start()

    icon.run(setup=setup)


def main():
    ap = argparse.ArgumentParser(description="DedSec Uplink PC companion for the Flipper app")
    ap.add_argument("--dump", action="store_true", help="print one frame and exit (no BLE)")
    ap.add_argument("--console", action="store_true", help="run without the tray icon")
    ap.add_argument("--install-autostart", action="store_true", help="enable start with Windows")
    ap.add_argument("--uninstall-autostart", action="store_true", help="disable start with Windows")
    ap.add_argument("--install-claude-hooks", action="store_true", help="add Claude Code hooks")
    ap.add_argument("--remove-claude-hooks", action="store_true", help="remove Claude Code hooks")
    ap.add_argument("--hook", action="store_true", help=argparse.SUPPRESS)  # run by Claude Code
    args = ap.parse_args()
    if args.hook:
        hooks.record_event()
        return
    if args.install_claude_hooks or args.remove_claude_hooks:
        cmd = hooks.install(remove=args.remove_claude_hooks)
        print("claude hooks", "removed" if args.remove_claude_hooks else "installed: " + cmd)
        return
    setup_logging(console=args.console or args.dump)
    cfg = config.load()

    if args.install_autostart:
        config.set_autostart(True)
        print("autostart enabled")
        return
    if args.uninstall_autostart:
        config.set_autostart(False)
        print("autostart disabled")
        return

    feed = Feed(cfg)
    if args.dump:
        dump(feed)
        return
    if not single_instance():
        log.info("another copy is already running")
        return
    log.info("DedSec Uplink starting on %s (shell=%s cmd_enabled=%s autostart=%s)",
             feed.host, cfg.get("shell"), cfg.get("cmd_enabled"), config.autostart_enabled())
    if args.console:
        run_console(feed)
    else:
        run_tray(feed, cfg)
    log.info("DedSec Uplink stopped")
