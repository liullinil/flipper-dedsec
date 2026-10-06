"""Companion-side config (JSON) and Windows autostart (HKCU Run key)."""
import json
import logging
import os
import sys

log = logging.getLogger("uplink.config")

APP_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "DedSecUplink")
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "DedSecUplink"

DEFAULTS = {
    "cmd_enabled": True,     # allow the Flipper to run shell commands on this PC
    "shell": "cmd",          # "cmd" or "powershell"
}


def load():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            cfg.update({k: v for k, v in json.load(fh).items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    return cfg


def save(cfg):
    os.makedirs(APP_DIR, exist_ok=True)
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
    except OSError as exc:
        log.warning("cannot save config: %s", exc)


# --------------------------------------------------------------------------- autostart
def _script_command():
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "dedsec_uplink.pyw")
    base = os.path.dirname(sys.executable)
    pyw = os.path.join(base, "pythonw.exe")
    exe = pyw if os.path.exists(pyw) else sys.executable
    return '"%s" "%s"' % (exe, script)


def autostart_enabled():
    if os.name != "nt":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_NAME)
            return True
    except OSError:
        return False


def set_autostart(enable):
    if os.name != "nt":
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enable:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, _script_command())
            log.info("autostart enabled: %s", _script_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_NAME)
                log.info("autostart disabled")
            except OSError:
                pass
