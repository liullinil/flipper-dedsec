"""Persistent interactive terminal for the remote CMD tab.

``Shell`` keeps the legacy callback API used by :mod:`uplink.app`, while ``Terminal`` owns
the Windows ConPTY and pyte virtual screen.  There are no sentinel commands or setup writes
queued into the child: an interactive program receives exactly the bytes the user sends.
"""

from __future__ import annotations

import logging
import os
import threading

from .common import utf8_text
from .terminal import KEY_BYTES, Terminal

log = logging.getLogger("uplink.shell")

LINE_CHARS = 120
PROMPT_MARK = "UPLINKPROMPT>"


class Shell:
    """A persistent ConPTY shell with the old run/write/cancel surface."""

    def __init__(self, kind="cmd", on_output=None, on_exit=None, on_cwd=None):
        self.kind = kind
        self.on_output = on_output or (lambda seq, text: None)
        self.on_exit = on_exit or (lambda seq, code: None)
        self.on_cwd = on_cwd or (lambda cwd: None)
        self.terminal = None
        self.proc = None  # compatibility alias; updated after ensure()
        self.reader = None
        self.lock = threading.RLock()
        self.seq = None
        self.started = 0.0
        self.lines = 0
        self.alive = False
        self.ready = threading.Event()
        self.interactive = True
        self._text_buffer = ""
        self._await_prompt = False
        self._prompt_seen = False
        self._initial_prompt_pending = False
        self._generation = 0

    # ------------------------------------------------------------------ process
    def _spawn(self):
        terminal = Terminal(
            self.kind,
            on_output=self._on_terminal_output,
            on_exit=self._on_terminal_exit,
        )
        self.terminal = terminal
        self._generation += 1
        self._initial_prompt_pending = True
        terminal.start()
        self.proc = terminal.proc
        self.alive = terminal.alive
        self._text_buffer = ""
        self._await_prompt = False
        self._prompt_seen = True  # Terminal.start waits for cmd's environment prompt.
        self.ready.set()
        self.on_cwd(utf8_text(os.path.expanduser("~"), 120))
        log.info("shell started (%s) (ConPTY)", self.kind)

    def ensure(self):
        with self.lock:
            if self.terminal is None or not self.terminal.alive:
                self._spawn()
            self.proc = self.terminal.proc if self.terminal else None
            self.alive = bool(self.terminal and self.terminal.alive)
        self.ready.wait(3.0)

    # ------------------------------------------------------------------ output
    def _on_terminal_output(self, text):
        """Consume coalesced printable output and detect our environment prompt."""
        with self.lock:
            self._text_buffer += text
            marker = PROMPT_MARK
            while marker in self._text_buffer:
                before, self._text_buffer = self._text_buffer.split(marker, 1)
                # Terminal.start waits for this prompt, but the coalescing callback may
                # deliver its bytes just after run() has assigned a sequence.  Consume
                # that first prompt as startup output so it cannot finish the first command.
                was_initial = self._initial_prompt_pending
                self._initial_prompt_pending = False
                if not was_initial:
                    self._emit_text(before)
                if was_initial:
                    continue
                if self._await_prompt and self.seq is not None:
                    seq = self.seq
                    self.seq = None
                    self._await_prompt = False
                    self._prompt_seen = True
                    # Without writing a status query into stdin, cmd's prompt can only
                    # provide a completion boundary; retain the stable success code.
                    self.on_exit(seq, 0)
                else:
                    self._prompt_seen = True
            if self.seq is not None:
                # Keep a possible split prompt suffix; emit everything else immediately.
                keep = max(0, len(marker) - 1)
                if len(self._text_buffer) > keep:
                    ready, self._text_buffer = self._text_buffer[:-keep], self._text_buffer[-keep:]
                    self._emit_text(ready)
            elif len(self._text_buffer) > 4096:
                self._text_buffer = self._text_buffer[-(len(marker) - 1):]

    def _emit_text(self, text):
        if not text or self.seq is None:
            return
        text = text.replace("\r", "\n")
        for line in text.split("\n"):
            line = line.strip()
            if not line or line == ">>":
                continue
            for chunk in _wrap(utf8_text(line, 4000), LINE_CHARS):
                self.lines += 1
                self.on_output(self.seq, chunk)

    def _on_terminal_exit(self, code):
        with self.lock:
            active = self.seq
            self.seq = None
            self._await_prompt = False
            self.alive = False
            self.proc = None
            self.ready.set()
        if active is not None:
            self.on_output(active, "...shell exited...")
            self.on_exit(active, code)

    # ------------------------------------------------------------------ commands
    def run(self, seq, command):
        command = command.strip()
        if not command:
            self.on_exit(seq, 0)
            return
        self.ensure()
        with self.lock:
            if self.seq is not None:
                self.on_output(seq, "busy: a command is already running (Back to stop)")
                return
            self.seq = seq
            self.lines = 0
            self._await_prompt = True
            self._prompt_seen = False
            terminal = self.terminal
        log.info("run seq=%s: %s", seq, command)
        try:
            terminal.write(command + "\r")
        except Exception as exc:
            log.warning("write failed: %s", exc)
            self.restart()
            self.on_exit(seq, -1)

    def write_input(self, seq, text):
        """Write text followed by Enter to the active terminal command."""
        with self.lock:
            if self.seq != seq or self.terminal is None or not self.terminal.alive:
                return False
            terminal = self.terminal
        try:
            terminal.write(text + "\r")
            return True
        except Exception as exc:
            log.warning("input write failed: %s", exc)
            return False

    def write_raw(self, seq, text):
        """Write raw terminal bytes represented as a Python string."""
        with self.lock:
            if self.seq != seq or self.terminal is None or not self.terminal.alive:
                return False
            terminal = self.terminal
        try:
            terminal.write(text)
            return True
        except Exception as exc:
            log.warning("raw input write failed: %s", exc)
            return False

    def write_key(self, seq, key):
        if key not in KEY_BYTES:
            raise ValueError(f"unknown terminal key: {key}")
        return self.write_raw(seq, KEY_BYTES[key])

    def poll_timeout(self):
        # A terminal command may legitimately run forever.  The old two-minute watchdog
        # would kill Codex while it is waiting for input, so this compatibility hook is now
        # intentionally a no-op.
        return None

    def cancel(self, seq):
        with self.lock:
            active = self.seq
            terminal = self.terminal
        if active is None or (seq and seq != active):
            return
        log.info("cancel seq=%s", active)
        if terminal is not None:
            try:
                terminal.write_key("CtrlC")
            except Exception:
                pass
        self.on_output(active, "...cancelled...")
        self.restart()
        self.on_exit(active, -1)

    def restart(self):
        with self.lock:
            terminal = self.terminal
            self.terminal = None
            self.proc = None
            self.alive = False
            self.seq = None
            self._await_prompt = False
        if terminal is not None:
            terminal.close(force=True)
        self.ensure()

    def stop(self):
        with self.lock:
            terminal = self.terminal
            self.terminal = None
            self.proc = None
            self.alive = False
            self.seq = None
            self._await_prompt = False
        if terminal is not None:
            terminal.close(force=True)

    def terminal_page(self, top=-1, left=0, rows=5, cols=21):
        with self.lock:
            terminal = self.terminal
        if terminal is None:
            return {"lines": [""] * max(0, rows), "top": 0, "left": left,
                    "rows": rows, "cols": cols, "total_rows": 0, "total_cols": 0,
                    "cursor_row": 0, "cursor_col": 0, "revision": 0, "alive": False}
        return terminal.terminal_page(top, left, rows, cols)


def _wrap(text, width):
    if not text:
        return [""]
    return [text[i:i + width] for i in range(0, len(text), width)]
