"""Codex (CLI `codex --profile ...` and the desktop app) sessions from ~/.codex/sessions.

Each thread writes a rollout JSONL file. Signals used:
  task_started / task_complete / turn_aborted   -> working / your turn
  request_user_input_async (function_call)     -> a question waits for you (until a UserMessage)
  *approval_request* events                     -> approval needed (older CLIs, approval_policy != never)
  response_item reasoning summary, commands, file edits -> "what it is doing now"
  session_meta.source.subagent                  -> sub-agent of a root thread ("threads")
"""
import glob
import json
import os
import time

from .common import (Session, JsonlTail, AttentionCounter, parse_ts, recent_files, utf8_text,
                     strip_md, short_key, revision_token, join_text, WORKING, APPROVAL,
                     YOUR_TURN, IDLE, ORDER)

ACTIVE_WINDOW = 3 * 3600    # rollout files touched within this window are watched
YOUR_TURN_FOR = 20 * 60     # a finished turn counts as "your turn" this long
STALL_AFTER = 20 * 60       # an open turn silent this long is treated as idle (process gone)
DAYS_BACK = 3


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


class Thread:
    def __init__(self, path):
        self.tail = JsonlTail(path)
        self.id = ""
        self.session = ""
        self.parent = ""
        self.agent_path = ""
        self.nickname = ""
        self.cwd = ""
        self.turn_open = False
        self.turn_end = 0.0
        self.turn_id = ""
        self.turn_serial = 0
        self.last_ts = 0.0
        self.question = ""
        self.approval = ""
        self.activity = ""
        self.final = ""
        self.response_parts = []

    @property
    def is_sub(self):
        return bool(self.parent)

    def feed(self, entries):
        for o in entries:
            ts = parse_ts(o.get("timestamp"))
            if ts:
                self.last_ts = max(self.last_ts, ts)
            kind = o.get("type")
            p = o.get("payload") if isinstance(o.get("payload"), dict) else {}
            if kind == "session_meta":
                self._meta(p)
            elif kind == "event_msg":
                self._event(p, ts)
            elif kind == "response_item":
                self._response(p)

    def _meta(self, p):
        self.id = p.get("id", "")
        self.session = p.get("session_id") or self.id
        self.cwd = p.get("cwd", "")
        src = p.get("source")
        if isinstance(src, dict) and isinstance(src.get("subagent"), dict):
            spawn = src["subagent"].get("thread_spawn") or {}
            self.parent = spawn.get("parent_thread_id", "") or p.get("parent_thread_id", "")
            self.agent_path = spawn.get("agent_path", "") or ""
            self.nickname = spawn.get("agent_nickname", "") or ""

    def _event(self, p, ts):
        t = p.get("type", "")
        if t == "task_started":
            self.turn_open = True
            self.turn_serial += 1
            self.turn_id = str(p.get("turn_id") or p.get("root_turn_id") or self.turn_serial)
            self.question = ""
            self.final = ""
            self.response_parts = []
        elif t == "task_complete":
            self.turn_open = False
            self.turn_end = ts or self.last_ts
            self.final = join_text(self.response_parts + [p.get("last_agent_message") or ""])
            self.approval = ""
        elif t == "turn_aborted":
            self.turn_open = False
            self.turn_end = ts or self.last_ts
            self.final = "turn aborted"
            self.approval = ""
        elif "approval_request" in t:
            cmd = p.get("command") or p.get("reason") or "approval"
            self.approval = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
        elif t == "item_completed":
            self._item(p.get("item") or {})

    def _item(self, item):
        it = item.get("type")
        if it == "UserMessage":
            self.question = ""
            self.approval = ""
        elif it == "CommandExecution":
            cmd = item.get("command")
            if isinstance(cmd, list) and cmd:
                cmd = cmd[-1]
            self.activity = "$ " + _first_line(str(cmd or ""))
            self.approval = ""
        elif it == "FileChange":
            changes = item.get("changes") or {}
            names = [os.path.basename(k) for k in (changes.keys() if isinstance(changes, dict) else [])]
            if names:
                self.activity = "edit " + ", ".join(names[:3])
        elif it == "Reasoning":
            summary = item.get("summary_text") or []
            if summary:
                self.activity = strip_md(summary[-1])

    def _response(self, p):
        t = p.get("type")
        if t == "message" and p.get("role", "assistant") == "assistant":
            content = p.get("content")
            parts = []
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") in ("output_text", "text") and block.get("text"):
                        parts.append(block["text"])
            elif isinstance(content, str):
                parts.append(content)
            elif p.get("text"):
                parts.append(p["text"])
            self.response_parts.extend(parts)
        elif t == "reasoning":
            summary = p.get("summary") or []
            if summary and isinstance(summary[-1], dict):
                self.activity = strip_md(summary[-1].get("text", ""))
        elif t == "function_call" and p.get("name") == "request_user_input_async":
            try:
                args = json.loads(p.get("arguments") or "{}")
            except ValueError:
                args = {}
            qs = args.get("questions") or []
            q = qs[0] if qs and isinstance(qs[0], dict) else {}
            self.question = q.get("title") or q.get("question") or "question for you"

    def state(self, now):
        if self.question or self.approval:
            return APPROVAL
        if self.turn_open:
            return WORKING if now - self.last_ts < STALL_AFTER else IDLE
        if self.turn_end and not self.is_sub and now - self.turn_end < YOUR_TURN_FOR:
            return YOUR_TURN
        return IDLE


