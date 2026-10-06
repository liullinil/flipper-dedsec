"""Claude Code sessions (CLI and the desktop app's Code tab) from ~/.claude/projects/*/*.jsonl.

Signals used:
  user prompt (not a tool_result)          -> turn started, working
  system/stop_hook_summary, end_turn text  -> turn finished, your turn
  pending AskUserQuestion / ExitPlanMode    -> a question / plan waits for you
  pending tool > PERMISSION_AFTER in a prompting permission mode -> probably a permission prompt
  TodoWrite                                -> progress done/total and the current step
  custom-title / agent-name / last-prompt  -> session name
"""
import glob
import json
import os
import time

from .common import (Session, JsonlTail, AttentionCounter, parse_ts, recent_files, utf8_text,
                     strip_md, short_key, revision_token, join_text, WORKING, APPROVAL,
                     YOUR_TURN, IDLE, ORDER)

HOOK_EVENTS = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "DedSecUplink", "claude_events.jsonl")


class HookReader:
    """Reads claude_events.jsonl (written by uplink_hook.py) -> latest event per session.
    Hooks give precise transitions the transcript can only guess at."""

    def __init__(self, path=HOOK_EVENTS):
        self.tail = JsonlTail(path, initial_tail=128 * 1024)
        self.latest = {}  # session_id -> (event, ts, message)

    def poll(self):
        if not self.tail.changed():
            return
        for o in self.tail.read():
            sid = o.get("session_id")
            if not sid:
                continue
            self.latest[sid] = (o.get("event", ""), float(o.get("ts", 0)), o.get("message", ""))

    def get(self, session_id):
        return self.latest.get(session_id)

ACTIVE_WINDOW = 3 * 3600
YOUR_TURN_FOR = 20 * 60
STALL_AFTER = 15 * 60
PERMISSION_AFTER = 90
PROMPTING_MODES = ("default", "acceptEdits", "plan")
ASKING_TOOLS = ("AskUserQuestion", "ExitPlanMode")


def _first_line(text):
    """First meaningful line; glue on the next one when it is just a label like 'Done:'."""
    out = ""
    for line in (text or "").splitlines():
        line = line.strip(" #*-")
        if not line:
            continue
        out = f"{out} {line}".strip()
        if len(out) >= 24:
            break
    return out


def _report(text, limit=220):
    """Keep the useful body of the latest agent report for the detail view."""
    parts = []
    for line in (text or "").splitlines():
        line = line.strip(" #*-")
        if line:
            parts.append(line)
    return " ".join(parts)[:limit]


def describe_tool(name, inp):
    inp = inp if isinstance(inp, dict) else {}
    if name == "Bash" or name == "PowerShell":
        return inp.get("description") or "$ " + _first_line(inp.get("command", ""))
    if name in ("Read", "Edit", "Write", "NotebookEdit"):
        return f"{name.lower()} {os.path.basename(inp.get('file_path', '') or '')}"
    if name in ("Grep", "Glob"):
        return f"{name.lower()} {inp.get('pattern', '')}"
    if name in ("Task", "Agent"):
        return "agent: " + (inp.get("description") or inp.get("subagent_type") or "")
    if name.startswith("Web"):
        return "web: " + (inp.get("query") or inp.get("url") or "")
    if name.startswith("mcp__"):
        return name.split("__")[-1].replace("_", " ")
    return name


