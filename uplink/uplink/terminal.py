"""ConPTY terminal used by the DedSec Uplink remote shell.

The companion keeps one pseudo-terminal alive, so cmd.exe/PowerShell keep their cwd and
environment and interactive programs (python -i, codex) see a real console.

ConPTY does not pass a program's output through: it renders its console buffer as VT text,
so the stream contains cursor positioning instead of some line breaks, rows repainted after a
scroll and writes split at arbitrary points. Two consumers read it:

* ``_LineTracker`` is a small, fast interpreter for that VT subset. It keeps a few rows of
  screen, joins soft-wrapped rows and reports *complete logical lines* plus the line the
  cursor is on. That is what the Flipper's line console needs. It also answers cursor
  position / device attribute queries, which full-screen programs wait for before drawing.
* a pyte screen backs ``terminal_page()``. pyte is slow (~80k chars/s, too slow for a big
  ``dir /s``), so it is fed lazily from a bounded backlog only when a page is requested.

Every process of the terminal (the shell, whatever it starts and the ConPTY host) is put in a
Windows Job object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: ``close()`` kills the whole tree,
including children that ignore Ctrl+C, and nothing outlives the companion.
"""

from __future__ import annotations

import collections
import logging
import os
import re
import threading
from typing import Callable, Optional

try:  # pywinpty is Windows-only; importing the companion on another host should still work.
    from winpty import PtyProcess
except ImportError:  # pragma: no cover - exercised on non-Windows development hosts
    PtyProcess = None

try:
    import pyte
except ImportError:  # pragma: no cover - terminal_page() then returns blank pages
    pyte = None

try:
    import psutil
except ImportError:  # pragma: no cover - ConPTY hosts are then not put in the job
    psutil = None

try:
    from wcwidth import wcwidth as _wcwidth  # installed with pyte
except ImportError:  # pragma: no cover
    def _wcwidth(_char):
        return 1

log = logging.getLogger("uplink.terminal")

DEFAULT_COLS = 120      # wide enough that ordinary output lines are not soft-wrapped
DEFAULT_ROWS = 24
DEFAULT_HISTORY = 1000
READ_SIZE = 65536
BACKLOG_MAX = 256 * 1024  # raw characters kept for the lazily fed pyte screen
_KEEP_ROWS = 64           # rows kept above the emitted ones, for soft-wrap joins


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


# ---------------------------------------------------------------------------- line tracker
_TOKEN = re.compile(
    r"([^\x00-\x1f\x7f-\x9f]+)"                        # 1: printable text
    r"|\x1b\[([0-?]*)[ -/]*([@-~])"                     # 2, 3: CSI parameters, final byte
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"               # OSC (window title) ... BEL or ST
    r"|\x1b[P^_X][^\x1b]*\x1b\\"                         # DCS / PM / APC / SOS ... ST
    r"|\x1b([ -/]*[0-9:;<=>?@A-OQ-WYZ\\`a-~])"          # 4: other escape sequences
    r"|([\x00-\x1f\x7f-\x9f])"                          # 5: one control character
)
# an escape sequence cut off by the end of a chunk; it is completed by the next chunk
_INCOMPLETE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*|\][^\x07\x1b]*\x1b?|[P^_X][^\x1b]*\x1b?|[ -/]*)\Z")


