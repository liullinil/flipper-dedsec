"""Small ConPTY/pyte terminal used by the DedSec Uplink shell.

The PC companion keeps one pseudo-terminal alive.  A real terminal is important here:
interactive programs (including Codex) inspect the console before starting their UI.  The
screen parser is deliberately separate from :mod:`shell`: it can be tested without running
commands, and callers can request a small page for the Flipper display instead of receiving
every cursor movement over BLE.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, Optional

try:  # pywinpty is Windows-only; importing the companion on another host should still work.
    from winpty import PtyProcess
except ImportError:  # pragma: no cover - exercised on non-Windows development hosts
    PtyProcess = None

try:
    import pyte
except ImportError:  # pragma: no cover - gives a useful error at spawn time
    pyte = None

log = logging.getLogger("uplink.terminal")

DEFAULT_COLS = 80
DEFAULT_ROWS = 24
DEFAULT_HISTORY = 1000
OUTPUT_FLUSH = 0.04
OUTPUT_MAX = 8192


KEY_BYTES = {
    "Enter": "\r",
    "Tab": "\t",
    "Escape": "\x1b",
    "Backspace": "\x7f",
    "Up": "\x1b[A",
    "Down": "\x1b[B",
    "Right": "\x1b[C",
    "Left": "\x1b[D",
    "Home": "\x1b[H",
    "End": "\x1b[F",
    "PageUp": "\x1b[5~",
    "PageDown": "\x1b[6~",
    "CtrlC": "\x03",
    "CtrlD": "\x04",
    "CtrlZ": "\x1a",
}


class _Screen(pyte.HistoryScreen if pyte else object):
    """HistoryScreen that sends terminal query replies back through ConPTY."""

    def __init__(self, columns, lines, history, write_input):
        if pyte is None:  # pragma: no cover - guarded by Terminal.start
            return
        super().__init__(columns, lines, history=history)
        self._write_input = write_input

    def write_process_input(self, data):
        # pyte calls this for DSR (cursor/status) and DA requests.  Sending the reply is
        # necessary for full-screen programs which wait for it before drawing their UI.
        try:
            self._write_input(data)
        except Exception:
            log.debug("terminal query reply failed", exc_info=True)


class Terminal:
    """A persistent ConPTY process and its pyte virtual screen.

    ``on_output`` receives coalesced printable text for the legacy line console.  It is
    optional; the authoritative state is :meth:`terminal_page`.  ``on_exit`` receives the
    process exit status and is called once by the reader thread.
    """

    def __init__(
        self,
        kind: str = "cmd",
        *,
        on_output: Optional[Callable[[str], None]] = None,
        on_exit: Optional[Callable[[int], None]] = None,
        dimensions: tuple[int, int] = (DEFAULT_ROWS, DEFAULT_COLS),
        history: int = DEFAULT_HISTORY,
    ):
        self.kind = kind
        self.on_output = on_output
        self.on_exit = on_exit
        self.rows, self.cols = dimensions
        self.history_size = history
        self.proc = None
        self.reader = None
        self.alive = False
        self.revision = 0
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._generation = 0
        self._stream = None
        self._screen = None
        self._plain_state = "normal"
        self._pending = ""
        self._pending_lock = threading.Lock()
        self._flush_thread = None

    # ------------------------------------------------------------------ process lifecycle
    def start(self):
        with self._lock:
            if self.alive and self.proc is not None:
                return
            if PtyProcess is None:
                raise RuntimeError("pywinpty is required for the interactive Windows shell")
            if pyte is None:
                raise RuntimeError("pyte is required for the interactive terminal")

            if self.kind == "powershell":
                # Define the marker in argv, rather than by writing a setup command into
                # stdin.  -NoExit keeps the interactive prompt attached to ConPTY.
                argv = [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-NoExit",
                    "-Command",
                    "function prompt { 'UPLINKPROMPT> ' }",
                ]
            else:
                argv = ["cmd.exe", "/d", "/q"]
            env = os.environ.copy()
            env["TERM"] = "xterm-256color"
            env.setdefault("COLORTERM", "truecolor")
            # cmd reads PROMPT from the environment at startup, so the completion marker is
            # present without writing a setup command into the child's stdin.
            if self.kind != "powershell":
                env["PROMPT"] = "UPLINKPROMPT$G$S"

            self._generation += 1
            generation = self._generation
            proc = PtyProcess.spawn(
                argv,
                cwd=os.path.expanduser("~"),
                env=env,
                dimensions=(self.rows, self.cols),
            )
            screen = _Screen(self.cols, self.rows, self.history_size, self._write_process_input)
            stream = pyte.Stream(screen)
            self.proc = proc
            self._screen = screen
            self._stream = stream
            self.alive = True
            self._stop.clear()
            self._ready.clear()
            self._plain_state = "normal"
            self._pending = ""
            self.revision += 1
            self.reader = threading.Thread(
                target=self._read_loop,
                args=(proc, generation),
                name="terminal-reader",
                daemon=True,
            )
            self.reader.start()
            if self.on_output and (self._flush_thread is None or not self._flush_thread.is_alive()):
                self._flush_thread = threading.Thread(
                    target=self._flush_loop, name="terminal-output", daemon=True
                )
                self._flush_thread.start()

        # cmd prints a banner and prompt.  Waiting briefly means the first run starts after
        # the prompt rather than interleaving with the banner; no input is sent here.
        self._ready.wait(3.0)

    def _read_loop(self, proc, generation):
        status = -1
        try:
            while not self._stop.is_set() and proc.isalive():
                try:
                    raw = proc.read(4096)
                except EOFError:
                    break
                if not raw:
                    time.sleep(0.01)
                    continue
                if generation != self._generation:
                    continue
                self._feed(raw)
        except Exception as exc:
            if not self._stop.is_set():
                log.debug("terminal reader stopped: %s", exc)
        finally:
            try:
                status = proc.exitstatus if proc.exitstatus is not None else -1
            except Exception:
                status = -1
            with self._lock:
                if generation == self._generation:
                    self.alive = False
                    self.proc = None
                    self._ready.set()
            if generation == self._generation and self.on_exit:
                try:
                    self.on_exit(status)
                except Exception:
                    log.exception("terminal exit callback failed")

    def _feed(self, raw):
        text = raw if isinstance(raw, str) else bytes(raw).decode("utf-8", "replace")
        plain = ""
        with self._lock:
            stream = self._stream
            if stream is None:
                return
            try:
                stream.feed(text)
            except Exception:
                # A malformed sequence must not kill the reader; pyte can be fed again
                # after resetting its parser state.
                log.debug("pyte parser error", exc_info=True)
                try:
                    stream.feed("\x1b[0m")
                except Exception:
                    pass
            self.revision += 1
            plain = _plain_text(text, self)
            if "UPLINKPROMPT>" in plain:
                self._ready.set()
        if self.on_output:
            if plain:
                with self._pending_lock:
                    self._pending += plain
                    if len(self._pending) > OUTPUT_MAX:
                        self._pending = self._pending[-OUTPUT_MAX:]

    def _flush_loop(self):
        while not self._stop.wait(OUTPUT_FLUSH):
            if not self.on_output:
                continue
            with self._pending_lock:
                pending, self._pending = self._pending, ""
            if pending:
                try:
                    self.on_output(pending)
                except Exception:
                    log.exception("terminal output callback failed")

    def _write_process_input(self, data):
        with self._write_lock:
            proc = self.proc
            if not self.alive or proc is None:
                return
            proc.write(data)

    def write(self, text):
        self._write_process_input(text)

    def close(self, force=True):
        with self._lock:
            proc = self.proc
            self._generation += 1
            self.alive = False
            self.proc = None
            self._stop.set()
            self._ready.set()
        if proc is not None:
            try:
                proc.close(force=force)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    # ------------------------------------------------------------------ screen and input
    def terminal_page(self, top=-1, left=0, rows=5, cols=21):
        """Return a cropped virtual-screen page.

        ``top=-1`` selects the bottom viewport (which includes the cursor when possible).
        Positive ``top`` is an absolute row in the scrollback plus visible screen.
        """
        rows = max(0, int(rows))
        cols = max(0, int(cols))
        left = max(0, int(left))
        with self._lock:
            screen = self._screen
            if screen is None:
                lines = [""] * rows
                total_rows = total_cols = 0
                cursor_row = cursor_col = 0
                revision = self.revision
                alive = False
            else:
                history_lines = [_line_text(line, screen.columns) for line in screen.history.top]
                visible = list(screen.display)
                all_lines = history_lines + visible
                total_rows = len(all_lines)
                total_cols = int(screen.columns)
                cursor_row = len(history_lines) + int(screen.cursor.y)
                cursor_col = int(screen.cursor.x)
                if top < 0:
                    start = max(0, min(cursor_row, total_rows - rows if rows else cursor_row))
                else:
                    start = max(0, int(top))
                lines = []
                for index in range(start, start + rows):
                    line = all_lines[index] if index < total_rows else ""
                    lines.append(line[left:left + cols].rstrip())
                revision = self.revision
                alive = self.alive
        return {
            "lines": lines,
            "top": max(0, start if screen is not None else int(top)),
            "left": left,
            "rows": rows,
            "cols": cols,
            "total_rows": total_rows,
            "total_cols": total_cols,
            "cursor_row": cursor_row,
            "cursor_col": cursor_col,
            "revision": revision,
            "alive": alive,
        }

    def write_key(self, key):
        if key not in KEY_BYTES:
            raise ValueError(f"unknown terminal key: {key}")
        self.write(KEY_BYTES[key])


ConPTYTerminal = Terminal


def _line_text(line, width):
    """Convert pyte's sparse character mapping or display string to plain text."""
    if isinstance(line, str):
        return line
    out = [" "] * width
    for index, char in line.items():
        if 0 <= index < width:
            out[index] = getattr(char, "data", str(char))
    return "".join(out)


def _plain_text(text, terminal):
    """Drop ANSI controls for the legacy O callback while preserving prompt/newlines."""
    out = []
    state = terminal._plain_state
    for char in text:
        if state == "normal":
            if char == "\x1b":
                state = "esc"
            elif char in "\x07\x00":
                continue
            elif char == "\t":
                out.append("    ")
            else:
                out.append(char)
        elif state == "esc":
            if char == "[":
                state = "csi"
            elif char == "]":
                state = "osc"
            else:
                state = "normal"
        elif state == "csi":
            if "@" <= char <= "~":
                state = "normal"
        elif state == "osc":
            if char == "\x07":
                state = "normal"
            elif char == "\x1b":
                state = "osc_esc"
        else:  # osc_esc
            state = "normal" if char == "\\" else "osc"
    terminal._plain_state = state
    return "".join(out)
