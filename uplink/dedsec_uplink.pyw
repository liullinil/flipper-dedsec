"""DedSec Uplink - PC companion for the Flipper Zero "DedSec Uplink" app.

    pythonw dedsec_uplink.pyw            tray icon, runs in the background
    python  dedsec_uplink.pyw --console  same, with log output in the console
    python  dedsec_uplink.pyw --dump     print one data frame and exit (no Bluetooth)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from uplink.app import main  # noqa: E402

if __name__ == "__main__":
    main()
