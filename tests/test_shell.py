"""Regression tests for the remote shell (uplink/uplink/shell.py).

The first part drives Shell with a fake terminal whose output goes through the real VT line
tracker, so framing, echo dropping, exit codes, caps and timers are tested without processes
and with a fake clock. The second part runs the real Shell against cmd.exe / PowerShell
(Windows only, every wait bounded).
"""

from __future__ import annotations

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "uplink"))

from uplink.shell import RC_QUERY, Shell, looks_interactive, shell_command  # noqa: E402
from uplink.terminal import _LineTracker  # noqa: E402
from uplink.common import CWD_BYTES, utf8_tail, utf8_text  # noqa: E402

PROMPT = "UPLINKPROMPT[C:\\Users\\me]>"
BANNER = "\x1b[?25l\x1b[2J\x1b[m\x1b[H\x1b]0;cmd.exe\x07\x1b[?25h\r\n" + PROMPT


# ---------------------------------------------------------------------------- fakes
class FakeTerminal:
    """Stands in for terminal.Terminal; output is fed through the real _LineTracker."""

    banner = BANNER
    start_in_thread = False
    instances = []

    def __init__(self, kind, *, on_output=None, on_exit=None, argv=None, env=None, cwd=None):
        self.kind = kind
        self.on_output = on_output
        self.on_exit = on_exit
        self.argv = argv
        self.env = env
        self.cwd = cwd
        self.tracker = _LineTracker(120, 24)
        self.writes = []
        self.write_hook = None
        self.alive = False
        self.closed = False
        self.proc = object()
        FakeTerminal.instances.append(self)

    @property
    def tui_events(self):
        return self.tracker.tui_events

    def start(self):
        self.alive = True
        if not self.banner:
            return
        banner = self.banner
        if self.cwd:                       # a restarted shell shows its directory
            banner = banner.replace("C:\\Users\\me", self.cwd)
        if self.start_in_thread:
            # like the old reader thread: hand the prompt over from another thread and only
            # then return from start(); a caller holding the shell lock would deadlock here
            t = threading.Thread(target=self.feed, args=(banner,))
            t.start()
            t.join(5)
        else:
            self.feed(banner)

    def feed(self, text):
        self.tracker.feed(text)
        self.on_output(self.tracker.take(), self.tracker.partial())

    def write(self, text):
        if self.write_hook:
            self.write_hook(text)
        self.writes.append(text)
        return self.alive

    def process_alive(self):
        return self.alive

    def close(self, force=True):
        self.alive = False
        self.closed = True

    def terminal_page(self, *args):
        return {}


class Recorder:
    def __init__(self):
        self.lines = []
        self.lock = threading.Lock()

    def out(self, seq, text):
        with self.lock:
            self.lines.append("O|%s|%s" % (seq, text))

    def exit(self, seq, code):
        with self.lock:
            self.lines.append("X|%s|%s" % (seq, code))

    def cwd(self, cwd):
        with self.lock:
            self.lines.append("W|%s" % cwd)

    def take(self):
        with self.lock:
            out, self.lines = self.lines, []
        return out


@pytest.fixture
def fresh():
    """A Shell on a FakeTerminal with a manual clock and no supervisor thread."""
    FakeTerminal.instances = []
    FakeTerminal.banner = BANNER
    FakeTerminal.start_in_thread = False
    rec = Recorder()
    sh = Shell("cmd", on_output=rec.out, on_exit=rec.exit, on_cwd=rec.cwd)
    sh.terminal_factory = FakeTerminal
    now = [1000.0]
    sh.clock = lambda: now[0]
    sh._start_supervisor = lambda: None

    def tick(dt=0.0):
        now[0] += dt
        with sh.lock:
            actions = sh._tick(now[0])
        for action in actions:
            action()

    sh.tick = tick
    sh.now = now
    sh.rec = rec
    yield sh
    sh.stop()


@pytest.fixture
def fake(fresh):
    """Like `fresh`, with the shell started and its startup W line consumed."""
    assert fresh.ensure()
    assert fresh.rec.take() == ["W|C:\\Users\\me"]
    return fresh


def term(sh):
    return FakeTerminal.instances[-1]


def start(sh, seq, command):
    """run() the command and return the terminal it was written to."""
    sh.run(seq, command)
    t = term(sh)
    assert t.writes[-1] == command.strip() + "\r"
    return t