class ClaudeSession:
    def __init__(self, path):
        self.tail = JsonlTail(path)
        self.id = os.path.splitext(os.path.basename(path))[0]
        self.project = os.path.basename(os.path.dirname(path))
        self.title = ""
        self.agent_name = ""
        self.last_prompt = ""
        self.cwd = ""
        self.mode = ""
        self.turn_open = False
        self.turn_end = 0.0
        self.turn_id = ""
        self.turn_serial = 0
        self.last_ts = 0.0
        self.pending = {}       # tool_use id -> (name, input, ts)
        self.activity = ""
        self.last_text = ""
        self.text_parts = []
        self.final_text = ""
        self.todo = (0, 0, "")

    def feed(self, entries):
        for o in entries:
            t = o.get("type")
            ts = parse_ts(o.get("timestamp"))
            if t == "custom-title":
                self.title = o.get("customTitle") or self.title
            elif t == "agent-name":
                self.agent_name = o.get("agentName") or self.agent_name
            elif t == "last-prompt":
                self.last_prompt = o.get("lastPrompt") or self.last_prompt
            if ts:
                self.last_ts = max(self.last_ts, ts)
            if o.get("isSidechain"):
                continue        # sub-agent chatter: keeps the session alive, no state change
            if o.get("cwd"):
                self.cwd = o["cwd"]
            if o.get("permissionMode"):
                self.mode = o["permissionMode"]
            if t == "user":
                self._user(o, ts)
            elif t == "assistant":
                self._assistant(o, ts)
            elif t == "system" and o.get("subtype") == "stop_hook_summary":
                self._end(ts)

    def _end(self, ts):
        self.turn_open = False
        self.turn_end = ts or self.last_ts
        self.pending.clear()
        self.final_text = join_text(self.text_parts) or self.final_text
        self.last_text = self.final_text

    def _user(self, o, ts):
        content = (o.get("message") or {}).get("content")
        if isinstance(content, list):
            results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
            for b in results:
                self.pending.pop(b.get("tool_use_id"), None)
            if results:
                return
            text = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
        else:
            text = content or ""
        if "[Request interrupted by user" in text:
            self._end(ts)
            return
        if o.get("isMeta"):
            return
        self.turn_serial += 1
        self.turn_id = str(o.get("turn_id") or self.turn_serial)
        self.turn_open = True
        self.pending.clear()
        self.text_parts = []
        self.activity = ""
        if text and not text.lstrip().startswith("<"):
            self.last_prompt = text

    def _assistant(self, o, ts):
        msg = o.get("message") or {}
        content = msg.get("content") if isinstance(msg, dict) else None
        blocks = list(content) if isinstance(content, list) else ([content] if content else [])
        if isinstance(msg, dict) and msg.get("text"):
            blocks.append({"type": "text", "text": msg["text"]})
        for b in blocks:
            if isinstance(b, str):
                b = {"type": "text", "text": b}
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                name, inp = b.get("name", ""), b.get("input") or {}
                self.pending[b.get("id")] = (name, inp, ts or self.last_ts)
                if name == "TodoWrite":
                    todos = inp.get("todos") or []
                    done = sum(1 for x in todos if x.get("status") == "completed")
                    cur = next((x.get("activeForm") or x.get("content", "")
                                for x in todos if x.get("status") == "in_progress"), "")
                    self.todo = (done, len(todos), cur)
                else:
                    self.activity = describe_tool(name, inp)
                self.turn_open = True
            elif b.get("type") == "text" and b.get("text", "").strip():
                self.text_parts.append(b["text"])
                self.last_text = join_text(self.text_parts)
        if msg.get("stop_reason") == "end_turn":
            self._end(ts)

    def asking(self):
        for name, inp, _ in self.pending.values():
            if name in ASKING_TOOLS:
                if name == "ExitPlanMode":
                    return "plan ready for review"
                qs = inp.get("questions") or []
                q = qs[0].get("question") if qs and isinstance(qs[0], dict) else ""
                return "Q: " + (q or "question for you")
        return ""

    def permission_wait(self, now):
        if self.mode not in PROMPTING_MODES or not self.pending:
            return ""
        name, inp, ts = max(self.pending.values(), key=lambda v: v[2])
        if now - max(ts, self.last_ts) > PERMISSION_AFTER:
            return "allow? " + describe_tool(name, inp)
        return ""

    def state(self, now):
        if self.asking() or (self.turn_open and self.permission_wait(now)):
            return APPROVAL
        if self.turn_open:
            return WORKING if now - self.last_ts < STALL_AFTER else IDLE
        if self.turn_end and now - self.turn_end < YOUR_TURN_FOR:
            return YOUR_TURN
        return IDLE

    def name(self):
        if self.title or self.agent_name:
            return self.title or self.agent_name
        if self.last_prompt:
            return _first_line(self.last_prompt)
        return os.path.basename(self.cwd) or self.project

    def detail(self, st, now):
        if st == APPROVAL:
            return self.asking() or self.permission_wait(now)
        if st == WORKING:
            return self.todo[2] or self.activity or "thinking"
        return _report(strip_md(self.final_text or self.last_text)) or self.activity

    def body(self, st):
        if st == APPROVAL:
            return self.asking() or self.permission_wait(time.time())
        if st == WORKING:
            return self.todo[2] or self.activity or "thinking"
        return strip_md(self.final_text or self.last_text) or self.activity


class ClaudeWatcher:
    def __init__(self, home=None):
        self.home = home or os.path.join(os.path.expanduser("~"), ".claude")
        self.sessions = {}
        self.attn = AttentionCounter()
        self.files = []
        self.last_scan = 0.0
        self.hooks = HookReader()

    def _hook_state(self, cs, st, detail, now):
        """Refine state with the precise hook event, unless the transcript has clearly moved on."""
        ev = self.hooks.get(cs.id)
        if not ev:
            return st, detail
        event, ts, message = ev
        moved_on = cs.last_ts > ts + 3   # Claude did things after the hook fired -> hook is stale
        if event == "Stop" and not moved_on and now - ts < YOUR_TURN_FOR:
            return YOUR_TURN, cs.body(YOUR_TURN) or detail
        if event == "Notification" and not moved_on:
            return APPROVAL, strip_md(message) or "needs your attention"
        if event == "UserPromptSubmit" and not moved_on:
            return WORKING, detail
        return st, detail

    def _scan(self, now):
        files = glob.glob(os.path.join(self.home, "projects", "*", "*.jsonl"))
        self.files = recent_files(files, ACTIVE_WINDOW, now)
        self.last_scan = now
        for p in list(self.sessions):
            if p not in self.files:
                del self.sessions[p]

    def poll(self):
        now = time.time()
        if now - self.last_scan > 15:
            self._scan(now)
        self.hooks.poll()
        rows = []
        for p in self.files:
            cs = self.sessions.get(p)
            if cs is None:
                cs = self.sessions[p] = ClaudeSession(p)
            if cs.tail.changed():
                cs.feed(cs.tail.read())
            if not cs.last_ts or now - cs.last_ts > ACTIVE_WINDOW:
                continue
            st = cs.state(now)
            detail = cs.detail(st, now)
            st, detail = self._hook_state(cs, st, detail, now)
            full_name = cs.name()
            body = detail if st == APPROVAL and detail else cs.body(st)
            s = Session(key=short_key(cs.id), name=utf8_text(full_name, 24), state=st,
                        detail=utf8_text(body, 220), last_ts=cs.last_ts,
                        body=body, revision=revision_token(cs.turn_id or cs.id, body),
                        full_name=full_name)
            s.done, s.total = cs.todo[0], cs.todo[1]
            if st not in (WORKING, APPROVAL) and s.done == s.total:
                s.done = s.total = 0     # finished checklist: show age instead
            s.attn = self.attn.update(s.key, st)
            # Keep idle state in the counter so a later attention transition is
            # still detected, but omit quiet sessions from the Flipper list.
            if st == IDLE:
                continue
            rows.append(s)
        rows.sort(key=lambda s: (ORDER.get(s.state, 9), -s.last_ts))
        return rows
