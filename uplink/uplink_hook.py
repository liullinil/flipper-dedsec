"""Claude Code hook -> DedSec Uplink.

Configured in ~/.claude/settings.json for the events Notification, Stop, SubagentStop and
UserPromptSubmit (see install_claude_hooks.py). Claude pipes a JSON object on stdin; we append
one compact event line to <LOCALAPPDATA>\\DedSecUplink\\claude_events.jsonl, which the companion
reads to show a precise state (needs-you / your-turn / working) for each Claude session.

It must never fail or block Claude: everything is wrapped, output is nothing, exit is always 0.
"""
import json
import os
import sys
import time

APP_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "DedSecUplink")
EVENTS = os.path.join(APP_DIR, "claude_events.jsonl")
MAX_BYTES = 256 * 1024


def main():
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}
    event = data.get("hook_event_name") or (sys.argv[1] if len(sys.argv) > 1 else "")
    rec = {
        "ts": time.time(),
        "session_id": data.get("session_id", ""),
        "event": event,
        "message": (data.get("message") or "")[:200],
        "cwd": data.get("cwd", ""),
    }
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        # keep the file bounded without locking: rewrite tail when it grows too big
        if os.path.exists(EVENTS) and os.path.getsize(EVENTS) > MAX_BYTES:
            try:
                with open(EVENTS, encoding="utf-8") as fh:
                    tail = fh.readlines()[-500:]
                with open(EVENTS, "w", encoding="utf-8") as fh:
                    fh.writelines(tail)
            except Exception:
                pass
        with open(EVENTS, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


if __name__ == "__main__":
    main()
    sys.exit(0)