class _LineTracker:
    """Turns ConPTY output into logical lines.

    Rows are addressed by absolute index (``top`` is the first screen row and only grows; a
    clear-screen starts a fresh set of rows), so a row index identifies a line for its lifetime.
    ``take()`` returns the lines completed since the last call as ``(line_id, text, new)``:
    ``line_id`` is the absolute row where the logical line starts, ``text`` the whole line and
    ``new`` the part that was not reported before (a line soft-wrapped across a repaint can be
    reported in two parts). ``partial()`` is ``(line_id, text)`` of the line under the cursor.
    """

    def __init__(self, cols=DEFAULT_COLS, rows=DEFAULT_ROWS):
        self.cols = max(2, int(cols))
        self.nrows = max(2, int(rows))
        self.rows = {}            # absolute row -> list of cells (one character each)
        self.wrapped = set()      # absolute rows that soft-wrapped into the next row
        self.top = 0              # absolute index of the first screen row
        self.row = 0              # cursor (absolute row, column); col == cols: pending wrap
        self.col = 0
        self.emit = 0             # first absolute row not reported yet
        self.margins = None       # scrolling region (top, bottom) in screen rows, if set
        self.autowrap = True
        self.saved = (0, 0)
        self.tui_events = 0       # alternate-screen switches seen (full-screen programs)
        self._carry = ""
        self._out = []
        self.replies = []

    # ------------------------------------------------------------------ input
    def feed(self, data):
        data = self._carry + data
        self._carry = ""
        for m in _TOKEN.finditer(data):
            text = m.group(1)
            if text is not None:
                self._draw(text)
                continue
            final = m.group(3)
            if final is not None:
                self._csi(m.group(2), final)
                continue
            esc = m.group(4)
            if esc is not None:
                self._esc(esc)
                continue
            ctl = m.group(5)
            if ctl is None:
                continue                          # OSC / DCS: nothing to show
            if ctl == "\x1b":
                rest = data[m.start():]
                if _INCOMPLETE.match(rest):
                    self._carry = rest if len(rest) < 4096 else ""
                    break
                continue                          # stray ESC
            self._control(ctl)
        self._collect()

    def take(self):
        out, self._out = self._out, []
        return out

    def take_replies(self):
        out, self.replies = self.replies, []
        return out

    def partial(self):
        start = self._logical_start(self.row)
        return start, self._join(start, self.row)

    # ------------------------------------------------------------------ rows
    def _line(self, r):
        row = self.rows.get(r)
        if row is None:
            row = self.rows[r] = [" "] * self.cols
        return row

    def _logical_start(self, r):
        steps = 0
        while (r - 1) in self.wrapped and steps < _KEEP_ROWS:
            r -= 1
            steps += 1
        return r

    def _join(self, a, b):
        parts = []
        for r in range(a, b + 1):
            row = self.rows.get(r)
            if row is not None:
                parts.append("".join(row))
        return "".join(parts).rstrip()

    def _commit(self, end):
        """Report the logical lines in rows [emit, end) as complete."""
        r = self.emit
        while r < end:
            start = self._logical_start(r)
            last = r
            while last in self.wrapped and last + 1 < end:
                last += 1
            text = self._join(start, last)
            new = text if start == r else self._join(r, last)
            self._out.append((start, text, new))
            r = last + 1
        self.emit = max(self.emit, end)

    def _collect(self):
        start = self._logical_start(self.row)
        if start > self.emit:
            self._commit(start)
        if len(self.rows) > 4 * self.nrows + _KEEP_ROWS:
            low = min(self.emit, self.top) - _KEEP_ROWS
            for r in [r for r in self.rows if r < low]:
                del self.rows[r]
            self.wrapped = {r for r in self.wrapped if r >= low}

    # ------------------------------------------------------------------ text
    def _draw(self, text):
        cols = self.cols
        if text.isascii() and self.autowrap:
            i, n = 0, len(text)
            while i < n:
                if self.col >= cols:
                    self._wrap()
                take = min(n - i, cols - self.col)
                self._line(self.row)[self.col:self.col + take] = text[i:i + take]
                self.col += take
                i += take
            return
        for ch in text:
            o = ord(ch)
            width = 1 if (o < 0x300 or 0x370 <= o < 0x1100) else _wcwidth(ch)
            if width < 0:
                continue
            if width == 0:                        # combining mark: join the previous cell
                if 0 < self.col <= cols:
                    row = self._line(self.row)
                    row[self.col - 1] += ch
                continue
            if self.col + width > cols:
                if self.autowrap:
                    self._wrap()
                else:
                    self.col = cols - width
            row = self._line(self.row)
            row[self.col] = ch
            if width == 2 and self.col + 1 < cols:
                row[self.col + 1] = ""
            self.col += width

    def _wrap(self):
        self.wrapped.add(self.row)
        self.col = 0
        self._index()

    # ------------------------------------------------------------------ movement
    def _bottom(self):
        return self.top + self.nrows - 1

    def _index(self):
        if self.margins is not None:
            m_top, m_bottom = self.top + self.margins[0], self.top + self.margins[1]
            if self.row == m_bottom:
                self._shift_up(m_top, m_bottom, 1)
                return
            if self.row < self._bottom():
                self.row += 1
            return
        if self.row >= self._bottom():
            self.top += 1
        self.row += 1

    def _reverse_index(self):
        m_top, m_bottom = (self.top, self._bottom())
        if self.margins is not None:
            m_top, m_bottom = self.top + self.margins[0], self.top + self.margins[1]
        if self.row == m_top:
            self._shift_down(m_top, m_bottom, 1)
        elif self.row > self.top:
            self.row -= 1

    def _shift_up(self, a, b, n):
        """Scroll rows a..b up by n inside a region (full-screen programs only)."""
        for r in range(a, b + 1):
            src = r + n
            self.rows[r] = self.rows.get(src, [" "] * self.cols)[:] if src <= b else [" "] * self.cols
            self.wrapped.discard(r)

    def _shift_down(self, a, b, n):
        for r in range(b, a - 1, -1):
            src = r - n
            self.rows[r] = self.rows.get(src, [" "] * self.cols)[:] if src >= a else [" "] * self.cols
            self.wrapped.discard(r)

    def _clear(self):
        """Erase the whole display: what is on it is final, a fresh set of rows starts."""
        self._commit(self.row + 1)
        new_top = max(self.top + self.nrows, self.row + 1)
        self.row = new_top + (self.row - self.top)
        self.top = new_top
        self.emit = new_top

    def _control(self, ch):
        if ch == "\r":
            self.col = 0
        elif ch in "\n\x0b\x0c":
            self._index()
        elif ch == "\b":
            self.col = max(0, min(self.col, self.cols - 1) - 1)
        elif ch == "\t":
            self.col = min(self.cols - 1, (self.col // 8 + 1) * 8)
        # BEL, NUL, SO/SI and C1 controls do not change the text

    def _esc(self, seq):
        if seq == "7":
            self.saved = (self.row - self.top, self.col)
        elif seq == "8":
            self.row = self.top + min(self.saved[0], self.nrows - 1)
            self.col = min(self.saved[1], self.cols)
        elif seq == "D":
            self._index()
        elif seq == "E":
            self.col = 0
            self._index()
        elif seq == "M":
            self._reverse_index()
        elif seq == "c":
            self.margins = None
            self.autowrap = True
            self._clear()
            self.row, self.col = self.top, 0

    def _csi(self, params, final):
        private = ""
        if params[:1] in ("?", ">", "<", "="):
            private, params = params[0], params[1:]
        args = [int(p) if p.isdigit() else 0 for p in params.split(";")] if params else []
        n = args[0] if args else 0
        n1 = max(1, n)
        cols = self.cols
        if private:
            if private == "?" and final in "hl":
                on = final == "h"
                for mode in args:
                    if mode in (47, 1047, 1049):
                        self.tui_events += 1
                    elif mode == 7:
                        self.autowrap = on
            return
        if final == "A":
            self.row = max(self.top, self.row - n1)
            self.col = min(self.col, cols - 1)
        elif final in "Be":
            self.row = min(self._bottom(), self.row + n1)
            self.col = min(self.col, cols - 1)
        elif final in "Ca":
            self.col = min(cols - 1, self.col + n1)
        elif final == "D":
            self.col = max(0, min(self.col, cols - 1) - n1)
        elif final == "E":
            self.row = min(self._bottom(), self.row + n1)
            self.col = 0
        elif final == "F":
            self.row = max(self.top, self.row - n1)
            self.col = 0
        elif final in "G`":
            self.col = min(cols, n1) - 1
        elif final in "Hf":
            r = max(1, args[0]) if args else 1
            c = max(1, args[1]) if len(args) > 1 else 1
            self.row = self.top + min(self.nrows, r) - 1
            self.col = min(cols, c) - 1
        elif final == "d":
            self.row = self.top + min(self.nrows, n1) - 1
        elif final == "J":
            if n in (2, 3):
                self._clear()
            elif n == 1:
                for r in range(self.top, self.row):
                    self.rows.pop(r, None)
                self._line(self.row)[:min(self.col, cols - 1) + 1] = [" "] * (min(self.col, cols - 1) + 1)
            else:
                row = self._line(self.row)
                start = min(self.col, cols)
                row[start:] = [" "] * (cols - start)
                for r in range(self.row + 1, self._bottom() + 1):
                    self.rows.pop(r, None)
                    self.wrapped.discard(r)
        elif final == "K":
            row = self._line(self.row)
            col = min(self.col, cols)
            if n == 1:
                row[:min(col, cols - 1) + 1] = [" "] * (min(col, cols - 1) + 1)
            else:
                if n == 2:
                    row[:] = [" "] * cols
                else:
                    row[col:] = [" "] * (cols - col)
                self.wrapped.discard(self.row)
        elif final == "X":
            row = self._line(self.row)
            col = min(self.col, cols - 1)
            end = min(cols, col + n1)
            row[col:end] = [" "] * (end - col)
        elif final == "P":
            row = self._line(self.row)
            col = min(self.col, cols - 1)
            del row[col:col + n1]
            row.extend([" "] * (cols - len(row)))
        elif final == "@":
            row = self._line(self.row)
            col = min(self.col, cols - 1)
            row[col:col] = [" "] * n1
            del row[cols:]
        elif final == "L":
            self._shift_down(self.row, self._bottom(), n1)
        elif final == "M":
            self._shift_up(self.row, self._bottom(), n1)
        elif final == "S":
            if self.margins is None:
                self.top += n1
                self.row += n1
            else:
                self._shift_up(self.top + self.margins[0], self.top + self.margins[1], n1)
        elif final == "T":
            a, b = self.top, self._bottom()
            if self.margins is not None:
                a, b = self.top + self.margins[0], self.top + self.margins[1]
            self._shift_down(a, b, n1)
        elif final == "r":
            first = (args[0] if args and args[0] else 1) - 1
            last = (args[1] if len(args) > 1 and args[1] else self.nrows) - 1
            if first <= 0 and last >= self.nrows - 1 or first >= last:
                self.margins = None
            else:
                self.margins = (max(0, first), min(self.nrows - 1, last))
            self.row, self.col = self.top, 0
        elif final == "n":
            if n == 6:
                self.replies.append("\x1b[%d;%dR" % (self.row - self.top + 1, min(self.col, cols - 1) + 1))
            elif n == 5:
                self.replies.append("\x1b[0n")
        elif final == "c":
            if n == 0:
                self.replies.append("\x1b[?6c")
        elif final == "s" and not args:
            self.saved = (self.row - self.top, self.col)
        elif final == "u":
            self.row = self.top + min(self.saved[0], self.nrows - 1)
            self.col = min(self.saved[1], cols)
        # SGR colours, window ops and unknown sequences do not change the text


# ---------------------------------------------------------------------------- pyte screen
class _Screen(pyte.HistoryScreen if pyte else object):
    """HistoryScreen that can send terminal query replies back through ConPTY."""

    def __init__(self, columns, lines, history, write_input):
        if pyte is None:  # pragma: no cover - guarded by Terminal._page_screen
            return
        super().__init__(columns, lines, history=history)
        self._write_input = write_input

    def write_process_input(self, data):
        try:
            self._write_input(data)
        except Exception:
            log.debug("terminal query reply failed", exc_info=True)


# ---------------------------------------------------------------------------- job object
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_kernel32 = None


def _win32():
    """Lazily bound kernel32 functions with explicit signatures (64-bit HANDLEs)."""
    global _kernel32
    if _kernel32 is None:
        import ctypes
        from ctypes import wintypes

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateJobObjectW.restype = wintypes.HANDLE
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k.SetInformationJobObject.restype = wintypes.BOOL
        k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                              wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.AssignProcessToJobObject.restype = wintypes.BOOL
        k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k.TerminateJobObject.restype = wintypes.BOOL
        k.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k.CloseHandle.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC_LIMIT(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class EXTENDED_LIMIT(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC_LIMIT),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        k.EXTENDED_LIMIT = EXTENDED_LIMIT
        k.ctypes = ctypes
        _kernel32 = k
    return _kernel32


class _Job:
    """A Job object that kills every process in it (and their descendants) when terminated or
    when its handle is closed, including on a crash of the companion."""

    def __init__(self):
        self.handle = None
        if os.name != "nt":
            return
        try:
            k = _win32()
            handle = k.CreateJobObjectW(None, None)
            if not handle:
                return
            info = k.EXTENDED_LIMIT()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k.SetInformationJobObject(handle, _JOB_EXTENDED_LIMIT_INFORMATION,
                                             k.ctypes.byref(info), k.ctypes.sizeof(info)):
                k.CloseHandle(handle)
                return
            self.handle = handle
        except Exception:
            log.debug("job object unavailable", exc_info=True)

    def assign(self, pid):
        if not self.handle or not pid:
            return False
        k = _win32()
        process = k.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, int(pid))
        if not process:
            return False
        try:
            return bool(k.AssignProcessToJobObject(self.handle, process))
        finally:
            k.CloseHandle(process)

    def terminate(self, code=1):
        if self.handle:
            _win32().TerminateJobObject(self.handle, code)

    def close(self):
        handle, self.handle = self.handle, None
        if handle:
            _win32().CloseHandle(handle)


_SPAWN_LOCK = threading.Lock()   # one spawn at a time, so a new ConPTY host can be attributed


def _allow_ctrl_c():
    """Children inherit the "ignore Ctrl+C" attribute from the process that creates them. A
    companion started from a launcher that set it would give the shell (and every command in
    it) a deaf ear to the Ctrl+C sent on cancel, so restore normal Ctrl+C processing first."""
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False)
    except Exception:
        log.debug("SetConsoleCtrlHandler failed", exc_info=True)


def _console_hosts():
    """PIDs of ConPTY host processes (conhost/OpenConsole) that are children of this process."""
    if psutil is None:
        return set()
    out = set()
    try:
        children = psutil.Process().children()
    except Exception:
        return out
    for child in children:
        try:
            if child.name().lower() in ("conhost.exe", "openconsole.exe"):
                out.add(child.pid)
        except Exception:
            continue
    return out


def _kill_tree(pid, extra=()):
    """Fallback when there is no job object: kill a process, its descendants and `extra` PIDs."""
    if psutil is None:
        return
    victims = []
    try:
        root = psutil.Process(pid)
        victims = root.children(recursive=True) + [root]
    except Exception:
        pass
    for extra_pid in extra:
        try:
            victims.append(psutil.Process(extra_pid))
        except Exception:
            pass
    for proc in victims:
        try:
            proc.kill()
        except Exception:
            pass


def _dispose(proc, job):
    """Release pywinpty handles (its close() sleeps) and the job, off the caller's thread."""
    if proc is not None:
        try:
            proc.close(force=True)
        except Exception:
            log.debug("pty close failed", exc_info=True)
    if job is not None:
        job.close()


# ---------------------------------------------------------------------------- terminal
class Terminal:
    """A ConPTY process (cmd.exe or PowerShell), its line tracker and a lazy pyte screen.

    ``on_output(lines, partial)`` is called by the reader thread, without any lock held, after
    each chunk of output: ``lines`` is a list of ``(line_id, text, new)`` completed logical lines
    and ``partial`` is ``(line_id, text)`` for the line under the cursor (see _LineTracker).
    ``on_exit(status)`` is called once when the shell process ends by itself; it is not called
    after ``close()``.
    """

    def __init__(
        self,
        kind: str = "cmd",
        *,
        on_output: Optional[Callable] = None,
        on_exit: Optional[Callable[[int], None]] = None,
        dimensions: tuple = (DEFAULT_ROWS, DEFAULT_COLS),
        history: int = DEFAULT_HISTORY,
        argv: Optional[list] = None,
        env: Optional[dict] = None,
        cwd: Optional[str] = None,
    ):
        self.kind = kind
        self.on_output = on_output
        self.on_exit = on_exit
        self.rows, self.cols = dimensions
        self.history_size = history
        self.argv = argv
        self.env = env
        self.cwd = cwd
        self.proc = None
        self.pid = None
        self.host_pids = set()
        self.reader = None
        self.alive = False
        self.revision = 0
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._stop = threading.Event()
        self._generation = 0
        self._job = None
        self._tracker = _LineTracker(self.cols, self.rows)
        self._screen = None
        self._stream = None
        self._backlog = collections.deque()
        self._backlog_len = 0
        self._backlog_reset = False

    # ------------------------------------------------------------------ process lifecycle
    def start(self):
        """Spawn the shell and start reading. Returns at once; it does not wait for a prompt."""
        with self._lock:
            if self.alive and self.proc is not None:
                return
            if PtyProcess is None:
                raise RuntimeError("pywinpty is required for the interactive Windows shell")
            argv = list(self.argv or ["powershell.exe" if self.kind == "powershell" else "cmd.exe"])
            env = dict(self.env if self.env is not None else os.environ)
            env.setdefault("TERM", "xterm-256color")
            env.setdefault("COLORTERM", "truecolor")
            cwd = self.cwd if self.cwd and os.path.isdir(self.cwd) else os.path.expanduser("~")
            self._tracker = _LineTracker(self.cols, self.rows)
            self._backlog.clear()
            self._backlog_len = 0
            self._backlog_reset = True
        with _SPAWN_LOCK:
            _allow_ctrl_c()
            before = _console_hosts()
            proc = PtyProcess.spawn(argv, cwd=cwd, env=env, dimensions=(self.rows, self.cols))
            hosts = _console_hosts() - before
        job = _Job()
        if job.handle:
            if not job.assign(proc.pid):
                log.warning("cannot put pid %s in a job object; falling back to tree kill", proc.pid)
                job.close()
                job = None
            else:
                for pid in hosts:
                    job.assign(pid)
        else:
            job = None
        with self._lock:
            self._generation += 1
            generation = self._generation
            self.proc = proc
            self.pid = proc.pid
            self.host_pids = hosts
            self._job = job
            self.alive = True
            self._stop.clear()
            self.revision += 1
            self.reader = threading.Thread(
                target=self._read_loop, args=(proc, generation),
                name="terminal-reader", daemon=True)
            self.reader.start()
        log.info("terminal started: %s pid %s in %s (job %s)", argv[0], proc.pid, cwd,
                 "yes" if job else "no")

    def _read_loop(self, proc, generation):
        last_partial = None
        try:
            while True:
                try:
                    raw = proc.read(READ_SIZE)
                except (EOFError, OSError, ValueError):
                    break
                if generation != self._generation:
                    break
                if not raw:
                    continue
                lines, partial, replies = self._feed(raw)
                for reply in replies:
                    self.write(reply)
                if self.on_output and (lines or partial != last_partial):
                    last_partial = partial
                    try:
                        self.on_output(lines, partial)
                    except Exception:
                        log.exception("terminal output callback failed")
        except Exception:
            log.exception("terminal reader failed")
        finally:
            natural = False
            with self._lock:
                if generation == self._generation:
                    natural = True
                    self.alive = False
            if natural:
                try:
                    status = proc.exitstatus
                except Exception:
                    status = None
                if self.on_exit:
                    try:
                        self.on_exit(-1 if status is None else int(status))
                    except Exception:
                        log.exception("terminal exit callback failed")
                self.close()   # orphaned children and the ConPTY host go with the job

    def _feed(self, raw):
        """Process one chunk of output; returns (completed lines, partial line, query replies)."""
        text = raw if isinstance(raw, str) else bytes(raw).decode("utf-8", "replace")
        with self._lock:
            self.revision += 1
            self._backlog.append(text)
            self._backlog_len += len(text)
            while self._backlog_len > BACKLOG_MAX and len(self._backlog) > 1:
                self._backlog_len -= len(self._backlog.popleft())
                self._backlog_reset = True
            tracker = self._tracker
            tracker.feed(text)
            return tracker.take(), tracker.partial(), tracker.take_replies()

    def write(self, text):
        """Send text to the terminal input; False when the terminal is gone."""
        with self._write_lock:
            proc = self.proc
            if not self.alive or proc is None:
                return False
            try:
                proc.write(text)
                return True
            except Exception as exc:
                log.debug("terminal write failed: %s", exc)
                return False

    def write_key(self, key):
        if key not in KEY_BYTES:
            raise ValueError(f"unknown terminal key: {key}")
        return self.write(KEY_BYTES[key])

    def process_alive(self):
        """Is the shell process itself still running (independent of the output pipe)?"""
        proc = self.proc
        if proc is None:
            return False
        try:
            return bool(proc.pty.isalive())   # proc.isalive() has side effects on close()
        except Exception:
            return False

    @property
    def tui_events(self):
        return self._tracker.tui_events

    def current_line(self):
        """(line_id, text) of the line under the cursor."""
        with self._lock:
            return self._tracker.partial()

    def close(self, force=True):
        """Kill the whole process tree now; handles are released in the background."""
        with self._lock:
            proc, job = self.proc, self._job
            self._generation += 1
            self.alive = False
            self.proc = None
            self._job = None
            self._stop.set()
        if job is not None:
            try:
                job.terminate()
            except Exception:
                log.debug("job terminate failed", exc_info=True)
        elif proc is not None:
            _kill_tree(proc.pid, self.host_pids)
        if proc is not None or job is not None:
            threading.Thread(target=_dispose, args=(proc, job), name="terminal-dispose",
                             daemon=True).start()

    # ------------------------------------------------------------------ screen
    def _page_screen(self):
        """Bring the pyte screen up to date with the output backlog (lock held)."""
        if pyte is None:
            return None
        if self._screen is None or self._stream is None:
            self._screen = _Screen(self.cols, self.rows, self.history_size, lambda _data: None)
            self._stream = pyte.Stream(self._screen)
            self._backlog_reset = False
        if self._backlog:
            if self._backlog_reset:
                self._screen.reset()
                self._backlog_reset = False
            data = "".join(self._backlog)
            self._backlog.clear()
            self._backlog_len = 0
            try:
                self._stream.feed(data)
            except Exception:
                log.debug("pyte parser error", exc_info=True)
        return self._screen

    def terminal_page(self, top=-1, left=0, rows=5, cols=21):
        """Return a cropped virtual-screen page.

        ``top=-1`` selects the bottom viewport (which includes the cursor when possible).
        Positive ``top`` is an absolute row in the scrollback plus visible screen.
        """
        rows = max(0, int(rows))
        cols = max(0, int(cols))
        left = max(0, int(left))
        start = 0
        with self._lock:
            screen = self._page_screen()
            if screen is None:
                lines = [""] * rows
                total_rows = total_cols = 0
                cursor_row = cursor_col = 0
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
            "top": start if screen is not None else max(0, int(top)),
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
