"""Press a Flipper button over the USB CLI: python tools/press.py back [short|long] [COM5]"""
import sys
import time

from flipper_lib import Flipper

key = sys.argv[1] if len(sys.argv) > 1 else "back"
kind = sys.argv[2] if len(sys.argv) > 2 else "short"
port = sys.argv[3] if len(sys.argv) > 3 else "COM5"
with Flipper(port) as f:
    f.cmd(f"input send {key} press")
    time.sleep(0.6 if kind == "long" else 0.05)
    f.cmd(f"input send {key} {kind}")
    f.cmd(f"input send {key} release")
    time.sleep(0.8)
    print(f.cmd("loader info"))
