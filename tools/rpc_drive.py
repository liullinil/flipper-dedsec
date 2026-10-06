"""Drive the Flipper over RPC in one process: send button events and grab screen frames.

    python tools/rpc_drive.py OUTDIR KEYS...

KEYS is a sequence like: up down left right ok back  (each a short press)
A frame PNG is saved after every key (plus one at the start). Avoids COM-port contention
between CLI presses and the screen stream, and works even for views that only redraw on input.
"""
import os
import sys
import time

import serial
from PIL import Image

LCD_BG, LCD_INK = (255, 152, 32), (28, 20, 8)
KEYS = {"up": 0, "down": 1, "right": 2, "left": 3, "ok": 4, "back": 5}
PRESS, RELEASE, SHORT, LONG = 0, 1, 2, 3


def varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def read_varint(buf, i):
    shift = val = 0
    while True:
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return val, i


def fields(msg):
    i = 0
    while i < len(msg):
        key, i = read_varint(msg, i)
        no, wt = key >> 3, key & 7
        if wt == 0:
            v, i = read_varint(msg, i)
        elif wt == 2:
            ln, i = read_varint(msg, i)
            v, i = msg[i:i + ln], i + ln
        elif wt == 5:
            v, i = msg[i:i + 4], i + 4
        else:
            v, i = msg[i:i + 8], i + 8
        yield no, wt, v


def frame(cmd_id, field_no, payload=b""):
    body = b"\x08" + varint(cmd_id) + varint(field_no << 3 | 2) + varint(len(payload)) + payload
    return varint(len(body)) + body


def input_event(cmd_id, key, itype):
    payload = b"\x08" + varint(key) + b"\x10" + varint(itype)
    return frame(cmd_id, 23, payload)  # gui_send_input_event_request


def to_image(data, scale=3):
    img = Image.new("RGB", (128, 64), LCD_BG)
    px = img.load()
    for y in range(64):
        for x in range(128):
            if data[(y // 8) * 128 + x] >> (y % 8) & 1:
                px[x, y] = LCD_INK
    return img.resize((128 * scale, 64 * scale), Image.NEAREST)


class Rpc:
    def __init__(self, port="COM5"):
        self.ser = serial.Serial(port, 230400, timeout=0.3)
        self.ser.reset_input_buffer()
        self.ser.write(b"\r")
        time.sleep(0.3)
        self.ser.reset_input_buffer()
        self.ser.write(b"start_rpc_session\r")
        time.sleep(0.3)
        self.ser.read(self.ser.in_waiting)
        self.buf = b""
        self.cmd = 1
        self.last = None
        self.ser.write(frame(self.cmd, 20))  # start screen stream
        self.cmd += 1

    def pump(self, seconds=0.5):
        end = time.time() + seconds
        while time.time() < end:
            self.buf += self.ser.read(self.ser.in_waiting or 1)
            while True:
                try:
                    ln, i = read_varint(self.buf, 0)
                except IndexError:
                    break
                if len(self.buf) < i + ln:
                    break
                msg, self.buf = self.buf[i:i + ln], self.buf[i + ln:]
                for no, _, v in fields(msg):
                    if no == 22:
                        for fno, _, fv in fields(v):
                            if fno == 1 and len(fv) == 1024:
                                self.last = bytes(fv)

    def press(self, key):
        long = key.endswith("_long")
        name = key[:-5] if long else key
        k = KEYS[name]
        seq = (PRESS, LONG, RELEASE) if long else (PRESS, SHORT, RELEASE)
        for t in seq:
            self.ser.write(input_event(self.cmd, k, t))
            self.cmd += 1
            time.sleep(0.03)
        self.pump(0.5)

    def save(self, path):
        if self.last:
            to_image(self.last).save(path)

    def close(self):
        self.ser.write(frame(self.cmd, 21))
        self.ser.write(frame(self.cmd + 1, 19))
        time.sleep(0.2)
        self.ser.close()


def main():
    out = sys.argv[1]
    keys = sys.argv[2:]
    os.makedirs(out, exist_ok=True)
    r = Rpc()
    r.pump(0.8)
    r.save(os.path.join(out, "00_start.png"))
    for n, key in enumerate(keys, 1):
        r.press(key)
        r.save(os.path.join(out, f"{n:02d}_{key}.png"))
    r.close()
    print(f"{len(keys)} keys, frames in {out}")


if __name__ == "__main__":
    main()
