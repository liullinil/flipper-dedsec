"""Install/remove DedSec Uplink hooks in ~/.claude/settings.json.

    python install_claude_hooks.py            # add the hooks (merges, keeps your other hooks)
    python install_claude_hooks.py --remove   # take them out again

Adds command hooks for Notification, Stop, SubagentStop and UserPromptSubmit that run
uplink_hook.py, so the companion knows precisely when a Claude session needs you, finishes a
turn, or starts working. A timestamped backup of settings.json is written next to it.
"""
import json
import os
import shutil
import sys
import time

HOME = os.path.expanduser("~")
SETTINGS = os.path.join(HOME, ".claude", "settings.json")
HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uplink_hook.py")
EVENTS = ("Notification", "Stop", "SubagentStop", "UserPromptSubmit")
TAG = "uplink_hook.py"


def _pythonw():
    base = os.path.dirname(sys.executable)
    pyw = os.path.join(base, "pythonw.exe")
    return pyw if os.path.exists(pyw) else sys.executable


def command_string():
    return '"%s" "%s"' % (_pythonw(), HOOK)


def load():
    try:
        with open(SETTINGS, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save(data):
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    if os.path.exists(SETTINGS):
        shutil.copy2(SETTINGS, SETTINGS + ".bak-%d" % int(time.time()))
    with open(SETTINGS, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def strip_ours(groups):
    """Drop any hook group that references our script (so re-install is idempotent)."""
    out = []
    for g in groups or []:
        hooks = [h for h in g.get("hooks", []) if TAG not in str(h.get("command", ""))]
        if hooks:
            g = dict(g)
            g["hooks"] = hooks
            out.append(g)
        elif not g.get("hooks"):
            out.append(g)
    return out


def install(remove=False):
    data = load()
    hooks = data.setdefault("hooks", {})
    cmd = command_string()
    for event in EVENTS:
        hooks[event] = strip_ours(hooks.get(event))
        if not remove:
            hooks[event].append({"hooks": [{"type": "command", "command": cmd, "timeout": 5}]})
        if not hooks[event]:
            del hooks[event]
    if not hooks:
        data.pop("hooks", None)
    save(data)
    print("%s hooks %s %s" % (
        "removed" if remove else "installed", "from" if remove else "in", SETTINGS))
    if not remove:
        print("command:", cmd)
        print("Restart Claude Code sessions (or /hooks) for the change to take effect.")


if __name__ == "__main__":
    install(remove="--remove" in sys.argv)
