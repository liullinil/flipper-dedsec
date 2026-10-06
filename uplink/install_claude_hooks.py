"""Install/remove the DedSec Uplink hooks in ~/.claude/settings.json.

    python install_claude_hooks.py            # add (merged with your other hooks, backup kept)
    python install_claude_hooks.py --remove   # take them out

The .exe build does the same with `DedSecUplink.exe --install-claude-hooks` / `--remove-claude-hooks`
or from the tray menu. Restart Claude Code sessions (or run /hooks) afterwards.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from uplink import hooks  # noqa: E402

if __name__ == "__main__":
    remove = "--remove" in sys.argv
    cmd = hooks.install(remove=remove)
    print("hooks %s %s" % ("removed from" if remove else "installed in", hooks.SETTINGS))
    if not remove:
        print("command:", cmd)
        print("Restart Claude Code sessions (or run /hooks) for the change to take effect.")