def finish_cmd(t, rc, cwd="C:\\Users\\me"):
    """Answer cmd's %ERRORLEVEL% query and show the next prompt."""
    assert t.writes[-1] == RC_QUERY
    t.feed("echo UPLINKRC=%%ERRORLEVEL%%= & (call )\r\nUPLINKRC=%d= \r\n\r\nUPLINKPROMPT[%s]>"
           % (rc, cwd))


# ---------------------------------------------------------------------------- 1: no stalls
def test_shell_start_does_not_wait_while_holding_its_lock(fresh):
    """Defect 1: the prompt is delivered by another thread before start() returns."""
    FakeTerminal.start_in_thread = True
    t0 = time.monotonic()
    assert fresh.ensure()
    assert time.monotonic() - t0 < 1.0
    assert fresh.rec.take() == ["W|C:\\Users\\me"]      # W once at start


def test_cancel_returns_immediately_and_supervisor_does_the_work():
    """Defect 1: K must not block the BLE thread, even when the terminal write is slow."""
    FakeTerminal.instances = []
    FakeTerminal.banner = BANNER
    FakeTerminal.start_in_thread = False
    rec = Recorder()
    sh = Shell("cmd", on_output=rec.out, on_exit=rec.exit, on_cwd=rec.cwd)
    sh.terminal_factory = FakeTerminal
    try:
        t = start(sh, "4", "ping -t 127.0.0.1")
        t.write_hook = lambda text: time.sleep(0.5) if text == "\x03" else None
        t0 = time.monotonic()
        sh.cancel("4")
        sh.poll_timeout()
        assert time.monotonic() - t0 < 0.05
        deadline = time.monotonic() + 3
        while "\x03" not in t.writes and time.monotonic() < deadline:
            time.sleep(0.01)
        assert "\x03" in t.writes                       # Ctrl+C sent by the supervisor
        t.feed("ping -t 127.0.0.1\r\nControl-C\r\n^C\r\n\r\n" + PROMPT)
        deadline = time.monotonic() + 3
        while t.writes[-1] != RC_QUERY and time.monotonic() < deadline:
            time.sleep(0.01)
        finish_cmd(t, -1073741510)                      # ERRORLEVEL is reset, X says -1
        out = rec.take()
        assert out[-2:] == ["O|4|[cancelled]", "X|4|-1"]
    finally:
        sh.stop()


# ---------------------------------------------------------------------------- 2: no deadlock
def test_k_cancels_whatever_runs_and_answers_its_own_seq(fake):
    t = start(fake, "5", "ping -t 127.0.0.1")
    fake.rec.take()
    fake.cancel("1")                       # the Flipper restarted: its seq is 1, not 5
    fake.tick()
    assert t.writes[-1] == "\x03"
    t.feed("\r\n^C\r\n\r\n" + PROMPT)
    finish_cmd(t, -1073741510)
    assert fake.rec.take() == ["O|5|^C", "O|5|[cancelled]", "X|5|-1", "X|1|-1"]


def test_c_while_busy_gets_an_exit_and_busy_line(fake):
    start(fake, "5", "ping -t 127.0.0.1")
    fake.rec.take()
    fake.run("1", "dir")
    out = fake.rec.take()
    assert out[0].startswith("O|1|busy: 'ping -t 127.0.0.1' is still running")
    assert out[1] == "X|1|-1"
    assert fake.seq == "5"                 # the running command is untouched


def test_kill_while_busy_stops_the_old_command(fake):
    t = start(fake, "5", "codex")
    fake.rec.take()
    fake.run("1", "Kill")
    assert fake.rec.take() == ["O|1|[stopping: codex]"]
    fake.tick()
    assert t.writes[-1] == "\x03"
    fake.tick(2.5)                         # Ctrl+C ignored: kill and restart
    assert fake.rec.take() == ["O|5|[killed, shell restarted]", "X|5|-1", "X|1|-1",
                               "W|C:\\Users\\me"]           # the new shell's first prompt
    assert t.closed and FakeTerminal.instances[-1].alive


def test_input_or_cancel_for_an_unknown_seq_gets_an_exit(fake):
    fake.ensure()
    fake.rec.take()
    assert not fake.write_input("3", "hello")
    assert fake.rec.take() == ["O|3|[no command is running]", "X|3|-1"]
    fake.cancel("3")                       # answered already: no second X
    fake.cancel("8")
    assert fake.rec.take() == ["O|8|[nothing to cancel]", "X|8|-1"]


