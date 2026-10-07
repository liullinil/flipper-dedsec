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
from uplink.terminal import KEY_BYTES, Terminal, _LineTracker, _Screen  # noqa: E402


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


# ---------------------------------------------------------------------------- line tracker
# The chunks below are ConPTY output captured from cmd.exe (window titles shortened).
def _feed(tracker, *chunks):
    lines = []
    for chunk in chunks:
        tracker.feed(chunk)
        lines += tracker.take()
    return [text for _id, text, _new in lines if text], tracker.partial()[1]


def test_tracker_joins_a_line_split_across_reads_and_cursor_moves():
    tracker = _LineTracker(80, 24)
    lines, partial = _feed(
        tracker,
        "\x1b[?9001h\x1b[?1004h\x1b[?25l\x1b[2J\x1b[m\x1b[H\x1b]0;cmd.exe - chcp\x07\x1b[?25h"
        "\r\nUPLINKPROMPT[C:\\Users]>",
        "ping -n 2 127.0.0.1\r\n",
        "\x1b]0;cmd.exe - ping\x07\r\nPinging 127.0.0.1 with 32 bytes of data:\r\n"
        "Reply from 127.0.0.1: bytes=32 time<1ms TTL=128\r\n",
        "R",
        "\x1b[?25leply from 127.0.0.1: bytes=32 time<1ms TTL=128\x1b[9;1HPing statistics\r\n"
        "    Packets: Sent = 2\x1b[12;1HUPLINKPROMPT[C:\\Users]>\x1b]0;cmd.exe\x07\x1b[?25h")
    assert lines == ["UPLINKPROMPT[C:\\Users]>ping -n 2 127.0.0.1",
                     "Pinging 127.0.0.1 with 32 bytes of data:",
                     "Reply from 127.0.0.1: bytes=32 time<1ms TTL=128",
                     "Reply from 127.0.0.1: bytes=32 time<1ms TTL=128",
                     "Ping statistics",
                     "    Packets: Sent = 2"]
    assert partial == "UPLINKPROMPT[C:\\Users]>"


def test_tracker_joins_soft_wrapped_rows_including_conpty_repaint():
    tracker = _LineTracker(80, 24)
    tracker.feed("\x1b[24;1H")                      # at the bottom: every newline scrolls
    tracker.take()
    lines, _ = _feed(tracker, "a" * 80 + "\x1b]0;t\x07\r\n\x1b[23;80H" + "a" * 41 + "\r\n")
    assert lines == ["a" * 120]                     # ConPTY re-prints the wrapped cell
    lines, _ = _feed(tracker, "b" * 100 + "\r\n")
    assert lines == ["b" * 100]


def test_tracker_carries_an_escape_sequence_cut_by_a_read():
    tracker = _LineTracker(80, 24)
    lines, partial = _feed(tracker, "one\x1b[", "2;1Htwo\x1b]0;tit", "le\x07\r\n")
    assert lines == ["one", "two"]
    assert partial == ""


def test_tracker_keeps_the_last_state_of_an_overwritten_line():
    tracker = _LineTracker(80, 24)
    lines, partial = _feed(tracker, "progress 10%\rprogress 55%", "\rprogress 100%\x1b[K\r\n")
    assert lines == ["progress 100%"]
    lines, partial = _feed(tracker, "Name? ")
    assert lines == [] and partial == "Name?"


def test_tracker_commits_output_before_a_clear_screen():
    tracker = _LineTracker(80, 24)
    lines, partial = _feed(tracker, "before\r\n\x1b[2J\x1b[Hafter\r\n")
    assert lines == ["before", "after"]


def test_tracker_answers_cursor_and_attribute_queries():
    tracker = _LineTracker(80, 24)
    tracker.feed("\x1b[3;5Hx\x1b[6n\x1b[c\x1b[?1049h")
    assert tracker.take_replies() == ["\x1b[3;6R", "\x1b[?6c"]
    assert tracker.tui_events == 1


def test_tracker_is_fast_enough_for_big_outputs():
    tracker = _LineTracker(120, 24)
    data = "".join("C:\\Windows\\System32\\drivers\\file%05d.sys\r\n" % i for i in range(20000))
    t0 = time.perf_counter()
    for i in range(0, len(data), 4096):
        tracker.feed(data[i:i + 4096])
        tracker.take()
    assert time.perf_counter() - t0 < 2.0           # pyte needs ~10 s for this
