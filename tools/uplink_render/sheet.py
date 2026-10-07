"""Turn the rendered PBM frames into LCD-coloured PNGs: a contact sheet of every screen
(out/all_*.png) and the images used in the documentation (docs/uplink_*.png)."""
import glob
import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
DOCS = os.path.join(HERE, "..", "..", "docs")
BG, INK, GAP = (255, 152, 32), (28, 20, 8), (14, 14, 14)
SCALE = 3


def load(name):
    im = Image.open(os.path.join(OUT, name + ".pbm")).convert("1")
    if "_v_" in name:  # vertical frames are stored as the panel sees them
        im = im.transpose(Image.ROTATE_270)
    rgb = Image.new("RGB", im.size, BG)
    rgb.paste(Image.new("RGB", im.size, INK), mask=Image.eval(im.convert("L"), lambda v: 255 - v))
    return rgb.resize((im.size[0] * SCALE, im.size[1] * SCALE), Image.NEAREST)


def sheet(names, path, cols, label=False):
    ims = [load(n) for n in names]
    cw = max(i.size[0] for i in ims) + 8
    ch = max(i.size[1] for i in ims) + (22 if label else 8)
    rows = (len(ims) + cols - 1) // cols
    img = Image.new("RGB", (cols * cw + 8, rows * ch + 8), GAP)
    draw = ImageDraw.Draw(img)
    for k, (name, im) in enumerate(zip(names, ims)):
        x, y = 8 + (k % cols) * cw, 8 + (k // cols) * ch
        img.paste(im, (x, y))
        if label:
            draw.text((x, y + im.size[1] + 3), name, fill=(200, 200, 200))
    img.save(path)
    print(os.path.relpath(path, HERE), img.size)


def main():
    names = sorted(os.path.basename(p)[:-4] for p in glob.glob(os.path.join(OUT, "*.pbm")))
    for orient, cols in (("h", 4), ("v", 8)):
        group = [n for n in names if "_%s_" % orient in n]
        sheet(group, os.path.join(OUT, "all_%s.png" % orient), cols, label=True)
    sheet(
        ["sys_bars_h_normal", "cdx_list_h_normal", "cdx_detail_h_normal",
         "cmd_h_normal", "alert_approval_h_normal", "alert_update_h_normal"],
        os.path.join(DOCS, "uplink_screens.png"), 3)
    sheet(
        ["sys_bars_v_normal", "cdx_list_v_normal", "cdx_detail_v_normal", "cmd_v_normal",
         "rf_scout_v_normal", "offline_v_normal"],
        os.path.join(DOCS, "uplink_vertical.png"), 6)
    sheet(
        [f"{screen}_h_{size}" for screen in ("sys_bars", "cdx_list", "rf_scout")
         for size in ("micro", "small", "normal", "large")],
        os.path.join(DOCS, "uplink_fonts.png"), 4)
    sheet(["rf_scout_h_normal", "rf_follow_h_normal", "rf_nfc_h_normal", "rf_full_h_normal"],
          os.path.join(DOCS, "uplink_rf.png"), 2)


if __name__ == "__main__":
    main()
