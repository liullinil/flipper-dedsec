"""Persistent remote shell for the Flipper CMD tab: one cmd.exe or PowerShell in a ConPTY.

Protocol (see README): C|seq|command -> run(), T|seq|text -> write_input(), K|seq -> cancel().
Output goes back as O|seq|line (UTF-8, at most 127 bytes per line, at most 400 lines per
command), the exit code as X|seq|code and the shell directory as W|cwd.

Commands are framed by the shell's own prompt, so nothing extra is written into the stdin of
a program that may be reading it:

* cmd.exe starts with PROMPT=UPLINKPROMPT[$P]$G, PowerShell with a prompt function printing
  UPLINKPROMPT[exit code|$PWD]> (worked out from $?, $LASTEXITCODE and $Error, see PS_SETUP).
  The command line is written as typed; its echo (the row with our prompt) is dropped, and the
  command has finished when the cursor rests right after a fresh prompt, which also gives the
  cwd for W.
* cmd.exe cannot show %ERRORLEVEL% in a prompt, so once it is back at its prompt (reading its
  own input again) one hidden query line, RC_QUERY, reads it and resets it to 0 before X is
  sent.

Only complete lines are sent. A line without a newline yet (``pause``, ``set /p``, a REPL
prompt) is sent once the output has been quiet for QUIET_FLUSH, or when the prompt returns.

Cancel (K) sends Ctrl+C; if the command is still running CANCEL_GRACE later, the whole process
tree is killed (Job object, see terminal.py) and the shell restarts in the same directory.
Ordinary commands get the same treatment after MAX_RUNTIME; interactive ones (a REPL, Codex,
anything the user typed input into) stay until K. All timers run on a supervisor thread, so
the BLE thread (cancel, write_input, poll_timeout) never waits for the shell.

SECURITY: this runs whatever the Flipper sends as a shell command on this PC. The BLE link has
no pairing, so anyone in Bluetooth range who knows the protocol could drive it. It is disabled
unless explicitly enabled (Feed/app gate it), every command is logged, and output is capped.
"""
import logging
import os
import re
import shlex
import threading
import time

from .common import LINE_BYTES, CWD_BYTES, utf8_chunks, utf8_cut, utf8_tail, utf8_text
from .terminal import KEY_BYTES, Terminal

log = logging.getLogger("uplink.shell")

MAX_OUTPUT_LINES = 400      # O lines per command; then one "[output truncated]", rest dropped
MAX_RUNTIME = 120.0         # seconds before an ordinary (non-interactive) command is stopped
CANCEL_GRACE = 2.0          # after Ctrl+C, seconds before the process tree is killed
QUIET_FLUSH = 0.3           # an unfinished line is sent after this much silence
QUERY_TIMEOUT = 3.0         # cmd's %ERRORLEVEL% answer must arrive within this
READY_TIMEOUT = 15.0        # first prompt of a new shell
LATE_WINDOW = 30.0          # a T/K for a command finished this recently is a race, not an error
TICK = 0.05                 # supervisor period while a command runs
IDLE_TICK = 1.0             # ... and while the shell is idle

PROMPT_TAG = "UPLINKPROMPT"
CMD_PROMPT = PROMPT_TAG + "[$P]$G"
# %ERRORLEVEL% is expanded when the line is read, then `(call )` resets it to 0: internal
# commands such as echo or cd leave it alone, so without the reset an `echo` after a failed
# command would report that command's code again.
RC_QUERY = "echo UPLINKRC=%ERRORLEVEL%= & (call )\r"
_PROMPT_RE = re.compile(re.escape(PROMPT_TAG) + r"\[([^>]*)\]>$")
_ECHO_RE = re.compile(re.escape(PROMPT_TAG) + r"\[[^>]*\]>")
_RC_RE = re.compile(r"UPLINKRC=(-?\d+)=")

