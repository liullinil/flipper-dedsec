"""Persistent shell for the remote-cmd mode: one cmd.exe/PowerShell keeps cwd and env.

Each command is wrapped with a unique sentinel echo so we can tell where its output ends and
read its exit code. Output lines are pushed to a callback as they arrive; a KILL restarts the
shell. ASCII-only, since the Flipper fonts can't show anything else.

SECURITY: this runs whatever the Flipper sends as a shell command on this PC. The BLE link has
no pairing, so anyone in Bluetooth range who knows the protocol could drive it. It is disabled
unless explicitly enabled (Feed/app gate it), every command is logged, and output is capped.
"""
import logging
import os
import subprocess
import threading
import time
import uuid

from .common import utf8_text

log = logging.getLogger("uplink.shell")

MAX_OUTPUT_LINES = 400       # per command, then we stop forwarding (process keeps running)
MAX_RUNTIME = 120.0          # seconds before we auto-kill a command
LINE_CHARS = 120
PROMPT_ARG = "UPLINKPROMPT$G"   # cmd `prompt` argument; $G renders as ">"
PROMPT_MARK = "UPLINKPROMPT>"   # what that prompt prints; filtered from output


class Shell:
    def __init__(self, kind="cmd", on_output=None, on_exit=None, on_cwd=None):
        self.kind = kind
        self.on_output = on_output or (lambda seq, text: None)
        self.on_exit = on_exit or (lambda seq, code: None)
        self.on_cwd = on_cwd or (lambda cwd: None)
        self.proc = None
        self.reader = None
        self.lock = threading.Lock()
        self.seq = None
        self.sentinel = ""
        self.started = 0.0
        self.lines = 0
        self.alive = False
        self.ready = threading.Event()

    # ------------------------------------------------------------------ process
    def _spawn(self):
        if self.kind == "powershell":
            argv = ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "-"]
        else:
            # marker prompt (filtered out below) so the ">" never glues onto real output;
            # the sentinel echo is sent as its own line so %ERRORLEVEL% is the command's own
            argv = ["cmd.exe", "/q", "/k", "prompt " + PROMPT_ARG + "$_"]
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=os.path.expanduser("~"),
            bufsize=1,
            universal_newlines=True,
            encoding="utf-8",
            errors="replace",
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
        self.alive = True
        self.ready.clear()
        self.reader = threading.Thread(target=self._read_loop, daemon=True)
        self.reader.start()
        # prime: swallow the shell banner up to our sentinel before any real command
        self.sentinel = "UPLINKxxPRIMExx"
        self.seq = None
        if self.kind == "powershell":
            self.proc.stdin.write("Write-Output \"%s 0 $($PWD.Path)\"\n" % self.sentinel)
        else:
            # UTF-8 code page, so Cyrillic file names and messages reach the Flipper intact
            self.proc.stdin.write("chcp 65001>nul\r\necho %s 0 %%CD%%\r\n" % self.sentinel)
        self.proc.stdin.flush()
        log.info("shell started (%s) pid %s", self.kind, self.proc.pid)

    def ensure(self):
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                self._spawn()
        self.ready.wait(3.0)

    def _read_loop(self):
        proc = self.proc
        try:
            for raw in proc.stdout:
                line = raw.rstrip("\r\n")
                if line == PROMPT_MARK:
                    continue  # our own prompt line, never shown
                with self.lock:
                    seq, sentinel = self.seq, self.sentinel
                if sentinel and sentinel in line:
                    rest = line.split(sentinel, 1)[1].strip().split(None, 1)
                    try:
                        code = int(rest[0]) if rest else 0
                    except ValueError:
                        code = 0
                    cwd = rest[1].strip() if len(rest) > 1 else ""
                    with self.lock:
                        was_seq = self.seq
                        self.seq = None
                        self.sentinel = ""
                    if cwd:
                        self.on_cwd(utf8_text(cwd, 120))
                    if was_seq is None:
                        self.ready.set()  # prime sentinel: banner consumed, ready for commands
                    else:
                        self.on_exit(was_seq, code)
                    continue
                if seq is None:
                    continue  # banner / idle output between commands
                if not line.strip():
                    continue  # collapse blank lines (the tiny console has no room for them)
                with self.lock:
                    self.lines += 1
                    if self.lines > MAX_OUTPUT_LINES:
                        if self.lines == MAX_OUTPUT_LINES + 1:
                            self.on_output(seq, "...output truncated...")
                        continue
                for chunk in _wrap(utf8_text(line, 4000), LINE_CHARS):
                    self.on_output(seq, chunk)
        except Exception as exc:  # pipe closed on kill/restart
            log.debug("reader stopped: %s", exc)
        self.alive = False

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
            self.sentinel = "UPLINKxx%sxx" % uuid.uuid4().hex[:12]
            self.seq = seq
            self.started = time.time()
            self.lines = 0
            sentinel = self.sentinel
        log.info("run seq=%s: %s", seq, command)
        if self.kind == "powershell":
            wrapped = "%s\nWrite-Output \"%s $LASTEXITCODE $($PWD.Path)\"\n" % (command, sentinel)
        else:
            # separate line for the sentinel -> %ERRORLEVEL% is THIS command's code
            wrapped = "%s\r\necho %s %%ERRORLEVEL%% %%CD%%\r\n" % (command, sentinel)
        try:
            self.proc.stdin.write(wrapped)
            self.proc.stdin.flush()
        except Exception as exc:
            log.warning("write failed: %s", exc)
            self.restart()
            self.on_exit(seq, -1)

    def poll_timeout(self):
        with self.lock:
            running = self.seq is not None
            started = self.started
            seq = self.seq
        if running and time.time() - started > MAX_RUNTIME:
            log.warning("command seq=%s timed out, killing shell", seq)
            self.on_output(seq, "...timed out, shell restarted...")
            self.restart()
            self.on_exit(seq, -1)

    def cancel(self, seq):
        with self.lock:
            active = self.seq
        if active is not None:
            log.info("cancel seq=%s", seq)
            self.restart()
            self.on_output(active, "...cancelled...")
            self.on_exit(active, -1)

    def restart(self):
        with self.lock:
            self.seq = None
            self.sentinel = ""
            proc = self.proc
            self.proc = None
        if proc:
            try:
                proc.kill()
            except Exception:
                pass
        self.ensure()

    def stop(self):
        with self.lock:
            proc = self.proc
            self.proc = None
            self.alive = False
        if proc:
            try:
                proc.kill()
            except Exception:
                pass


def _wrap(text, width):
    if not text:
        return [""]
    return [text[i:i + width] for i in range(0, len(text), width)]
