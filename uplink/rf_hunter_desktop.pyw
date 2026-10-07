"""Standalone RF Signal Hunter viewer (no console window).

Imports from the Flipper run inside the DedSec Uplink companion; this viewer
only reads event folders.  Errors go to %LOCALAPPDATA%\\DedSecUplink\\rf_hunter.log.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from uplink.rf_analyzer import main, setup_standalone_logging


if __name__ == "__main__":
    setup_standalone_logging()
    raise SystemExit(main())