# PowerShell: UTF-8 console, no PSReadLine (it repaints the input line), no progress bars
# (cursor art), and a prompt that reports the exit code and location of the last command:
# 1 for a parse error (it leaves $? as it was); 0 when $? is true; $LASTEXITCODE when a native
# program changed it; 1 for a new error record (a failed cmdlet leaves $LASTEXITCODE at an
# older program's value); else the unchanged $LASTEXITCODE (the same program failed again).
PS_SETUP = (
    "$e = New-Object System.Text.UTF8Encoding $false; "
    "[Console]::OutputEncoding = $e; [Console]::InputEncoding = $e; $OutputEncoding = $e; "
    "Remove-Module PSReadLine -ErrorAction SilentlyContinue; "
    "$ProgressPreference = 'SilentlyContinue'; "
    "function global:prompt { $ok = $?; $rc = $global:LASTEXITCODE; "
    "$err = if ($Error.Count) { $Error[0] } else { $null }; "
    "$new = -not [object]::ReferenceEquals($err, $global:UplinkErr); "
    "$code = if ($new -and ($err -is [Management.Automation.ParseException] -or "
    "('' + $err.CategoryInfo.Category) -eq 'ParserError')) { 1 } "
    "elseif ($ok) { 0 } elseif ($rc -and $rc -ne $global:UplinkRc) { $rc } "
    "elseif ($new) { 1 } elseif ($rc) { $rc } else { 1 }; "
    "$global:UplinkRc = $rc; $global:UplinkErr = $err; "
    "$d = if ($PWD.Provider.Name -eq 'FileSystem') { $PWD.ProviderPath } else { $PWD.Path }; "
    "'" + PROMPT_TAG + "[' + $code + '|' + $d + ']> ' }"
)

STARTING, RUNNING, QUERY = "starting", "running", "query"


def shell_command(kind):
    """argv and extra environment for the persistent shell of the given kind."""
    if kind == "powershell":
        return ["powershell.exe", "-NoLogo", "-NoProfile", "-NoExit", "-Command", PS_SETUP], {}
    # /d: no AutoRun, /q: echo off, /k: run chcp and stay interactive. UTF-8 code page, so
    # Cyrillic file names and `type` of UTF-8 files come through. The prompt comes from the
    # environment, so nothing has to be typed into the shell to set it up.
    return ["cmd.exe", "/d", "/q", "/k", "chcp 65001>nul"], {"PROMPT": CMD_PROMPT}


def _parse_prompt(kind, inner):
    """(cwd, exit code or None) from the text between UPLINKPROMPT[ and ]>. Only the
    PowerShell prompt carries the code; cmd's comes from the %ERRORLEVEL% query."""
    if kind == "powershell":
        code, sep, cwd = inner.partition("|")
        if sep:
            try:
                return cwd, int(code)
            except ValueError:
                return cwd, 1
    return inner, None


# ------------------------------------------------------------------ interactive commands
_ALWAYS_INTERACTIVE = {
    "codex", "claude", "ssh", "telnet", "ftp", "sftp", "diskpart", "edit", "vim", "vi", "nano",
    "less", "more", "top", "htop", "irb", "ghci", "sqlite3", "mysql", "psql", "mongosh",
    "redis-cli", "choice", "pause",
}
_BARE_INTERACTIVE = {"nslookup", "netsh", "node", "lua", "julia", "scala", "r", "wmic", "gdb"}
_REPLS = {"python", "python3", "py", "pythonw", "ipython", "bash", "sh", "zsh"}
_PS_RUN = {"-c", "-command", "-f", "-file", "-e", "-ec", "-encodedcommand", "/c", "/command"}


