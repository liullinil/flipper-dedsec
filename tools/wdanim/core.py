"""Core helpers for 1-bit 128x64 Flipper Zero dolphin animations.

Colour convention (same as the SDK converter): INK (0) is a lit/dark pixel on the
Flipper LCD, PAPER (255) is the backlit background.
"""
import os
import math
import random
import shutil

from PIL import Image, ImageDraw, ImageFont, ImageChops

W, H = 128, 64
INK, PAPER = 0, 255
FPS = 2                 # stock dolphin animations run at 2 fps
TARGET_SHOW_S = 30      # how long one animation stays on screen before the next is picked
FONT_DIR = r"C:\Windows\Fonts"
TOP = 13                # rows 0..12 sit under the desktop status bar (icons + battery):
CY = (TOP + H) // 2     # keep them plain background and centre content in rows 13..63

# ---------------------------------------------------------------- canvas / text
_fonts = {}


def font(name, size):
    key = (name, size)
    if key not in _fonts:
        _fonts[key] = ImageFont.truetype(os.path.join(FONT_DIR, name), size)
    return _fonts[key]


def canvas(bg=PAPER):
    return Image.new("1", (W, H), bg)


def pen(img):
    d = ImageDraw.Draw(img)
    d.fontmode = "1"
    return d


def text_size(s, f):
    x0, y0, x1, y1 = f.getbbox(s)
    return x1 - x0, y1 - y0


def put_text(img, x, y, s, f, fill=INK):
    """Draw text with the top-left of its ink box at (x, y)."""
    x0, y0, _, _ = f.getbbox(s)
    pen(img).text((x - x0, y - y0), s, font=f, fill=fill)