class HistoryWatcher:
    """Recent Codex app/API conversations from CODEX_HOME/history.jsonl.

    These conversations do not always have a rollout file, but the history journal still
    gives us a stable session id, the first user message as a name, and the latest prompt
    as useful activity. They are treated as working until they have been quiet for the
    normal stall window, then omitted with other idle sessions.
    """

    def __init__(self, home):
        self.path = os.path.join(home, "history.jsonl")
        self.tail = JsonlTail(self.path, initial_tail=512 * 1024)
        self.sessions = {}
        self.attn = AttentionCounter()

    def poll(self, now, known_ids):
        if self.tail.changed():
            for obj in self.tail.read():
                sid = str(obj.get("session_id") or "")
                text = str(obj.get("text") or "").strip()
                if not sid or not text:
                    continue
                try:
                    ts = float(obj.get("ts") or 0)
                except (TypeError, ValueError):
                    ts = 0.0
                item = self.sessions.setdefault(sid, {"first": text, "last": text, "ts": ts})
                item["first"] = item.get("first") or text
                if ts >= item.get("ts", 0):
                    item["last"], item["ts"] = text, ts

        rows = []
        for sid, item in self.sessions.items():
            if sid in known_ids:
                continue
            ts = float(item.get("ts") or 0)
            if not ts or now - ts > ACTIVE_WINDOW:
                continue
            state = WORKING if now - ts < STALL_AFTER else IDLE
            key = short_key(sid)
            body = "chat: " + item["last"]
            row = Session(
                key=key,
                name=utf8_text(_first_line(item["first"]) or "Codex chat", 24),
                state=state,
                detail=utf8_text(body, 220),
                last_ts=ts,
                body=body,
                revision=revision_token(sid, body),
                full_name=item["first"],
            )
            row.attn = self.attn.update(key, state)
            if state != IDLE:
                rows.append(row)
        return rows

