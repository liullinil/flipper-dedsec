"""DedSec Uplink: tray companion that feeds the Flipper app over BLE and runs its commands."""
import argparse
import ctypes
import logging
import logging.handlers
import os
import queue
import socket
import sys
import threading
import time
import webbrowser
from collections import deque

from . import config, hooks
from .claude import ClaudeWatcher
from .codex import CodexWatcher
from .common import ascii_text
from .frozen import apply_companion_update
from .link import Link
from .rf_sync import RF_TAGS, RfSync
from .shell import Shell
from .session_views import SessionViews
from .sysmon import SysMon
from .tray_popup import TrayPopup, install_click_handler
from .ui import UiThread
from .updater import AUTO_RETRY, COMPANION_VERSION, Updater

APP_DIR = config.APP_DIR
LOG_PATH = os.path.join(APP_DIR, "uplink.log")
MAX_ROWS = 10
SESSION_PERIOD = 2.0
OUTBOX_PER_FRAME = 24   # command-output lines flushed to the Flipper per send cycle
CLOCK_EVERY = 600       # seconds between Z| clock lines (the Flipper stamps RF events with it)
RF_STORE = os.path.join(APP_DIR, "rf_hunter")
GITHUB_URL = "https://github.com/liullinil/flipper-dedsec"
UPDATE_TICK = 60       # seconds between automatic update decisions

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
        self.rf_sync = RfSync(RF_STORE, send_clock=False)   # frame() sends Z| itself
        self.next_clock = 0.0
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
        if self.updater.busy():
            # an update is streaming: keep the radio for it (its chunks keep the link alive)
            return []
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
        self.updater.auto_flipper()
        if now >= self.next_clock:
            # PC clock for RF timestamps: the Flipper's RTC keeps local time
            offset = -(time.altzone if time.localtime(now).tm_isdst > 0 else time.timezone) // 60
            lines.append(f"Z|{int(now)}|{offset}")
            self.next_clock = now + CLOCK_EVERY
        if self.shell:
            self.shell.poll_timeout()
        return lines

    def urgent(self):
        """Lines that go out immediately: update chunks, RF import requests, command output."""
        lines = self.updater.urgent()
        try:
            lines += self.rf_sync.urgent_lines()
        except Exception:
            log.exception("RF import failed")
        with self.outbox_lock:
            for _ in range(min(OUTBOX_PER_FRAME, len(self.outbox))):
                lines.append(self.outbox.popleft())
        return lines

    def counts(self):
        with self.lock:
            return {k: len(v) for k, v in self.rows.items()}

    def on_link(self, status):
        """Link status from the BLE thread."""
        up = status == "connected"
        if up:
            self.next_clock = 0.0   # send the clock with the first frame
        self.rf_sync.on_link(up)

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

    def _purge_output(self, seq):
        """Drop queued output of a cancelled command, so its exit line is not stuck behind it."""
        prefix = f"O|{seq}|"
        with self.outbox_lock:
            kept = [line for line in self.outbox if not line.startswith(prefix)]
            self.outbox.clear()
            self.outbox.extend(kept)

    def reset_shell(self):
        """Restart the shell (another kind was chosen, or commands were disabled)."""
        shell, self.shell = self.shell, None
        if shell:
            try:
                shell.stop()
            except Exception:
                log.exception("cannot stop the shell")

    def handle_rx(self, line):
        """Flipper -> PC line (command requests). Runs on the BLE thread; keep it light."""
        parts = line.split("|")
        tag = parts[0]
        if tag in RF_TAGS:
            try:
                self.rf_sync.handle_line(parts)
            except Exception:
                log.exception("bad RF line %r", line[:80])
            return
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
        elif tag == "K" and len(parts) >= 2:
            self._purge_output(parts[1])
            # even without a running shell this answers X|seq|-1, so the Flipper leaves RUN
            self._get_shell().cancel(parts[1])
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
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # Cyrillic names
    except Exception:
        pass
    feed.poll_sessions()
    time.sleep(1.0)
    for line in feed.frame():
        print(line)


def run_console(feed):
    def on_status(status, name):
        feed.on_link(status)
        print(f"[link] {status} {name}")

    link = Link(feed.frame, on_status=on_status, on_rx=feed.handle_rx, urgent_source=feed.urgent)
    link.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        link.stop()
        feed.shutdown()


def ensure_integration():
    """Start with Windows and the Claude Code hooks are always on: re-enabled at every start."""
    try:
        config.ensure_autostart()
    except Exception:
        log.exception("cannot enable start with Windows")
    try:
        hooks.ensure_installed()
    except Exception:
        log.exception("cannot install the Claude Code hooks")