def looks_interactive(command):
    """Heuristic: will this command wait for the user (REPL, TUI, pager) rather than finish?"""
    text = command.strip()
    if "|" in text or "<" in text:
        return False                       # reads a pipe or a file, not the user
    low = text.lower()
    if low.startswith("set /p") or low.startswith("set/p"):
        return True
    try:
        words = shlex.split(text, posix=False)
    except ValueError:
        words = text.split()
    words = [w.strip('"\'') for w in words if w.strip('"\'')]
    if words and words[0].lower() in ("call", "start"):
        words = words[1:]
    if not words:
        return False
    name = os.path.basename(words[0].lstrip("@").replace("/", "\\")).lower()
    for ext in (".exe", ".cmd", ".bat", ".com", ".ps1"):
        if name.endswith(ext):
            name = name[:-len(ext)]
    args = [w.lower() for w in words[1:]]
    if name == "codex":
        return not args or args[0] not in ("exec", "e", "login", "logout", "apply", "mcp",
                                           "completion", "--version", "-v", "--help", "-h")
    if name == "claude":
        return not any(a in ("-p", "--print", "-v", "--version", "-h", "--help") for a in args) \
            and not (args and args[0] in ("update", "mcp", "config", "doctor", "install"))
    if name in _ALWAYS_INTERACTIVE:
        return True
    if name in _BARE_INTERACTIVE:
        return not args or "-i" in args or "--interactive" in args
    if name in _REPLS:
        if "-i" in args:
            return True
        return not any(a in ("-c", "-m", "-e", "--") or not a.startswith("-") for a in args)
    if name == "cmd":
        return not any(a.startswith("/c") for a in args)
    if name in ("powershell", "pwsh"):
        if "-noexit" in args:
            return True
        return not any(a in _PS_RUN or not a.startswith("-") for a in args)
    if name == "wsl":
        rest, skip = [], False
        for a in args:
            if skip:
                skip = False
            elif a in ("-d", "--distribution", "-u", "--user", "--cd"):
                skip = True
            elif a in ("-e", "--exec", "--"):
                return False
            else:
                rest.append(a)
        return not rest
    return False


class _Command:
    def __init__(self, seq, text):
        self.seq = seq
        self.text = text
        self.state = STARTING
        self.interactive = looks_interactive(text)
        self.start_line = None      # line id of the prompt the command was typed at
        self.started = 0.0
        self.last_output = 0.0
        self.lines = 0
        self.truncated = False
        self.flushed = (None, "")   # (line id, text) of an unfinished line already sent
        self.cancel = None          # "cancel" or "timeout"
        self.ctrl_c_at = None
        self.extra = set()          # other seqs waiting for this command's end (K/kill)
        self.pending = []           # T input that arrived while the shell was starting
        self.query_line = None
        self.query_at = 0.0
        self.code = None            # %ERRORLEVEL% answer (cmd)
        self.result = None          # forced exit code (-1 after cancel / timeout)
        self.note = None            # line sent before X ("[cancelled]")
        self.tui_base = 0