def test_late_input_for_a_finished_command_is_ignored(fake):
    t = start(fake, "2", "ver")
    t.feed("ver\r\n\r\nMicrosoft Windows\r\n\r\n" + PROMPT)
    finish_cmd(t, 0)
    fake.rec.take()
    assert not fake.write_input("2", "x")  # raced with the X: no extra X|2|-1
    fake.cancel("2")
    assert fake.rec.take() == []


def test_watchdog_stops_ordinary_commands_only(fake):
    t = start(fake, "6", "ping -n 500 127.0.0.1")
    fake.tick(119)
    assert "\x03" not in t.writes
    fake.tick(2)
    assert fake.rec.take()[-1] == "O|6|[timed out after 2 min]"
    assert t.writes[-1] == "\x03"
    t.feed("\r\n^C\r\n\r\n" + PROMPT)
    finish_cmd(t, -1073741510)
    assert fake.rec.take() == ["O|6|^C", "X|6|-1"]

    t = start(fake, "7", "python")         # interactive by name: stays until K
    first = len(t.writes)
    fake.tick(600)
    assert "\x03" not in t.writes[first:]
    fake.write_input("7", "exit()")
    t.feed("python\r\n>>> exit()\r\n\r\n" + PROMPT)
    finish_cmd(t, 0)
    assert fake.rec.take() == ["O|7|>>> exit()", "X|7|0"]

    t = start(fake, "8", "set /a 1+1 & pause")
    first = len(t.writes)
    fake.write_input("8", "x")             # the user answered it: interactive now
    fake.tick(600)
    assert "\x03" not in t.writes[first:]


def test_interactive_heuristic():
    assert looks_interactive("python")
    assert looks_interactive("python -i script.py")
    assert looks_interactive("Codex")
    assert looks_interactive("powershell")
    assert looks_interactive("cmd")
    assert looks_interactive("set /p NAME=Name? ")
    assert not looks_interactive("python script.py")
    assert not looks_interactive("python -c \"print(1)\"")
    assert not looks_interactive("codex exec \"fix it\"")
    assert not looks_interactive("cmd /c dir")
    assert not looks_interactive("powershell -Command Get-Date")
    assert not looks_interactive("ping -t 127.0.0.1")
    assert not looks_interactive("echo print(1) | python")


# ---------------------------------------------------------------------------- 3: output
def test_lines_split_across_reads_are_sent_whole(fake):
    t = start(fake, "1", "ping -n 2 127.0.0.1")
    t.feed("ping -n 2 127.0.0.1\r\n\r\nPinging 127.0.0.1 with 32 bytes of data:\r\n"
           "Reply from 127.0.0.1: bytes=32 time<1")
    t.feed("ms TTL=128\r\nR")
    t.feed("\x1b[?25leply from 127.0.0.1: bytes=32 time<1ms TTL=128\x1b[9;1HPing statistics")
    out = fake.rec.take()
    assert out == ["O|1|Pinging 127.0.0.1 with 32 bytes of data:",
                   "O|1|Reply from 127.0.0.1: bytes=32 time<1ms TTL=128",
                   "O|1|Reply from 127.0.0.1: bytes=32 time<1ms TTL=128"]
    t.feed(" for 127.0.0.1:\r\n\r\n" + PROMPT)
    assert fake.rec.take() == ["O|1|Ping statistics for 127.0.0.1:"]
    finish_cmd(t, 0)
    assert fake.rec.take() == ["X|1|0"]


def test_command_echo_is_not_sent_back(fake):
    t = start(fake, "1", "echo hi")
    t.feed("echo hi\r\nhi\r\n\r\n" + PROMPT)
    finish_cmd(t, 0)
    assert fake.rec.take() == ["O|1|hi", "X|1|0"]


def test_prompt_waiting_for_input_is_sent_after_quiet(fake):
    t = start(fake, "1", "pause")
    t.feed("pause\r\nPress any key to continue . . . ")
    fake.tick(0.1)
    assert fake.rec.take() == []
    fake.tick(0.25)
    assert fake.rec.take() == ["O|1|Press any key to continue . . ."]
    fake.tick(1)
    assert fake.rec.take() == []           # once per line
    fake.write_input("1", "x")
    t.feed("\r\n\r\n" + PROMPT)
    finish_cmd(t, 0)
    assert fake.rec.take() == ["X|1|0"]

    t = start(fake, "2", "set /p NAME=Name? ")
    t.feed("set /p NAME=Name? \r\nName? ")
    fake.tick(0.4)
    assert fake.rec.take() == ["O|2|Name?"]
    fake.write_input("2", "Вася")
    t.feed("Вася\r\n\r\n" + PROMPT)
    finish_cmd(t, 0)
    assert fake.rec.take() == ["O|2|Вася", "X|2|0"]  # only the answer, not "Name?" again


