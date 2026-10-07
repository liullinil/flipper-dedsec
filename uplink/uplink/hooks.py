"""Claude Code hooks for precise session state (shared by the script and the .exe build).

record_event() is what the hook runs: Claude pipes a JSON object on stdin and we append one line to
<LOCALAPPDATA>\\DedSecUplink\\claude_events.jsonl, which ClaudeWatcher reads.
install()/installed() manage the Notification, Stop, SubagentStop and UserPromptSubmit entries in
~/.claude/settings.json (merged with whatever is there, a timestamped backup is kept).
"""
import json
import logging
import os
import shutil
import sys
import time

log = logging.getLogger("uplink.hooks")

APP_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "DedSecUplink")
EVENTS = os.path.join(APP_DIR, "claude_events.jsonl")
SETTINGS = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
HOOK_EVENTS = ("Notification", "Stop", "SubagentStop", "UserPromptSubmit")
MARKERS = ("uplink_hook.py", "uplink_hook.exe", "DedSecUplink.exe")
NATIVE_HOOK = os.path.join(APP_DIR, "uplink_hook.exe")   # hook/uplink_hook.c, ~20 ms per event
MAX_BYTES = 256 * 1024


# --------------------------------------------------------------------------- the hook itself
def record_event(stream=None):
    """Never fails and never blocks Claude."""
    stream = stream or sys.stdin
    try:
        raw = stream.read() if stream else ""
        data = json.loads(raw) if raw and raw.strip() else {}
    except Exception:
        data = {}
    rec = {
        "ts": time.time(),
        "session_id": data.get("session_id", ""),
        "event": data.get("hook_event_name", ""),
        "message": (data.get("message") or "")[:200],
        "cwd": data.get("cwd", ""),
    }
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        if os.path.exists(EVENTS) and os.path.getsize(EVENTS) > MAX_BYTES:
            with open(EVENTS, encoding="utf-8") as fh:
                tail = fh.readlines()[-500:]
            with open(EVENTS, "w", encoding="utf-8") as fh:
                fh.writelines(tail)
        with open(EVENTS, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


# --------------------------------------------------------------------------- settings.json
def _bundled_hook():
    """The native hook shipped with this companion (inside the .exe, or built in uplink/hook)."""
    if getattr(sys, "frozen", False):
        return os.path.join(getattr(sys, "_MEIPASS", ""), "uplink_hook.exe")
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, "hook", "uplink_hook.exe")


def install_native_hook():
    """Keep a copy of the native hook in APP_DIR (the .exe unpacks to a temp folder that goes
    away). Returns True when it is there."""
    source = _bundled_hook()
    try:
        if os.path.exists(source):
            with open(source, "rb") as fh:
                data = fh.read()
            current = None
            if os.path.exists(NATIVE_HOOK):
                with open(NATIVE_HOOK, "rb") as fh:
                    current = fh.read()
            if current != data:
                os.makedirs(APP_DIR, exist_ok=True)
                tmp = NATIVE_HOOK + ".tmp"
                with open(tmp, "wb") as fh:
                    fh.write(data)
                os.replace(tmp, NATIVE_HOOK)
    except OSError as exc:      # e.g. Claude runs the old copy right now: keep it
        log.warning("cannot update the native hook: %s", exc)
    return os.path.exists(NATIVE_HOOK)


def hook_command():
    if os.path.exists(NATIVE_HOOK):
        return '"%s"' % NATIVE_HOOK
    if getattr(sys, "frozen", False):                     # the PyInstaller .exe
        return '"%s" --hook' % sys.executable
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    exe = pyw if os.path.exists(pyw) else sys.executable
    return '"%s" "%s"' % (exe, os.path.join(here, "uplink_hook.py"))


def _load():
    try:
        with open(SETTINGS, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save(data):
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    if os.path.exists(SETTINGS):
        shutil.copy2(SETTINGS, SETTINGS + ".bak-%d" % int(time.time()))
    with open(SETTINGS, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def _ours(hook):
    return any(m in str(hook.get("command", "")) for m in MARKERS)


def installed():
    hooks = _load().get("hooks", {})
    return any(_ours(h) for groups in hooks.values() for g in groups or [] for h in g.get("hooks", []))


def install(remove=False):
    data = _load()
    hooks = data.setdefault("hooks", {})
    cmd = hook_command()
    for event in HOOK_EVENTS:
        groups = []
        for g in hooks.get(event) or []:
            kept = [h for h in g.get("hooks", []) if not _ours(h)]
            if kept:
                groups.append(dict(g, hooks=kept))
        if not remove:
            groups.append({"hooks": [{"type": "command", "command": cmd, "timeout": 5}]})
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    if not hooks:
        data.pop("hooks", None)
    _save(data)
    return cmd


def ensure_installed():
    """The Claude Code hooks are always on: install them (or fix a stale command) at start."""
    install_native_hook()
    hooks = _load().get("hooks", {})
    cmd = hook_command()
    for event in HOOK_EVENTS:
        ours = [h for g in hooks.get(event) or [] for h in g.get("hooks", []) if _ours(h)]
        if len(ours) != 1 or ours[0].get("command") != cmd:
            install()
            log.info("claude hooks installed: %s", cmd)
            return True
    return False
