"""Claude Code hook -> DedSec Uplink (script form; the .exe uses `DedSecUplink.exe --hook`).

Appends one event line per Notification / Stop / SubagentStop / UserPromptSubmit so the companion
can show a precise state for each Claude session. Always exits 0 and prints nothing.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from uplink.hooks import record_event  # noqa: E402

if __name__ == "__main__":
    record_event()
    sys.exit(0)