def test_output_without_newline_is_sent_when_the_prompt_returns(fake):
    t = start(fake, "1", "set /p =abc<nul")
    t.feed("set /p =abc<nul\r\nabc" + PROMPT)
    assert fake.rec.take() == ["O|1|abc"]


# ---------------------------------------------------------------------------- 4: exit code, cwd
def test_cmd_exit_code_comes_from_errorlevel_and_cwd_from_prompt(fresh):
    fake = fresh
    fake.ensure()
    assert fake.rec.take() == ["W|C:\\Users\\me"]           # W once at start
    t = start(fake, "6", "cmd /c exit 3")
    t.feed("cmd /c exit 3\r\n\r\n" + PROMPT)
    assert fake.rec.take() == []           # X waits for the %ERRORLEVEL% answer
    finish_cmd(t, 3)
    assert fake.rec.take() == ["X|6|3"]

    t = start(fake, "7", "cd ..")
    t.feed("cd ..\r\n\r\nUPLINKPROMPT[C:\\Users]>")
    finish_cmd(t, 0, cwd="C:\\Users")
    assert fake.rec.take() == ["W|C:\\Users", "X|7|0"]


def test_errorlevel_answer_after_a_stray_enter(fake):
    t = start(fake, "1", "pause")
    t.feed("pause\r\nPress any key to continue . . . \r\n\r\n" + PROMPT)
    assert t.writes[-1] == RC_QUERY
    # the Enter after the key the user sent reaches cmd first and prints one more prompt
    t.feed("\r\n" + PROMPT)
    assert fake.seq == "1"
    finish_cmd(t, 0)
    assert fake.rec.take()[-1] == "X|1|0"


def test_powershell_prompt_carries_exit_code_and_cwd():
    FakeTerminal.instances = []
    FakeTerminal.banner = "\x1b[2J\x1b[HUPLINKPROMPT[0|C:\\Users\\me]> "
    rec = Recorder()
    sh = Shell("powershell", on_output=rec.out, on_exit=rec.exit, on_cwd=rec.cwd)
    sh.terminal_factory = FakeTerminal
    sh._start_supervisor = lambda: None
    try:
        t = start(sh, "1", "cmd /c exit 3")
        t.feed("cmd /c exit 3\r\nUPLINKPROMPT[3|C:\\Users\\me]> ")
        t = start(sh, "2", "cd ..")
        t.feed("cd ..\r\nUPLINKPROMPT[0|C:\\Users]> ")
        assert rec.take() == ["W|C:\\Users\\me", "X|1|3", "W|C:\\Users", "X|2|0"]
        assert RC_QUERY not in t.writes
    finally:
        sh.stop()
        FakeTerminal.banner = BANNER


def test_powershell_startup_sets_utf8_and_prompt():
    argv, env = shell_command("powershell")
    setup = argv[-1]
    assert "[Console]::OutputEncoding = $e" in setup and "[Console]::InputEncoding = $e" in setup
    assert "UTF8Encoding" in setup and "function global:prompt" in setup
    assert "$global:LASTEXITCODE" in setup and "$?" in setup and "$PWD" in setup
    argv, env = shell_command("cmd")
    assert argv == ["cmd.exe", "/d", "/q", "/k", "chcp 65001>nul"]       # defect 6
    assert env == {"PROMPT": "UPLINKPROMPT[$P]$G"}


# ---------------------------------------------------------------------------- 5: cap
def test_output_is_capped_at_400_lines(fake):
    t = start(fake, "1", "dir /s /b C:\\Windows\\System32")
    t.feed("dir /s /b C:\\Windows\\System32\r\n")
    for i in range(0, 600, 50):
        t.feed("".join("C:\\Windows\\System32\\file%d.dll\r\n" % n for n in range(i, i + 50)))
    t.feed("\r\n" + PROMPT)
    finish_cmd(t, 0)
    out = fake.rec.take()
    assert len(out) == 402
    assert out[399] == "O|1|C:\\Windows\\System32\\file399.dll"
    assert out[400:] == ["O|1|[output truncated]", "X|1|0"]


# ---------------------------------------------------------------------------- 7: stop
def test_stop_ends_the_running_command(fake):
    t = start(fake, "9", "ping -t 127.0.0.1")
    fake.rec.take()
    fake.stop()
    assert fake.rec.take() == ["O|9|[shell stopped]", "X|9|-1"]
    assert t.closed and not fake.alive


