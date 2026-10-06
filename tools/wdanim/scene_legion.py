"""Legion-style W: tall streaky emblem assembling out of vertical glitch rain, HUD bits."""
from PIL import Image

from .core import (Anim, INK, PAPER, W, H, TOP, canvas, pen, tiny, tear, invert, shift_columns,
                   rng_for)

# emblem geometry: two outer uprights, a big V and an inner peak
LV = ((44, 15), (46, 50))
RV = ((84, 14), (82, 50))
V = ((44, 15), (64, 62), (84, 14))
PEAK = ((46, 50), (64, 23), (82, 50))


def emblem(rng, streaks=40):
    img = canvas(INK)
    d = pen(img)
    for a, b in (LV, RV):
        d.line([a, b], fill=PAPER, width=2)
    d.line(list(V), fill=PAPER, width=2)
    d.line(list(PEAK), fill=PAPER, width=2)
    # vertical paint streaks hanging off the strokes
    px = img.load()
    lit = [(x, y) for y in range(H) for x in range(W) if px[x, y] == PAPER]
    for _ in range(streaks):
        x, y = rng.choice(lit)
        ln = rng.randint(2, 9)
        up = rng.random() < 0.4
        d.line([x, y, x, y - ln if up else y + ln], fill=PAPER)
    return img


def rain(rng, n=26, img=None):
    img = img or canvas(INK)
    d = pen(img)
    for _ in range(n):
        x = rng.randrange(W)
        y = rng.randrange(TOP, H)
        d.line([x, y, x, y + rng.randint(2, 10)], fill=PAPER)
    return img


def hud(img, label=True):
    d = pen(img)
    d.line([4, 52, 30, 52], fill=PAPER)
    d.line([4, 54, 18, 54], fill=PAPER)
    d.rectangle([56, 61, 70, 63], outline=PAPER)
    for cx in (101, 108):
        d.ellipse([cx - 2, 40, cx + 2, 44], outline=PAPER)
    d.line([93, 42, 97, 42], fill=PAPER)
    if label:
        tiny(img, 92, 56, "DEDSEC", PAPER)
    d.line([6, 17, 22, 17], fill=PAPER)
    tiny(img, 6, 20, "LDN", PAPER)
    return img


def reveal(full, rng, share):
    """Show only a random share of the emblem's columns."""
    out = canvas(INK)
    for x in range(W):
        if rng.random() < share:
            out.paste(full.crop((x, 0, x + 1, H)), (x, 0))
    return out


def build():
    a = Anim("WD_Legion_128x64", "Legion style W emblem", band=INK)
    rng = rng_for(a.name)
    base = emblem(rng)
    alt = emblem(rng_for("alt"), 55)
    a.add(rain(rng), 1)
    a.add(rain(rng, 14, reveal(base, rng, 0.3)), 1)
    a.add(rain(rng, 8, reveal(base, rng, 0.7)), 1)
    a.add(shift_columns(base, rng, 30, maxdy=5, x0=40, x1=88), 1)
    a.add(base, 4)
    a.add(shift_columns(alt, rng, 12, maxdy=3, x0=40, x1=88), 1)
    a.add(base, 2)
    a.add(tear(base, rng, bands=5, maxdx=6, bg=INK), 1)
    with_hud = hud(base.copy())
    a.add(hud(base.copy(), label=False), 1)
    a.add(with_hud, 4)
    a.add(hud(shift_columns(alt, rng, 16, maxdy=4, x0=40, x1=88)), 1)
    a.add(invert(with_hud), 1)
    a.add(with_hud, 2)
    a.add(rain(rng, 10, reveal(base, rng, 0.45)), 1)
    a.add(rain(rng), 1)
    return a