def run_tray(feed, cfg):
    import pystray
    from .icon import make_icon

    colors = {"connected": "#38e070", "searching": "#f0b400", "paused": "#808080"}
    state = {"status": "starting", "name": ""}
    stopping = threading.Event()
    companion = {"busy": False, "retry_at": 0.0}
    ui = UiThread()                    # one Tk thread for the tray panel and the RF analyzer
    analyzer = {"window": None}

    def status_text(_item=None):
        c = feed.counts()
        tail = f" {state['name']}" if state["status"] == "connected" else ""
        return f"{state['status'].upper()}{tail}  ·  CODEX {c['X']}  ·  CLAUDE {c['C']}"

    def on_status(status, name):
        feed.on_link(status)
        state.update(status=status, name=name)
        icon.icon = make_icon(colors.get(status, "#e03030"))
        icon.title = f"DEDSEC // UPLINK  ·  {status_text()}"
        icon.update_menu()

    link = Link(feed.frame, on_status=on_status, on_rx=feed.handle_rx, urgent_source=feed.urgent)

    def toggle_pause(*_args):
        link.set_paused(not link.paused)
        icon.update_menu()

    def quit_app(*_args):
        stopping.set()
        link.stop()
        feed.shutdown()
        ui.stop()                      # closes the windows on their own thread
        icon.stop()

    def open_analyzer(*_args):
        def show():
            from .rf_analyzer import AnalyzerWindow
            window = analyzer["window"]
            if window is not None and window.alive:
                window.show()
                return
            analyzer["window"] = AnalyzerWindow(ui.root, RF_STORE, sync=feed.rf_sync)
        ui.call(show)

    def close_analyzer():
        window, analyzer["window"] = analyzer["window"], None
        if window is not None:
            window.close()

    ui.on_close(close_analyzer)

    def open_github(*_args):
        webbrowser.open(GITHUB_URL)

    # ---- automatic updates: the companion replaces itself; the Flipper app is pushed by Feed
    def companion_downloaded(temp_path, error):
        if error:
            log.warning("companion update failed: %s", error)
            companion.update(busy=False, retry_at=time.time() + AUTO_RETRY)
            return
        try:
            apply_companion_update(temp_path, quit_app)
        except Exception:
            log.exception("cannot apply the companion update")
            companion.update(busy=False, retry_at=time.time() + AUTO_RETRY)

    def auto_updates():
        while not stopping.wait(UPDATE_TICK):
            updater = feed.updater
            updater.check_async()
            if (getattr(sys, "frozen", False) and not companion["busy"] and not updater.busy()
                    and time.time() >= companion["retry_at"]
                    and updater.companion_update_available()):
                companion["busy"] = True
                log.info("installing companion %s automatically",
                         updater.latest_companion["tag"])
                updater.install_companion_async(sys.executable, companion_downloaded)

    # A small plain menu stays for the case the styled panel cannot hook pystray.
    icon = pystray.Icon(
        "dedsec_uplink", make_icon("#f0b400"), "DEDSEC // UPLINK",
        menu=pystray.Menu(
            pystray.MenuItem(status_text, None, enabled=False),
            pystray.MenuItem("RF HUNTER ANALYZER…", open_analyzer, default=True),
            pystray.MenuItem("PAUSE UPLINK", toggle_pause, checked=lambda _i: link.paused),
            pystray.MenuItem("QUIT", quit_app),
        ))

    def panel_state():
        c = feed.counts()
        updater = feed.updater
        line = f"CODEX {c['X']}   CLAUDE {c['C']}"
        try:
            rf = feed.rf_sync.status()
            if rf.get("pending") is not None:
                line += f"   RF {rf['pending']} waiting"
        except Exception:
            pass
        flipper = f"v{updater.flipper_version}" if updater.flipper_version else "-"
        lines = [(line, "#7595a0"),
                 (f"COMPANION v{COMPANION_VERSION}   FLIPPER {flipper}", "#4d6b75")]
        if companion["busy"]:
            tag = (updater.latest_companion or {}).get("tag", "")
            lines.append((f"COMPANION UPDATE {tag}: downloading…", "#f28a32"))
        return {"status": state["status"], "name": state["name"], "lines": lines,
                "ota": updater.progress()}

    def panel_items():
        return [
            {"label": "RF HUNTER ANALYZER", "action": open_analyzer, "style": "accent"},
            {"label": "RESUME UPLINK" if link.paused else "PAUSE UPLINK", "action": toggle_pause},
            {"label": "QUIT", "action": quit_app, "style": "danger"},
        ]

    panel = TrayPopup(ui, panel_state, panel_items, on_join=open_github)
    if not install_click_handler(icon, panel.toggle_at):
        log.info("styled tray panel unavailable, using the plain menu")

    def setup(ic):
        ic.visible = True
        feed.updater.on_change = ic.update_menu
        link.start()
        threading.Thread(target=auto_updates, name="auto-update", daemon=True).start()

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
    log.info("DedSec Uplink %s starting on %s (shell=%s cmd_enabled=%s autostart=%s)", COMPANION_VERSION,
             feed.host, cfg.get("shell"), cfg.get("cmd_enabled"), config.autostart_enabled())
    if args.console:
        run_console(feed)
    else:
        ensure_integration()
        run_tray(feed, cfg)
    log.info("DedSec Uplink stopped")
    # Leave without interpreter teardown: daemon threads (BLE, shell, Tk) may still hold
    # objects whose finalizers must not run on this thread (Tcl aborts the process).
    logging.shutdown()
    os._exit(0)
