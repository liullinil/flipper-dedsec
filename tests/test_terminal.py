"""Focused tests for the ConPTY terminal adapter.

The integration test starts only the child owned by the test and always closes it in a
``finally`` block.  It is skipped on non-Windows development machines where ConPTY is not
available.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "uplink"))

from uplink.shell import Shell  # noqa: E402
from uplink.terminal import KEY_BYTES, Terminal, _Screen  # noqa: E402


def test_key_bytes_cover_terminal_controls():
    assert KEY_BYTES["Enter"] == "\r"
    assert KEY_BYTES["Tab"] == "\t"
    assert KEY_BYTES["Up"] == "\x1b[A"
    assert KEY_BYTES["PageDown"] == "\x1b[6~"
    assert KEY_BYTES["CtrlC"] == "\x03"


def test_ansi_screen_snapshot_and_cursor():
    terminal = Terminal()
    terminal._screen = _Screen(80, 24, 1000, lambda _data: None)
    terminal._stream = __import__("pyte").Stream(terminal._screen)
    terminal.alive = True
    terminal._feed("hello\x1b[2J\x1b[Hworld\x1b[2;3H!")
    page = terminal.terminal_page(top=0, left=0, rows=3, cols=10)
    assert page["lines"][:2] == ["world", "  !"]
    assert page["cursor_row"] == 1
    assert page["cursor_col"] == 3
    assert page["revision"] > 0


def test_pyte_query_replies_are_written_back():
    replies = []
    terminal = Terminal()
    terminal._screen = _Screen(80, 24, 1000, replies.append)
    terminal._stream = __import__("pyte").Stream(terminal._screen)
    terminal._stream.feed("\x1b[6n")
    terminal._stream.feed("\x1b[c")
    assert "\x1b[1;1R" in replies
    assert "\x1b[?6c" in replies


@pytest.mark.skipif(os.name != "nt", reason="ConPTY is Windows-only")
def test_real_tty_accepts_input_and_cleans_up():
    output = []
    exited = []
    shell = Shell(
        on_output=lambda _seq, text: output.append(text),
        on_exit=lambda _seq, code: exited.append(code),
    )
    try:
        shell.run("test", "python -i -q")
        time.sleep(0.7)
        assert shell.write_input("test", "print(7)")
        deadline = time.time() + 3
        while time.time() < deadline and "7" not in shell.terminal_page(0, 0, 24, 80)["lines"]:
            time.sleep(0.05)
        assert "7" in shell.terminal_page(0, 0, 24, 80)["lines"]
        assert not exited
    finally:
        shell.stop()
    assert not shell.alive

