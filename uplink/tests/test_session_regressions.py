"""Regression tests for the session list sent to the Flipper (codex.py, claude.py, common.py)."""
import json
import os
import time
from datetime import datetime, timezone

from uplink import codex
from uplink.claude import ClaudeSession, ClaudeWatcher
from uplink.codex import CodexWatcher, HistoryWatcher, Thread
from uplink.common import (DETAIL_BYTES, NAME_BYTES, WORKING, YOUR_TURN, IDLE, utf8_chunks,
                           utf8_tail, utf8_text)

INTERIM = "Обычный конвертер Word недоступен. Соберу PDF другим способом."
FINAL = "Готово - PDF на 2 страницы, файл лежит рядом с документом."


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def _rollout(home, name, lines, day_ts=None):
    day = time.localtime(day_ts if day_ts is not None else time.time())
    folder = os.path.join(home, "sessions", "%04d" % day.tm_year, "%02d" % day.tm_mon,
                          "%02d" % day.tm_mday)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    with open(path, "w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    return path


def _meta(tid, ts, parent=None, agent="", nick=""):
    payload = {"id": tid, "cwd": "C:\\work\\" + tid}
    if parent:
        payload["source"] = {"subagent": {"thread_spawn": {
            "parent_thread_id": parent, "agent_path": "agents/" + agent, "agent_nickname": nick}}}
    return {"timestamp": _iso(ts), "type": "session_meta", "payload": payload}


def _event(ts, **payload):
    return {"timestamp": _iso(ts), "type": "event_msg", "payload": payload}


def _message(ts, text):
    return {"timestamp": _iso(ts), "type": "response_item", "payload": {
        "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}}


# ---------------------------------------------------------------------------- 9: detail
def test_codex_detail_is_the_final_message_and_body_keeps_everything(tmp_path):
    now = time.time()
    _rollout(str(tmp_path), "rollout-a.jsonl", [
        _meta("root-a", now - 60),
        _event(now - 50, type="task_started", turn_id="t1"),
        _message(now - 40, INTERIM),
        _message(now - 20, FINAL),
        _event(now - 10, type="task_complete", last_agent_message=FINAL),
    ])
    row, = CodexWatcher(home=str(tmp_path)).poll()
    assert row.state == YOUR_TURN
    assert row.detail == FINAL
    assert INTERIM in row.body and FINAL in row.body


def test_codex_detail_uses_last_message_without_last_agent_message():
    thread = Thread("rollout.jsonl")
    thread.feed([
        {"type": "session_meta", "payload": {"id": "id"}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t1"}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                              "content": [{"type": "output_text", "text": INTERIM}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                              "content": [{"type": "output_text", "text": "Готово -"},
                                                          {"type": "output_text", "text": "две страницы."}]}},
        {"type": "event_msg", "payload": {"type": "task_complete"}},
    ])
    assert thread.final_message == "Готово -\n\nдве страницы."
    body, detail = CodexWatcher._texts(thread, YOUR_TURN, [])
    assert INTERIM in body and detail == thread.final_message


def _claude_lines(now):
    return [
        {"type": "user", "timestamp": _iso(now - 60), "message": {"content": "Сделай PDF из отчёта"}},
        {"type": "assistant", "timestamp": _iso(now - 50), "message": {
            "id": "m1", "content": [{"type": "text", "text": INTERIM}]}},
        {"type": "assistant", "timestamp": _iso(now - 49), "message": {
            "id": "m1", "content": [{"type": "tool_use", "id": "u1", "name": "Bash",
                                     "input": {"command": "soffice --convert-to pdf"}}]}},
        {"type": "user", "timestamp": _iso(now - 30), "message": {"content": [
            {"type": "tool_result", "tool_use_id": "u1", "content": "ok"}]}},
        {"type": "assistant", "timestamp": _iso(now - 20), "message": {
            "id": "m2", "content": [{"type": "text", "text": "## Готово"}]}},
        {"type": "assistant", "timestamp": _iso(now - 19), "message": {
            "id": "m2", "content": [{"type": "text", "text": "- PDF на 2 страницы."}],
            "stop_reason": "end_turn"}},
    ]


def test_claude_detail_is_the_last_message_and_body_keeps_everything(tmp_path):
    now = time.time()
    project = tmp_path / "projects" / "C--work"
    project.mkdir(parents=True)
    with open(project / "s1.jsonl", "w", encoding="utf-8") as fh:
        for line in _claude_lines(now):
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    watcher = ClaudeWatcher(home=str(tmp_path))
    watcher.hooks.latest = {}
    row, = watcher.poll()
    assert row.state == YOUR_TURN
    assert row.detail == "Готово PDF на 2 страницы."
    assert INTERIM in row.body and "PDF на 2 страницы." in row.body

    watcher.hooks.latest["s1"] = ("Stop", now - 18, "")   # the hook path gives the same line
    row, = watcher.poll()
    assert row.detail == "Готово PDF на 2 страницы."


def test_claude_stop_hook_before_the_transcript_closes_the_turn(tmp_path):
    now = time.time()
    cs = ClaudeSession(str(tmp_path / "s2.jsonl"))
    cs.feed(_claude_lines(now)[:-1])           # end_turn not written yet
    watcher = ClaudeWatcher(home=str(tmp_path))
    watcher.hooks.latest[cs.id] = ("Stop", now, "")
    state, detail = watcher._hook_state(cs, cs.state(now), cs.detail(cs.state(now), now), now)
    assert state == YOUR_TURN and detail == "Готово"


# ---------------------------------------------------------------------------- 10: sub-agents
def test_codex_sub_agents_stay_under_their_root(tmp_path, monkeypatch):
    now = time.time()
    home = str(tmp_path)
    _rollout(home, "rollout-a.jsonl", [_meta("root-a", now - 100),
                                       _event(now - 1, type="task_started", turn_id="a")])
    _rollout(home, "rollout-a1.jsonl", [_meta("a1", now - 90, "root-a", "explorer", "Ada"),
                                        _event(now - 20, type="task_started")])
    _rollout(home, "rollout-a2.jsonl", [_meta("a2", now - 90, "root-a", "worker"),
                                        _event(now - 30, type="task_started")])
    _rollout(home, "rollout-b.jsonl", [_meta("root-b", now - 100),
                                       _event(now - 10, type="task_started", turn_id="b")])
    _rollout(home, "rollout-b1.jsonl", [_meta("b1", now - 90, "root-b", "tester"),
                                        _event(now - 15, type="task_started")])
    with open(os.path.join(home, "history.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"session_id": "app-chat", "ts": int(now - 12), "text": "Привет"}) + "\n")
    monkeypatch.setattr(codex, "ROLLOUT_RETRY", 3600)
    rows = CodexWatcher(home=home).poll()
    names = [row.name for row in rows]
    assert names == ["root-a", "- explorer Ada", "- worker", "root-b", "- tester", "Привет"]
    assert all(row.state == WORKING for row in rows)


# ---------------------------------------------------------------------------- 11: history rows
def test_history_only_chat_becomes_your_turn_then_idle(tmp_path):
    watcher = HistoryWatcher(str(tmp_path))
    with open(watcher.path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"session_id": "s", "ts": 1000, "text": "Сделай отчёт"}) + "\n")
    watcher.read()
    rows = watcher.rows(1000 + 30, set())
    assert [(r.state, r.attn) for r in rows] == [(WORKING, 0)]
    rows = watcher.rows(1000 + codex.HISTORY_WORKING_FOR + 1, set())
    assert [(r.state, r.attn) for r in rows] == [(YOUR_TURN, 1)]     # buzzes once
    assert watcher.rows(1000 + codex.YOUR_TURN_FOR + 1, set()) == []  # idle: hidden
    assert watcher.state(codex.YOUR_TURN_FOR + 1) == IDLE


def test_history_chat_with_an_older_rollout_uses_the_rollout_state(tmp_path):
    now = time.time()
    home = str(tmp_path)
    sid = "01a1087a-eb66-7430-976a-3e0dcdba7ca1"
    # the thread began five days ago, so its file is in a day folder _scan does not read
    _rollout(home, "rollout-2026-10-01T08-00-00-%s.jsonl" % sid, [
        _meta(sid, now - 5 * 86400),
        _event(now - 40, type="task_started", turn_id="t9"),
        _event(now - 5, type="task_complete", last_agent_message=FINAL),
    ], day_ts=now - 5 * 86400)
    with open(os.path.join(home, "history.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"session_id": sid, "ts": int(now - 40), "text": "Сделай PDF"}) + "\n")
    rows = CodexWatcher(home=home).poll()
    assert [(row.state, row.detail) for row in rows] == [(YOUR_TURN, FINAL)]


# ---------------------------------------------------------------------------- 12: byte caps
def test_utf8_text_caps_bytes_without_splitting_characters():
    text = "Ж" * 200
    for limit in (NAME_BYTES, DETAIL_BYTES, 127, 1):
        cut = utf8_text(text, limit)
        assert len(cut.encode("utf-8")) <= limit
        assert cut == "Ж" * (limit // 2)
    assert utf8_text("abc|def\u2014x", None) == "abc/def-x"
    assert len(utf8_text("a" * 300, DETAIL_BYTES)) == 255


def test_utf8_chunks_and_tail():
    text = "Привет мир " * 20
    chunks = utf8_chunks(text.strip(), 127)
    assert all(len(c.encode("utf-8")) <= 127 for c in chunks)
    assert " ".join(chunks) == text.strip()
    assert utf8_chunks("", 127) == [""]
    tail = utf8_tail("C:\\Users\\me\\Документы\\проект", 20)
    assert tail.startswith("...") and tail.endswith("проект")
    assert len(tail.encode("utf-8")) <= 20


def test_session_rows_use_the_flipper_byte_limits(tmp_path):
    now = time.time()
    long_name = "Очень длинное имя сессии про отчёт " * 3
    long_report = "Длинный финальный отчёт. " * 30
    _rollout(str(tmp_path), "rollout-a.jsonl", [
        _meta("root-a", now - 60),
        _event(now - 50, type="task_started", turn_id="t1"),
        _event(now - 10, type="task_complete", last_agent_message=long_report),
    ])
    with open(os.path.join(str(tmp_path), "session_index.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"id": "root-a", "thread_name": long_name}, ensure_ascii=False) + "\n")
    row, = CodexWatcher(home=str(tmp_path)).poll()
    assert 55 < len(row.name.encode("utf-8")) <= NAME_BYTES       # was 24 characters
    assert 240 < len(row.detail.encode("utf-8")) <= DETAIL_BYTES  # was 220 characters
    row.detail.encode("utf-8").decode("utf-8")                     # no broken character