class CodexWatcher:
    def __init__(self, home=None):
        self.home = home or os.environ.get("CODEX_HOME") or os.path.join(
            os.path.expanduser("~"), ".codex")
        self.threads = {}
        self.history = HistoryWatcher(self.home)
        self.names = {}
        self.names_mtime = 0.0
        self.attn = AttentionCounter()
        self.files = []
        self.last_scan = 0.0

    def _scan(self, now):
        root = os.path.join(self.home, "sessions")
        files = []
        for back in range(DAYS_BACK):
            day = time.strftime("%Y/%m/%d", time.localtime(now - back * 86400))
            files += glob.glob(os.path.join(root, *day.split("/"), "*.jsonl"))
        self.files = recent_files(files, ACTIVE_WINDOW, now)
        self.last_scan = now
        for p in list(self.threads):
            if p not in self.files:
                del self.threads[p]

    def _load_names(self):
        path = os.path.join(self.home, "session_index.jsonl")
        try:
            m = os.path.getmtime(path)
        except OSError:
            return
        if m == self.names_mtime:
            return
        self.names_mtime = m
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if o.get("id") and o.get("thread_name"):
                    self.names[o["id"]] = o["thread_name"]

    def poll(self):
        now = time.time()
        if now - self.last_scan > 15:
            self._scan(now)
        self._load_names()
        for p in self.files:
            th = self.threads.get(p)
            if th is None:
                th = self.threads[p] = Thread(p)
            if th.tail.changed():
                th.feed(th.tail.read())

        threads = [t for t in self.threads.values() if t.id]
        by_id = {t.id: t for t in threads}
        kids = {}
        for t in threads:
            if t.is_sub and t.parent in by_id:
                kids.setdefault(t.parent, []).append(t)

        rows = []
        for t in threads:
            if t.is_sub and t.parent in by_id:
                continue
            if now - t.last_ts > ACTIVE_WINDOW:
                continue
            children = kids.get(t.id, [])
            st = t.state(now)
            busy = [k for k in children if k.state(now) == WORKING]
            if st in (YOUR_TURN, IDLE) and busy:
                st = WORKING  # the root is waiting on its own agents, not on you
            name = self.names.get(t.session) or self.names.get(t.id) or os.path.basename(t.cwd)
            if t.is_sub:
                name = (t.agent_path.rsplit("/", 1)[-1] or "agent") + " (sub)"
            s = Session(key=short_key(t.id), name=utf8_text(name, 24), state=st,
                        last_ts=max([t.last_ts] + [k.last_ts for k in children]))
            s.total = len(children)
            s.done = len(children) - len(busy)
            body = self._body(t, st, busy)
            s.detail = utf8_text(body, 220)
            s.body = body
            s.revision = revision_token(t.turn_id or t.id, body)
            s.full_name = name
            s.attn = self.attn.update(s.key, st)
            # Idle sessions stay tracked for attention transitions, but do not
            # consume one of the small Flipper list slots.
            if st == IDLE:
                continue
            subs = []
            for k in busy:
                ks = Session(key=short_key(k.id),
                             name=utf8_text("- " + (k.agent_path.rsplit("/", 1)[-1] or "agent")
                                            + (" " + k.nickname if k.nickname else ""), 24),
                             state=k.state(now), last_ts=k.last_ts,
                             detail=utf8_text(k.activity, 220))
                ks.body = k.activity
                ks.revision = revision_token(k.turn_id or k.id, k.activity)
                ks.full_name = "- " + (k.agent_path.rsplit("/", 1)[-1] or "agent") + (
                    " " + k.nickname if k.nickname else "")
                ks.attn = self.attn.update(ks.key, ks.state)
                subs.append(ks)
            rows.append((s, subs))

        rows.sort(key=lambda r: (ORDER.get(r[0].state, 9), -r[0].last_ts))
        out = []
        for s, subs in rows:
            out.append(s)
            out.extend(sorted(subs, key=lambda k: -k.last_ts))
        known_ids = {t.id for t in threads}
        out.extend(self.history.poll(now, known_ids))
        out.sort(key=lambda s: (ORDER.get(s.state, 9), -s.last_ts))
        return out

    @staticmethod
    def _body(t, st, busy):
        if t.question:
            return "Q: " + t.question
        if t.approval:
            return "approve: " + t.approval
        if st == WORKING:
            if t.turn_open and t.activity:
                return t.activity
            if busy:
                return "agents: " + ", ".join(
                    (k.nickname or k.agent_path.rsplit("/", 1)[-1]) for k in busy)
            return t.activity or "working"
        return t.final or t.activity
