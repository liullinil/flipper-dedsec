"""Launch the RF Signal Hunter desktop investigation console."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from uplink.rf_analyzer import main


if __name__ == "__main__":
    raise SystemExit(main())
