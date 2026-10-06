from uplink.common import IDLE, WORKING, YOUR_TURN, Session
from uplink.claude import ClaudeSession, ClaudeWatcher
from uplink.codex import Thread
from uplink.session_views import SessionViews


def _session(key="k", state=YOUR_TURN, revision="r1", **kwargs):
    return Session(key, "short", state=state, revision=revision, **kwargs)


def test_long_unicode_report_pages_to_the_last_line(tmp_path):
    report = "Привет мир. " * 80
    view = SessionViews(tmp_path / "acks.json")
    row = _session(full_name="Полное имя с длинным описанием", body=report)
    view.update("codex", [row])

    first = view.page("codex", "k", width=21, count=4)
    assert first["name"] == "Полное имя с длинным описанием"
    assert first["total"] > 4
    last_offset = max(0, first["total"] - 4)
    last = view.page("codex", "k", offset=last_offset, width=21, count=4)
    assert "мир." in " ".join(last["lines"])


def test_acknowledgement_is_revision_specific_and_persistent(tmp_path):
    path = tmp_path / "acks.json"
    view = SessionViews(path)
    first = _session(revision="turn-1")
    assert view.update("codex", [first]) == [first]
    assert view.acknowledge("codex", "k", "turn-1")
    assert view.update("codex", [first]) == []

    working = _session(state=WORKING, revision="turn-2")
    assert view.update("codex", [working]) == [working]
    second = _session(revision="turn-2")
    assert view.update("codex", [second]) == [second]
    assert not view.acknowledge("codex", "k", "turn-1")
    assert view.acknowledge("codex", "k", "turn-2")
    assert SessionViews(path).update("codex", [second]) == []


def test_idle_is_filtered_but_active_and_error_are_kept():
    view = SessionViews()
    rows = [_session("idle", IDLE), _session("work", WORKING),
            _session("error", "E")]
    assert [row.key for row in view.update("claude", rows)] == ["work", "error"]


def test_codex_final_combines_response_item_and_task_complete():
    thread = Thread("rollout.jsonl")
    thread.feed([
        {"type": "session_meta", "payload": {"id": "id"}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t1"}},
        {"type": "response_item", "payload": {
            "type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": "Первый абзац."},
                        {"type": "output_text", "text": "Второй абзац."}],
        }},
        {"type": "event_msg", "payload": {
            "type": "task_complete", "last_agent_message": "Третий абзац.",
        }},
    ])
    assert thread.final == "Первый абзац.\n\nВторой абзац.\n\nТретий абзац."


def test_claude_stop_hook_prefers_final_text_over_current_step(tmp_path):
    cs = ClaudeSession(str(tmp_path / "session.jsonl"))
    now = 1000.0
    cs.feed([
        {"type": "user", "timestamp": "1970-01-01T00:16:00+00:00",
         "message": {"content": "сделай работу"}},
        {"type": "assistant", "timestamp": "1970-01-01T00:16:01+00:00",
         "message": {"content": [
             {"type": "tool_use", "name": "Bash", "input": {"command": "текущий шаг"}},
             {"type": "text", "text": "Финальный абзац.\nВсё готово."},
         ], "stop_reason": "end_turn"}},
    ])
    watcher = ClaudeWatcher(home=str(tmp_path))
    watcher.hooks.latest[cs.id] = ("Stop", now, "")
    state, detail = watcher._hook_state(cs, cs.state(now), cs.detail(cs.state(now), now), now)
    assert state == YOUR_TURN
    assert "Финальный абзац." in detail
    assert "текущий шаг" not in detail