class Shell:
    """A persistent ConPTY shell with the run / write_input / cancel / stop surface of app.py."""

    terminal_factory = Terminal     # tests substitute a fake terminal
    clock = staticmethod(time.monotonic)

    def __init__(self, kind="cmd", on_output=None, on_exit=None, on_cwd=None):
        self.kind = kind if kind in ("cmd", "powershell") else "cmd"
        self.on_output = on_output or (lambda seq, text: None)
        self.on_exit = on_exit or (lambda seq, code: None)
        self.on_cwd = on_cwd or (lambda cwd: None)
        self.lock = threading.RLock()
        self._spawn_lock = threading.Lock()
        self._wake = threading.Event()
        self._terminal = None
        self._gen = 0
        self._ready = threading.Event()
        self._cmd = None
        self._cwd = None
        self._sent_cwd = None
        self._partial = (None, "")
        self._done = {}
        self._supervisor = None
        self._dead_since = None

    # ------------------------------------------------------------------ compatibility
    @property
    def terminal(self):
        return self._terminal

    @property
    def proc(self):
        term = self._terminal
        return term.proc if term is not None else None

    @property
    def alive(self):
        term = self._terminal
        return bool(term is not None and term.alive)

    @property
    def seq(self):
        cmd = self._cmd
        return cmd.seq if cmd is not None else None

    @property
    def cwd(self):
        return self._cwd

    # ------------------------------------------------------------------ public API
    def run(self, seq, command):
        """Start a command (worker thread). Returns once it has been written to the shell;
        output, W and X follow asynchronously. Blocks only while a new shell starts."""
        seq = str(seq)
        command = (command or "").replace("\r", " ").replace("\n", " ").strip()
        if not command:
            self.on_exit(seq, 0)
            return
        with self.lock:
            active = self._cmd
            if active is not None:
                self._busy(seq, command, active)
                return
            cmd = _Command(seq, command)
            self._cmd = cmd
            self._done.pop(seq, None)
            self._start_supervisor()
        log.info("run seq=%s: %s", seq, command)
        term = self._ensure(wait=True)
        with self.lock:
            if self._cmd is not cmd:
                return                      # cancelled or stopped while the shell started
            if term is None or term is not self._terminal:
                self._finish(cmd, -1, "[shell did not start]")
                return
            now = self.clock()
            cmd.state = RUNNING
            cmd.start_line = self._partial[0]
            cmd.started = cmd.last_output = now
            cmd.tui_base = term.tui_events
            pending, cmd.pending = cmd.pending, []
        written = term.write(command + "\r")
        for text in pending:
            written = written and term.write(text + "\r")
        if not written:
            with self.lock:
                if self._cmd is cmd:
                    self._finish(cmd, -1, "[shell is not running]")
        self._wake.set()

    def write_input(self, seq, text):
        """Send a line typed on the Flipper (T) to the running command. BLE thread: never waits."""
        seq = str(seq)
        with self.lock:
            cmd = self._cmd
            if cmd is None or cmd.seq != seq:
                self._unknown(seq, "[no command is running]")
                return False
            if cmd.cancel or cmd.state == QUERY:
                return False
            cmd.interactive = True          # the user is talking to it: no watchdog
            if cmd.state == STARTING:
                cmd.pending.append(text)
                return True
            term = self._terminal
        return bool(term is not None and term.write(text + "\r"))

    def write_raw(self, seq, text):
        """Write raw terminal bytes (keys, escape sequences) to the running command."""
        with self.lock:
            cmd = self._cmd
            if cmd is None or cmd.seq != str(seq) or cmd.state != RUNNING:
                return False
            cmd.interactive = True
            term = self._terminal
        return bool(term is not None and term.write(text))

    def write_key(self, seq, key):
        if key not in KEY_BYTES:
            raise ValueError(f"unknown terminal key: {key}")
        return self.write_raw(seq, KEY_BYTES[key])

    def cancel(self, seq):
        """K from the Flipper: cancel the running command whatever its seq. Returns at once;
        Ctrl+C, the kill and the restart happen on the supervisor thread."""
        seq = str(seq)
        with self.lock:
            cmd = self._cmd
            if cmd is None:
                self._unknown(seq, "[nothing to cancel]")
                return
            log.info("cancel seq=%s (running seq=%s)", seq, cmd.seq)
            if seq and seq != cmd.seq:
                cmd.extra.add(seq)          # the Flipper waits for an X with its own seq
            self._request_cancel(cmd, "cancel")
        self._wake.set()

    def poll_timeout(self):
        """Called by app.py every frame (BLE thread); the watchdog runs on the supervisor."""
        self._wake.set()

    def stop(self):
        """Stop the shell (shell kind changed, commands disabled, shutdown). A running command
        gets X|seq|-1 so the Flipper does not wait for it forever."""
        with self.lock:
            cmd = self._cmd
            if cmd is not None:
                self._finish(cmd, -1, "[shell stopped]")
            term = self._drop_terminal()
        if term is not None:
            term.close()
        self._wake.set()

    def restart(self):
        with self.lock:
            cmd = self._cmd
            if cmd is not None:
                self._finish(cmd, -1, "[shell restarted]")
            term = self._drop_terminal()
        if term is not None:
            term.close()
        self.ensure()

    def ensure(self):
        """Start the shell if needed and wait for its first prompt."""
        return self._ensure(wait=True) is not None

    def terminal_page(self, top=-1, left=0, rows=5, cols=21):
        term = self._terminal
        if term is None:
            return {"lines": [""] * max(0, rows), "top": 0, "left": left,
                    "rows": rows, "cols": cols, "total_rows": 0, "total_cols": 0,
                    "cursor_row": 0, "cursor_col": 0, "revision": 0, "alive": False}
        return term.terminal_page(top, left, rows, cols)

    # ------------------------------------------------------------------ shell process
    def _ensure(self, wait=True):
        """Return a running terminal (starting one if needed); with `wait`, after its first
        prompt. Never called with self.lock held; only the spawn itself is serialized."""
        with self._spawn_lock:
            with self.lock:
                term = self._terminal
                if term is not None and term.alive:
                    ready = self._ready
                    gen = None
                else:
                    term = None
                    self._gen += 1
                    gen = self._gen
                    self._ready.set()
                    self._ready = ready = threading.Event()
                    self._partial = (None, "")
                    self._dead_since = None
                    cwd = self._cwd
            if term is None:
                argv, extra_env = shell_command(self.kind)
                env = dict(os.environ)
                env.update(extra_env)
                term = self.terminal_factory(
                    self.kind,
                    on_output=lambda lines, partial, g=gen: self._on_output(g, lines, partial),
                    on_exit=lambda status, g=gen: self._on_exit(g, status),
                    argv=argv, env=env, cwd=cwd)
                try:
                    term.start()
                except Exception as exc:
                    log.warning("cannot start %s: %s", self.kind, exc)
                    with self.lock:
                        if self._gen == gen:
                            self._gen += 1
                            self._ready.set()
                    return None
                with self.lock:
                    stale = self._gen != gen
                    if not stale:
                        self._terminal = term
                        self._start_supervisor()
                if stale:                   # stop() ran while the shell was starting
                    term.close()
                    return None
                log.info("shell started (%s) in %s", self.kind, cwd or "~")
        if wait and not ready.wait(READY_TIMEOUT):
            log.warning("no prompt from %s after %.0f s", self.kind, READY_TIMEOUT)
        with self.lock:
            return term if self._terminal is term and term.alive else None

    def _drop_terminal(self):
        """Forget the current terminal (lock held); its late callbacks are ignored."""
        term = self._terminal
        self._terminal = None
        self._gen += 1
        self._ready.set()                   # wake anyone waiting for the old one
        self._ready = threading.Event()
        self._partial = (None, "")
        self._dead_since = None
        return term

    def _on_exit(self, gen, status):
        with self.lock:
            if gen != self._gen:
                return                      # killed on purpose; already handled
            self._drop_terminal()
            cmd = self._cmd
            if cmd is not None:
                self._finish(cmd, status, "[shell exited]")
        log.info("shell exited (%s)", status)

    # ------------------------------------------------------------------ output
    def _on_output(self, gen, lines, partial):
        """Reader-thread callback: complete lines and the line under the cursor."""
        actions = []
        with self.lock:
            if gen != self._gen:
                return
            now = self.clock()
            cmd = self._cmd
            for line_id, text, new in lines:
                self._line(cmd, line_id, text, new)
            self._partial = partial
            if cmd is not None:
                cmd.last_output = now
            self._prompt(cmd, partial, now, actions)
        for action in actions:
            action()

    def _line(self, cmd, line_id, text, new):
        if _ECHO_RE.search(text):
            return                          # a command typed at our prompt (or the bare prompt)
        if cmd is None or cmd.state == STARTING:
            return                          # banner / output between commands
        if cmd.state == QUERY:
            m = _RC_RE.search(text)
            if m:
                cmd.code = int(m.group(1))
            return
        fid, ftext = cmd.flushed
        if fid is not None and fid == line_id:
            new = text[len(ftext):] if text.startswith(ftext) else text
            cmd.flushed = (None, "")
        self._output(cmd, new)

    def _prompt(self, cmd, partial, now, actions):
        line_id, text = partial
        if PROMPT_TAG not in text:
            return
        m = _PROMPT_RE.search(text)
        if m is None:
            return
        cwd, code = _parse_prompt(self.kind, m.group(1))
        if not self._ready.is_set():
            self._ready.set()               # first prompt of this shell
            self._set_cwd(cwd, force=True)
            return
        if cmd is None or cmd.state == STARTING:
            self._set_cwd(cwd)
            return
        if cmd.start_line is not None and (line_id is None or line_id <= cmd.start_line):
            return                          # the prompt the command was typed at
        if cmd.state == RUNNING:
            prefix = text[:m.start()]
            fid, ftext = cmd.flushed
            if fid == line_id and prefix.startswith(ftext):
                prefix = prefix[len(ftext):]
            cmd.flushed = (None, "")
            self._output(cmd, prefix)       # output that ended without a newline
            self._set_cwd(cwd)
            if cmd.cancel:
                cmd.result = -1
                cmd.note = "[cancelled]" if cmd.cancel == "cancel" else None
            if code is not None:            # PowerShell: the prompt carries the exit code
                self._finish(cmd, code if cmd.result is None else cmd.result, cmd.note)
                return
            # cmd: read (and reset) %ERRORLEVEL% now that it reads its own input again
            cmd.state = QUERY
            cmd.query_line = line_id
            cmd.query_at = now
            term = self._terminal
            if term is not None:
                actions.append(lambda: term.write(RC_QUERY))
            return
        if cmd.state == QUERY and line_id is not None and line_id > cmd.query_line:
            if cmd.code is None:
                cmd.query_line = line_id    # a stray Enter made a prompt; the answer follows
                return
            self._set_cwd(cwd)
            self._finish(cmd, cmd.code if cmd.result is None else cmd.result, cmd.note)

    def _output(self, cmd, text):
        if cmd.truncated or not text:
            return
        clean = utf8_text(text)
        if not clean:
            return                          # blank lines: the tiny console has no room
        for chunk in utf8_chunks(clean, LINE_BYTES):
            if cmd.lines >= MAX_OUTPUT_LINES:
                cmd.truncated = True
                self.on_output(cmd.seq, "[output truncated]")
                return
            cmd.lines += 1
            self.on_output(cmd.seq, chunk)

    def _flush_partial(self, cmd):
        """Send the unfinished line under the cursor once (a prompt waiting for input)."""
        line_id, text = self._partial
        if line_id is None or not text.strip() or PROMPT_TAG in text:
            return
        if cmd.flushed[0] == line_id:
            return
        if cmd.start_line is not None and line_id <= cmd.start_line:
            return
        cmd.flushed = (line_id, text)
        self._output(cmd, text)

    def _set_cwd(self, cwd, force=False):
        cwd = (cwd or "").strip()
        if not cwd:
            return
        self._cwd = cwd
        if force or cwd != self._sent_cwd:
            self._sent_cwd = cwd
            self.on_cwd(utf8_tail(utf8_text(cwd), CWD_BYTES))

    # ------------------------------------------------------------------ command end
    def _finish(self, cmd, code, note=None):
        """Send the final lines of a command (lock held) and forget it."""
        if note:
            self.on_output(cmd.seq, note)
        self.on_exit(cmd.seq, code)
        for seq in sorted(cmd.extra):
            self.on_exit(seq, -1)
        now = self.clock()
        if self._cmd is cmd:
            self._cmd = None
        for seq in [cmd.seq] + list(cmd.extra):
            self._done[seq] = now
        if len(self._done) > 64:
            for seq, at in list(self._done.items()):
                if now - at > LATE_WINDOW:
                    del self._done[seq]
        log.info("seq=%s finished: %s", cmd.seq, code)

    def _busy(self, seq, text, active):
        """A C while another command runs: the Flipper lost track of it (for example after an
        app restart). Answer with X so it does not wait forever; 'kill' stops the old one."""
        if text.lower() == "kill":
            self.on_output(seq, utf8_text("[stopping: %s]" % active.text, LINE_BYTES))
            active.extra.add(seq)
            self._request_cancel(active, "cancel")
            self._wake.set()
            return
        shown = utf8_cut(utf8_text(active.text), 48)
        self.on_output(seq, utf8_text("busy: '%s' is still running; send kill to stop it" % shown,
                                      LINE_BYTES))
        self.on_exit(seq, -1)
        self._done[seq] = self.clock()

    def _unknown(self, seq, note):
        """T/K for a command that is not running: unless it has just finished (the X is on its
        way), answer X|seq|-1 so the Flipper leaves its RUN state."""
        now = self.clock()
        at = self._done.get(seq)
        if at is not None and now - at < LATE_WINDOW:
            return
        self.on_output(seq, note)
        self.on_exit(seq, -1)
        self._done[seq] = now

    def _request_cancel(self, cmd, reason):
        if cmd.state == STARTING:
            self._finish(cmd, -1, "[cancelled]")
        elif cmd.state == RUNNING and not cmd.cancel:
            cmd.cancel = reason
        # QUERY: the command has already finished; its X is moments away

    def _is_interactive(self, cmd, term):
        if cmd.interactive:
            return True
        return term is not None and term.tui_events != cmd.tui_base

    # ------------------------------------------------------------------ supervisor
    def _start_supervisor(self):
        if self._supervisor is None or not self._supervisor.is_alive():
            self._supervisor = threading.Thread(target=self._supervise, name="shell-supervisor",
                                                daemon=True)
            self._supervisor.start()

    def _supervise(self):
        while True:
            with self.lock:
                if self._terminal is None and self._cmd is None:
                    self._supervisor = None
                    return
                busy = self._cmd is not None
            self._wake.wait(TICK if busy else IDLE_TICK)
            self._wake.clear()
            with self.lock:
                actions = self._tick(self.clock())
            for action in actions:
                try:
                    action()
                except Exception:
                    log.exception("shell supervisor action failed")

    def _tick(self, now):
        """Timers (lock held): returns actions to run without the lock."""
        actions = []
        term = self._terminal
        if term is not None and term.alive and not term.process_alive():
            # the shell is gone but a background child keeps the console open
            if self._dead_since is None:
                self._dead_since = now
            elif now - self._dead_since > 1.0:
                cmd = self._cmd
                self._drop_terminal()
                if cmd is not None:
                    self._finish(cmd, -1, "[shell exited]")
                actions.append(term.close)
                return actions
        cmd = self._cmd
        if cmd is None or cmd.state == STARTING:
            return actions
        if cmd.state == QUERY:
            if now - cmd.query_at > QUERY_TIMEOUT:
                log.warning("no %%ERRORLEVEL%% answer for seq=%s", cmd.seq)
                code = cmd.result if cmd.result is not None else (cmd.code or 0)
                self._finish(cmd, code, cmd.note)
            return actions
        if not cmd.cancel and now - cmd.started >= MAX_RUNTIME \
                and not self._is_interactive(cmd, term):
            self.on_output(cmd.seq, "[timed out after %d min]" % (MAX_RUNTIME // 60))
            cmd.cancel = "timeout"
        if cmd.cancel:
            if cmd.ctrl_c_at is None:
                cmd.ctrl_c_at = now
                if term is not None:
                    actions.append(lambda: term.write(KEY_BYTES["CtrlC"]))
            elif now - cmd.ctrl_c_at >= CANCEL_GRACE:
                # Ctrl+C did not end it: kill the whole tree, restart in the same directory
                log.warning("seq=%s ignored Ctrl+C; killing the shell", cmd.seq)
                self._finish(cmd, -1, "[killed, shell restarted]")
                old = self._drop_terminal()
                if old is not None:
                    actions.append(old.close)
                actions.append(lambda: self._ensure(wait=False))
            return actions
        if now - cmd.last_output >= QUIET_FLUSH:
            self._flush_partial(cmd)
        return actions
