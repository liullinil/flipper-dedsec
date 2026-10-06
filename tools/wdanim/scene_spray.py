"""Spray-painted W with drips, then a black WATCH_DOGS 2 banner slams across it."""
from .core import (Anim, INK, PAPER, W, H, canvas, pen, font, text_size, put_text, tear,
                   invert, dissolve, shift_band, brush_line, speckle, rng_for)

STROKES = [
    ((40, 14), (44, 49)),            # left upright
    ((40, 14), (64, 62)),            # big V, left arm
    ((64, 62), (88, 13)),            # big V, right arm
    ((88, 13), (84, 49)),            # right upright
    ((44, 49), (64, 21)),            # inner peak, left
    ((64, 21), (84, 49)),            # inner peak, right
]
DRIPS = [((43, 49), 6), ((85, 48), 8), ((53, 36), 4), ((76, 37), 5), ((61, 56), 3)]
BANNER = (14, 31, 113, 43)


def spray(img, n, rng_seed):
    d = pen(img)
    for k, (a, b) in enumerate(STROKES[:n]):
        r = rng_for(f"{rng_seed}{k}")
        brush_line(d, a, b, r, w0=4.2, w1=3.3, jitter=0.9)
        # overspray mist around the stroke
        for _ in range(26):
            t = r.random()
            x = a[0] + (b[0] - a[0]) * t + r.uniform(-5, 5)
            y = a[1] + (b[1] - a[1]) * t + r.uniform(-3, 3)
            if 0 <= x < W and 0 <= y < H:
                d.point((round(x), round(y)), fill=INK)
    return img


def drips(img, share):
    d = pen(img)
    for (x, y), ln in DRIPS:
        end = y + max(1, round(ln * share))
        d.line([x, y, x, end], fill=INK)
        d.rectangle([x - 1, end - 1, x, end], fill=INK)      # bead at the tip
    return img


def banner(img, dx=0, glitch=None):
    x0, y0, x1, y1 = BANNER
    d = pen(img)
    d.rectangle([x0 + dx, y0, x1 + dx, y1], fill=INK)
    f = font("impact.ttf", 12)
    s = "WATCH_DOGS 2"
    w, h = text_size(s, f)
    put_text(img, (x0 + x1) // 2 - w // 2 + dx, y0 + 2, s, f, PAPER)
    # scuffed first letter like a torn sticker
    r = rng_for("scuff")
    sx = (x0 + x1) // 2 - w // 2 + dx
    for _ in range(14):
        d.point((sx + r.randint(0, 9), y0 + r.randint(2, 11)), fill=INK)
    if glitch:
        img = tear(img, glitch, bands=4, maxdx=5, y0=y0, y1=y1 + 1, hmin=1, hmax=3, bg=PAPER)
    return img


def build():
    a = Anim("WD2_Spray_128x64", "Spray W and WATCH_DOGS 2 banner")
    rng = rng_for(a.name)
    a.add(canvas(PAPER), 1)
    for n in (1, 3, 4, 6):
        a.add(spray(canvas(PAPER), n, "s"), 1)
    a.add(drips(spray(canvas(PAPER), 6, "s"), 0.5), 1)
    wall = drips(spray(canvas(PAPER), 6, "s"), 1.0)
    a.add(wall, 1)
    a.add(banner(wall.copy(), dx=-9, glitch=rng), 1)
    full = banner(wall.copy())
    a.add(full, 6)
    a.add(banner(wall.copy(), glitch=rng), 1)
    a.add(full, 3)
    a.add(invert(full), 1)
    a.add(full, 2)
    mist = full.copy()
    speckle(mist, rng, 160, color=PAPER)
    a.add(dissolve(mist, 0.5, PAPER), 1)
    a.add(dissolve(full, 0.85, PAPER), 1)
    return a
