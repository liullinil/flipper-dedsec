"""Stream the Flipper's log for N seconds and print animation-manager lines.

    python tools/watch_log.py [seconds] [COM5]

Shows which idle animation the desktop picks ("Select 'X' animation") and any load errors,
which is the easiest way to confirm that rotation works.
"""
import sys
import time

import serial

secs = float(sys.argv[1]) if len(sys.argv) > 1 else 70
port = sys.argv[2] if len(sys.argv) > 2 else "COM5"
KEYS = ("Animation", "animation", "Dolphin", "AnimationStorage")

ser = serial.Serial(port, 230400, timeout=0.3)
ser.reset_input_buffer()
ser.write(b"\r")
time.sleep(0.5)
ser.reset_input_buffer()
ser.write(b"log info\r")
t0 = time.time()
buf = b""
while time.time() - t0 < secs:
    buf += ser.read(ser.in_waiting or 1)
    while b"\n" in buf:
        line, buf = buf.split(b"\n", 1)
        text = line.decode("latin-1").strip()
        if any(k in text for k in KEYS):
            print(f"{time.time() - t0:6.1f}s  {text}", flush=True)
ser.write(b"\x03")
time.sleep(0.3)
ser.close()
