"""Download the u8g2 sources the renderer compiles (pinned tag) into ./u8g2."""
import os
import urllib.request

TAG = "2.37.1"
FILES = [
    "u8g2.h", "u8x8.h",
    "u8g2_bitmap.c", "u8g2_box.c", "u8g2_buffer.c", "u8g2_circle.c", "u8g2_font.c",
    "u8g2_hvline.c", "u8g2_intersection.c", "u8g2_kerning.c", "u8g2_line.c",
    "u8g2_ll_hvline.c", "u8g2_setup.c",
    "u8x8_8x8.c", "u8x8_byte.c", "u8x8_cad.c", "u8x8_capture.c", "u8x8_display.c",
    "u8x8_gpio.c", "u8x8_setup.c", "u8x8_u16toa.c", "u8x8_u8toa.c",
]


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    out = os.path.join(here, "u8g2")
    os.makedirs(out, exist_ok=True)
    for name in FILES:
        path = os.path.join(out, name)
        if os.path.exists(path):
            continue
        url = "https://raw.githubusercontent.com/olikraus/u8g2/%s/csrc/%s" % (TAG, name)
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = resp.read()
        with open(path, "wb") as fh:
            fh.write(data)
        print("fetched", name)
    print("u8g2", TAG, "ready in", out)


if __name__ == "__main__":
    main()