# ---------------------------------------------------------------------------- 8: kill + cwd
def test_kill_after_ctrl_c_grace_restarts_in_the_same_directory(fake):
    t = start(fake, "1", "cd /d D:\\work")
    t.feed("cd /d D:\\work\r\n\r\nUPLINKPROMPT[D:\\work]>")
    finish_cmd(t, 0, cwd="D:\\work")
    fake.rec.take()
    t = start(fake, "2", "python ignore_ctrl_c.py")
    fake.cancel("2")
    fake.tick()
    assert t.writes[-1] == "\x03"
    fake.tick(1.0)
    assert not t.closed                    # still within the grace period
    fake.tick(1.5)
    assert t.closed
    assert fake.rec.take() == ["O|2|[killed, shell restarted]", "X|2|-1", "W|D:\\work"]
    new = FakeTerminal.instances[-1]
    assert new is not t and new.cwd == "D:\\work" and new.alive


# ---------------------------------------------------------------------------- 12: widths
def test_long_lines_are_wrapped_into_127_byte_lines(fake):
    text = "Привет мир " * 30            # 330 characters, 570 bytes
    t = start(fake, "1", "type ru.txt")
    t.feed("type ru.txt\r\n" + text + "\r\n\r\n" + PROMPT)
    out = [line.split("|", 2)[2] for line in fake.rec.take()]
    assert len(out) > 4
    assert all(len(line.encode("utf-8")) <= 127 for line in out)
    assert " ".join(out) == text.strip()


def test_cwd_is_capped_to_the_flipper_buffer_keeping_the_tail(fake):
    fake.ensure()
    fake.rec.take()
    deep = "C:\\Users\\me\\Документы\\очень длинная папка\\проект"
    t = start(fake, "1", "cd x")
    t.feed("cd x\r\n\r\nUPLINKPROMPT[%s]>" % deep)
    finish_cmd(t, 0, cwd=deep)
    w = fake.rec.take()[0]
    assert w.startswith("W|...") and w.endswith("\\проект")
    assert len(w[2:].encode("utf-8")) <= 47


# ---------------------------------------------------------------------------- real shells
needs_windows = pytest.mark.skipif(os.name != "nt", reason="ConPTY is Windows-only")


def wline(path):
    """The W line for a directory: capped to the Flipper's 47-byte buffer, tail kept."""
    return "W|" + utf8_tail(utf8_text(str(path)), CWD_BYTES)


