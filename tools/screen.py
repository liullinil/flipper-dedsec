"""Watch the Flipper's screen over USB (the same RPC screen stream qFlipper uses).

    python tools/screen.py [seconds] [out_dir] [COM5]

Saves every distinct screen as a PNG (LCD colours, 3x) with its timestamp, plus capture.gif
with real timings. Handy to confirm what the desktop is really showing and when it changes.
"""
import os
import sys
import time

import serial
from PIL import Image

LCD_BG, LCD_INK = (255, 152, 32), (28, 20, 8)


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
        if i >= len(buf):
            raise IndexError
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return val, i


def fields(msg):
    """Yield (field_no, wire_type, value) for a protobuf message."""
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
        elif wt == 1:
            v, i = msg[i:i + 8], i + 8
        else:
            raise ValueError(f"wire type {wt}")
        yield no, wt, v


def request(cmd_id, field_no, payload=b""):
    body = b"\x08" + varint(cmd_id) + varint(field_no << 3 | 2) + varint(len(payload)) + payload
    return varint(len(body)) + body


def to_image(data, scale=3):
    img = Image.new("RGB", (128, 64), LCD_BG)
    px = img.load()
    for y in range(64):
        for x in range(128):
            if data[(y // 8) * 128 + x] >> (y % 8) & 1:
                px[x, y] = LCD_INK
    return img.resize((128 * scale, 64 * scale), Image.NEAREST)


class ScreenStream:
    def __init__(self, port="COM5"):
        self.ser = serial.Serial(port, 230400, timeout=0.2)
        self.ser.reset_input_buffer()
        self.ser.write(b"\r")
        time.sleep(0.3)
        self.ser.reset_input_buffer()
        self.ser.write(b"start_rpc_session\r")
        time.sleep(0.3)
        self.ser.read(self.ser.in_waiting)          # drop the CLI echo
        self.buf = b""
        self.ser.write(request(1, 20))             # gui_start_screen_stream_request

    def frames(self):
        while True:
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
                    if no == 22:                       # gui_screen_frame
                        for fno, _, fv in fields(v):
                            if fno == 1 and len(fv) == 1024:
                                yield bytes(fv)

    def close(self):
        try:
            self.ser.write(request(2, 21))         # stop screen stream
            self.ser.write(request(3, 19))         # stop rpc session
            time.sleep(0.3)
        finally:
            self.ser.close()


def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 10
    out = sys.argv[2] if len(sys.argv) > 2 else "screen_capture"
    port = sys.argv[3] if len(sys.argv) > 3 else "COM5"
    os.makedirs(out, exist_ok=True)
    st = ScreenStream(port)
    t0 = time.time()
    shots, last = [], None
    try:
        for data in st.frames():
            t = time.time() - t0
            if data != last:
                shots.append((t, data))
                last = data
            if t > secs:
                break
    finally:
        st.close()
    pics, durs = [], []
    for k, (t, data) in enumerate(shots):
        img = to_image(data)
        img.save(os.path.join(out, f"{k:03d}_{t:06.1f}s.png"))
        nxt = shots[k + 1][0] if k + 1 < len(shots) else secs
        pics.append(img)
        durs.append(max(20, int((nxt - t) * 1000)))
    if pics:
        pics[0].save(os.path.join(out, "capture.gif"), save_all=True, append_images=pics[1:],
                     loop=0, duration=durs)
    print(f"{len(shots)} distinct screens in {secs:.0f}s -> {out}")


if __name__ == "__main__":
    main()