def center_text(img, y, s, f, fill=INK, cx=W // 2):
    w, _ = text_size(s, f)
    put_text(img, cx - w // 2, y, s, f, fill)


def text_mask(s, f, pad=0):
    """Tight 1-bit mask image of a string: INK where glyphs are."""
    w, h = text_size(s, f)
    img = Image.new("1", (w + 2 * pad, h + 2 * pad), PAPER)
    put_text(img, pad, pad, s, f)
    return img


# 3x5 pixel font for tiny HUD labels
TINY = {
    "A": "111101111101101", "B": "110101110101110", "C": "111100100100111",
    "D": "110101101101110", "E": "111100110100111", "F": "111100110100100",
    "G": "111100101101111", "H": "101101111101101", "I": "111010010010111",
    "J": "001001001101111", "K": "101110100110101", "L": "100100100100111",
    "M": "101111111101101", "N": "110101101101101", "O": "111101101101111",
    "P": "111101111100100", "Q": "111101101111001", "R": "110101110101101",
    "S": "111100111001111", "T": "111010010010010", "U": "101101101101111",
    "V": "101101101101010", "W": "101101111111101", "X": "101101010101101",
    "Y": "101101010010010", "Z": "111001010100111",
    "0": "111101101101111", "1": "010110010010111", "2": "111001111100111",
    "3": "111001111001111", "4": "101101111001001", "5": "111100111001111",
    "6": "111100111101111", "7": "111001001010010", "8": "111101111101111",
    "9": "111101111001111",
    ">": "100010001010100", "<": "001010100010001", ".": "000000000000010",
    "/": "001001010100100", " ": "000000000000000", "_": "000000000000111",
    "!": "010010010000010", ":": "000010000010000", "-": "000000111000000",
    "[": "110100100100110", "]": "011001001001011", "#": "101111101111101",
    "+": "000010111010000", "=": "000111000111000", "%": "101001010100101",
    "|": "010010010010010", "?": "111001011000010", "x": "000101010101000",
}


def tiny(img, x, y, s, fill=INK, gap=1):
    d = pen(img)
    cx = x
    for ch in s:
        pat = TINY.get(ch, TINY[" "])
        for i, bit in enumerate(pat):
            if bit == "1":
                d.point((cx + i % 3, y + i // 3), fill=fill)
        cx += 3 + gap
    return cx


def tiny_w(s, gap=1):
    return len(s) * (3 + gap) - gap if s else 0


def tiny_center(img, y, s, fill=INK, gap=1, cx=W // 2):
    tiny(img, cx - tiny_w(s, gap) // 2, y, s, fill, gap)


# ---------------------------------------------------------------- effects
def invert(img):
    return ImageChops.invert(img.convert("L")).convert("1", dither=Image.Dither.NONE)


def bg_of(img):
    """Guess background colour from the four corners."""
    px = [img.getpixel(p) for p in ((0, 0), (W - 1, 0), (0, H - 1), (W - 1, H - 1))]
    return PAPER if sum(1 for p in px if p) >= 2 else INK


def shift_band(img, y0, y1, dx, bg=None):
    """Shift rows [y0, y1) sideways by dx - the classic WD 'cut' glitch."""
    if bg is None:
        bg = bg_of(img)
    y0, y1 = max(0, y0), min(H, y1)
    if y1 <= y0 or dx == 0:
        return img.copy()
    band = img.crop((0, y0, W, y1))
    out = img.copy()
    pen(out).rectangle([0, y0, W - 1, y1 - 1], fill=bg)
    out.paste(band, (dx, y0))
    return out


def tear(img, rng, bands=4, maxdx=6, y0=0, y1=H, hmin=1, hmax=4, bg=None):
    out = img
    for _ in range(bands):
        h = rng.randint(hmin, hmax)
        y = rng.randint(y0, max(y0, y1 - h))
        dx = rng.choice((-1, 1)) * rng.randint(1, maxdx)
        out = shift_band(out, y, y + h, dx, bg)
    return out


def invert_box(img, x0, y0, x1, y1):
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
    if x1 <= x0 or y1 <= y0:
        return img.copy()
    out = img.copy()
    out.paste(invert(img.crop((x0, y0, x1, y1))), (x0, y0))
    return out


def shift_columns(img, rng, n, maxdy=3, x0=0, x1=W, wmax=2, bg=None):
    """Vertical column displacement (Legion-style streak glitch)."""
    if bg is None:
        bg = bg_of(img)
    out = img.copy()
    for _ in range(n):
        w = rng.randint(1, wmax)
        x = rng.randint(x0, max(x0, x1 - w))
        dy = rng.choice((-1, 1)) * rng.randint(1, maxdy)
        col = img.crop((x, 0, x + w, H))
        pen(out).rectangle([x, 0, x + w - 1, H - 1], fill=bg)
        out.paste(col, (x, dy))
    return out


def speckle(img, rng, n, box=(0, 0, W, H), color=INK):
    d = pen(img)
    for _ in range(n):
        d.point((rng.randrange(box[0], box[2]), rng.randrange(box[1], box[3])), fill=color)


def dashes(img, rng, n, box=(0, 0, W, H), color=INK, lmin=2, lmax=14):
    """Short horizontal static streaks."""
    d = pen(img)
    for _ in range(n):
        y = rng.randrange(box[1], box[3])
        x = rng.randrange(box[0], box[2])
        d.line([x, y, min(box[2] - 1, x + rng.randint(lmin, lmax)), y], fill=color)


BAYER4 = ((0, 8, 2, 10), (12, 4, 14, 6), (3, 11, 1, 9), (15, 7, 13, 5))
CLUSTER4 = ((12, 5, 6, 13), (4, 0, 1, 7), (11, 3, 2, 8), (15, 10, 9, 14))


def on_matrix(x, y, level, m=BAYER4):
    """True if an ordered-dither cell at (x, y) is set for coverage `level` (0..1)."""
    return (m[y % 4][x % 4] + 0.5) / 16.0 < level


def dissolve(img, level, color=None, m=BAYER4, ox=0, oy=0):
    """Paint `level` share of pixels with `color` in an ordered pattern (fade in/out)."""
    if color is None:
        color = bg_of(img)
    out = img.copy()
    px = out.load()
    for y in range(H):
        for x in range(W):
            if on_matrix(x + ox, y + oy, level, m):
                px[x, y] = color
    return out


def blend_mask(base, top, mask):
    """Paste `top` over `base` where mask pixel is INK."""
    return Image.composite(base, top, mask)


def poly_line(d, pts, fill=INK, width=1):
    for a, b in zip(pts, pts[1:]):
        d.line([a, b], fill=fill, width=width)


def thick_point(d, x, y, r, fill=INK):
    if r <= 0.5:
        d.point((round(x), round(y)), fill=fill)
    else:
        d.ellipse([x - r, y - r, x + r, y + r], fill=fill)


def brush_line(d, a, b, rng, w0=2.0, w1=2.0, jitter=0.4, fill=INK, step=0.5):
    """Hand-drawn stroke: radius tapers from w0 to w1 with slight jitter."""
    (x0, y0), (x1, y1) = a, b
    n = max(1, int(math.hypot(x1 - x0, y1 - y0) / step))
    for i in range(n + 1):
        t = i / n
        r = (w0 + (w1 - w0) * t) / 2 + rng.uniform(-jitter, jitter) / 2
        thick_point(d, x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, max(0.0, r), fill)


# ---------------------------------------------------------------- timeline
class Anim:
    """Ordered list of (frame, ticks). Identical frames are stored once."""

    def __init__(self, name, title="", fps=FPS, band=PAPER):
        self.name = name
        self.title = title
        self.fps = fps
        self.band = band        # colour of the strip under the status bar
        self.steps = []

    def add(self, img, ticks=1):
        assert img.size == (W, H)
        img = img.convert("1", dither=Image.Dither.NONE).copy()
        pen(img).rectangle([0, 0, W - 1, TOP - 1], fill=self.band)
        self.steps.append((img, ticks))
        return img

    @property
    def ticks(self):
        return sum(t for _, t in self.steps)

    def frames_and_order(self):
        uniq, keys, order = [], {}, []
        for img, t in self.steps:
            k = img.tobytes()
            if k not in keys:
                keys[k] = len(uniq)
                uniq.append(img)
            order += [keys[k]] * t
        return uniq, order

    def loop_seconds(self):
        return self.ticks / self.fps

    def duration(self, target=TARGET_SHOW_S):
        """Whole number of loops close to `target` seconds, so the switch lands on a loop end."""
        loop = self.loop_seconds()
        k = max(1, round(target / loop))
        return int(round(k * loop))


# ---------------------------------------------------------------- packing
def _converter():
    import sys
    sys.path.insert(0, os.path.expanduser(r"~\.ufbt\current\scripts"))
    from flipper.assets.icon import file2image  # noqa: E402
    return file2image


def pack(anim, dolphin_dir, frames_dir):
    """Write frame_N.bm + meta.txt (for the SD card) and frame_N.png (for review)."""
    file2image = _converter()
    if anim.ticks % 2 and anim.fps == 2:
        img, t = anim.steps[-1]
        anim.steps[-1] = (img, t + 1)  # whole seconds per loop
    uniq, order = anim.frames_and_order()
    assert 2 <= len(uniq) <= 256, (anim.name, len(uniq))
    assert len(order) <= 255, (anim.name, len(order))
    adir = os.path.join(dolphin_dir, anim.name)
    fdir = os.path.join(frames_dir, anim.name)
    for p in (adir, fdir):
        if os.path.isdir(p):
            shutil.rmtree(p)
        os.makedirs(p)
    size = 0
    for i, img in enumerate(uniq):
        png = os.path.join(fdir, f"frame_{i}.png")
        img.save(png)
        bm = os.path.join(adir, f"frame_{i}.bm")
        file2image(png).write(bm)
        size += os.path.getsize(bm)
    meta = (
        "Filetype: Flipper Animation\nVersion: 1\n\n"
        f"Width: {W}\nHeight: {H}\n"
        f"Passive frames: {len(order)}\nActive frames: 0\n"
        f"Frames order: {' '.join(map(str, order))}\n"
        "Active cycles: 0\n"
        f"Frame rate: {anim.fps}\n"
        f"Duration: {anim.duration()}\n"
        "Active cooldown: 0\n\nBubble slots: 0\n"
    )
    with open(os.path.join(adir, "meta.txt"), "w", newline="\n") as fh:
        fh.write(meta)
    return {"name": anim.name, "unique": len(uniq), "ticks": len(order),
            "loop_s": anim.loop_seconds(), "duration": anim.duration(), "bytes": size}


# ---------------------------------------------------------------- previews
LCD_BG, LCD_INK = (255, 152, 32), (28, 20, 8)


_bar = None


def with_status_bar(img):
    """Overlay the desktop status bar the way the firmware draws it (preview only)."""
    global _bar
    if _bar is None:
        _bar = Image.open(os.path.join(os.path.dirname(__file__), "statusbar.png")).convert("L")
    out = img.copy()
    px, bp = out.load(), _bar.load()
    for y in range(_bar.height):
        for x in range(W):
            v = bp[x, y]
            if v != 128:
                px[x, y] = INK if v == 0 else PAPER
    return out


def screenify(img, scale=3, bar=True):
    if bar:
        img = with_status_bar(img)
    rgb = Image.new("RGB", (W, H), LCD_BG)
    rgb.paste(Image.new("RGB", (W, H), LCD_INK), mask=invert(img).convert("L"))
    return rgb.resize((W * scale, H * scale), Image.NEAREST)


def save_gif(anim, path, scale=3):
    frames, durs = [], []
    for img, t in anim.steps:
        ms = int(t * 1000 / anim.fps)
        if frames and img.tobytes() == frames[-1][1]:
            durs[-1] += ms
            continue
        frames.append((screenify(img, scale), img.tobytes()))
        durs.append(ms)
    pics = [f for f, _ in frames]
    pics[0].save(path, save_all=True, append_images=pics[1:], loop=0, duration=durs, disposal=2)


def contact_sheet(anim, path, cols=4, scale=2):
    uniq, _ = anim.frames_and_order()
    pad = 4
    rows = (len(uniq) + cols - 1) // cols
    gw, gh = W * scale, H * scale
    sheet = Image.new("RGB", (cols * gw + (cols + 1) * pad, rows * gh + (rows + 1) * pad), (16, 16, 16))
    for i, img in enumerate(uniq):
        r, c = divmod(i, cols)
        sheet.paste(screenify(img, scale), (pad + c * (gw + pad), pad + r * (gh + pad)))
    sheet.save(path)


def showreel(anims, path, scale=3):
    """One GIF that plays one loop of every animation in turn."""
    pics, durs = [], []
    for a in anims:
        last = None
        for img, t in a.steps:
            ms = int(t * 1000 / a.fps)
            key = img.tobytes()
            if key == last:
                durs[-1] += ms
                continue
            pics.append(screenify(img, scale))
            durs.append(ms)
            last = key
    pics[0].save(path, save_all=True, append_images=pics[1:], loop=0, duration=durs, disposal=2)


def rng_for(name):
    return random.Random(name)