class Live:
    """A real Shell with a recorder and bounded waits."""

    def __init__(self, kind="cmd"):
        self.rec = Recorder()
        self.shell = Shell(kind, on_output=self._out, on_exit=self._exit, on_cwd=self._cwd)

    def _out(self, seq, text):
        self.rec.out(seq, text)

    def _exit(self, seq, code):
        self.rec.exit(seq, code)

    def _cwd(self, cwd):
        self.rec.cwd(cwd)

    def run(self, seq, command, timeout=10):
        self.shell.run(seq, command)
        return self.wait(seq, timeout)

    def wait(self, seq, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.rec.lock:
                done = any(line.startswith("X|%s|" % seq) for line in self.rec.lines)
            if done:
                return self.rec.take()
            time.sleep(0.02)
        raise AssertionError("no X|%s within %ss: %r" % (seq, timeout, self.rec.take()))

    def wait_for(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.rec.lock:
                if any(predicate(line) for line in self.rec.lines):
                    return True
            time.sleep(0.02)
        return False


@needs_windows
def test_real_cmd_session(tmp_path):
    folder = tmp_path / "Папка"
    folder.mkdir()
    (folder / "привет.txt").write_text("строка в UTF-8\n", encoding="utf-8")
    live = Live("cmd")
    try:
        t0 = time.monotonic()
        assert live.shell.ensure()
        assert time.monotonic() - t0 < 3.0                        # defect 1: no 3 s stall
        assert live.rec.take()[0].startswith("W|")

        assert live.run("1", "echo привет") == ["O|1|привет", "X|1|0"]
        assert live.run("2", "cmd /c exit 3") == ["X|2|3"]          # defect 4
        assert live.run("3", 'cd /d "%s"' % folder) == [wline(folder), "X|3|0"]
        assert live.run("4", "chcp") == ["O|4|Active code page: 65001", "X|4|0"]  # defect 6
        assert live.run("5", "type привет.txt") == ["O|5|строка в UTF-8", "X|5|0"]
        assert live.run("6", "dir /b") == ["O|6|привет.txt", "X|6|0"]
        assert live.run("7", "set UPLINK_TEST=42") == ["X|7|0"]
        assert live.run("8", "echo %UPLINK_TEST%") == ["O|8|42", "X|8|0"]
        assert live.run("9", "cd ..") == [wline(tmp_path), "X|9|0"]

        live.shell.run("10", "set /p NAME=Name? ")
        assert live.wait_for(lambda line: line == "O|10|Name?")  # defect 3: visible prompt
        live.shell.write_input("10", "Вася")
        assert live.wait("10") == ["O|10|Name?", "O|10|Вася", "X|10|0"]
        assert live.run("11", "echo %NAME%") == ["O|11|Вася", "X|11|0"]

        out = live.run("12", "for /l %i in (1,1,450) do @echo line %i", timeout=30)
        assert len(out) == 402 and out[-2:] == ["O|12|[output truncated]", "X|12|0"]  # defect 5
    finally:
        live.shell.stop()


@needs_windows
def test_real_cancel_stops_ping_with_ctrl_c():
    live = Live("cmd")
    try:
        live.shell.run("1", "ping -t 127.0.0.1")
        assert live.wait_for(lambda line: line.startswith("O|1|Reply from"))
        t0 = time.monotonic()
        live.shell.cancel("1")
        assert time.monotonic() - t0 < 0.05                        # defect 1
        out = live.wait("1", timeout=5)
        assert out[-2:] == ["O|1|[cancelled]", "X|1|-1"]
        assert live.run("2", "echo still here") == ["O|2|still here", "X|2|0"]
    finally:
        live.shell.stop()


@needs_windows
def test_real_cancel_kills_the_tree_and_keeps_cwd(tmp_path):
    psutil = pytest.importorskip("psutil")
    live = Live("cmd")
    try:
        live.run("1", 'cd /d "%s"' % tmp_path)
        script = ("import os, signal, time; signal.signal(signal.SIGINT, signal.SIG_IGN); "
                  "print('pid', os.getpid(), flush=True); time.sleep(60)")
        live.shell.run("2", '"%s" -c "%s"' % (sys.executable, script))
        assert live.wait_for(lambda line: line.startswith("O|2|pid "))
        with live.rec.lock:
            pid = int([l for l in live.rec.lines if l.startswith("O|2|pid ")][0].split()[-1])
        old = live.shell.terminal
        tree = [old.pid] + list(old.host_pids) + [pid]
        live.shell.cancel("2")
        out = live.wait("2", timeout=6)
        assert out[-2:] == ["O|2|[killed, shell restarted]", "X|2|-1"]  # defect 8
        deadline = time.monotonic() + 3
        while any(psutil.pid_exists(p) for p in tree) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not [p for p in tree if psutil.pid_exists(p)]   # child and conhost are gone
        out = [l for l in live.run("3", "cd") if not l.startswith("W|")]
        assert out == ["O|3|%s" % tmp_path, "X|3|0"]                # same directory
    finally:
        live.shell.stop()


@needs_windows
def test_real_interactive_input_and_stop():
    live = Live("cmd")
    try:
        live.shell.run("1", "python -i -q")
        assert live.wait_for(lambda line: line == "O|1|>>>")
        assert live.shell.write_input("1", "print('привет', 6 * 7)")
        assert live.wait_for(lambda line: line == "O|1|привет 42")
        live.shell.stop()                                          # defect 7
        out = live.rec.take()
        assert out[-2:] == ["O|1|[shell stopped]", "X|1|-1"]
        assert not live.shell.alive
    finally:
        live.shell.stop()


@needs_windows
def test_real_powershell_session(tmp_path):
    live = Live("powershell")
    try:
        assert live.shell.ensure()
        live.rec.take()
        assert live.run("1", "cmd /c exit 3") == ["X|1|3"]          # defect 4
        assert live.run("2", "echo $LASTEXITCODE") == ["O|2|3", "X|2|0"]
        assert live.run("3", "Get-Item C:\\no-such-uplink-path")[-1] == "X|3|1"
        assert live.run("4", "echo привет") == ["O|4|привет", "X|4|0"]
        assert live.run("5", "[Console]::OutputEncoding.WebName") == ["O|5|utf-8", "X|5|0"]
        assert live.run("6", "cd '%s'" % tmp_path) == [wline(tmp_path), "X|6|0"]
    finally:
        live.shell.stop()
