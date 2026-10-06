"""ctOS breach terminal (CYPRUS): scan -> bypass -> ACCESS GRANTED over a lit skyline."""
from PIL import ImageDraw

from .core import Anim, INK, PAPER, W, H, TOP, canvas, pen, tiny, tiny_w, invert, rng_for

SKY_H = [59, 55, 57, 52, 60, 51, 56, 54, 59, 52, 57, 55, 59, 54, 59]


def skyline(d):
    pts = [(0, 64)]
    step = W / (len(SKY_H) - 1)
    for i, h in enumerate(SKY_H):
        x = int(i * step)
        pts.append((x, h))
        pts.append((min(W, int((i + 1) * step)), h))
    pts.append((W, 64))
    d.polygon(pts, fill=INK)
    for wx in range(6, W, 11):
        for wy in range(60, 64, 3):
            d.point((wx, wy), fill=PAPER)


def bar(d, pct):
    d.rectangle([4, 41, 124, 45], outline=INK, width=1)
    fillw = int((pct / 100) * 118)
    if fillw > 0:
        d.rectangle([5, 42, 5 + fillw, 44], fill=INK)


def screen(lines, pct, fx, rng):
    img = canvas(PAPER)
    d = pen(img)
    d.rectangle([0, TOP, W, TOP + 8], fill=INK)
    tiny(img, 4, TOP + 2, "SYS // CYPRUS", PAPER)
    tiny(img, W - 4 - tiny_w("CTOS"), TOP + 2, "CTOS", PAPER)
    skyline(d)
    y = TOP + 13
    for ln in lines:
        if "GRANTED" in ln:
            d.rectangle([2, y - 2, 5 + tiny_w(ln), y + 6], fill=INK)
            tiny(img, 4, y, ln, PAPER)
        else:
            tiny(img, 4, y, ln, INK)
        y += 9
    bar(d, pct)
    if fx == "invert":
        img = invert(img)
    elif fx == "tear":
        d = ImageDraw.Draw(img)
        for _ in range(3):
            yy = rng.randint(TOP + 10, 44)
            off = rng.choice((-6, 6))
            d.rectangle([max(0, off), yy, W, yy + 1], fill=INK)
    return img


SEQ = [  # (lines, progress %, effect, ticks @ 2 fps)
    (["> SCANNING GRID_"], 10, None, 2),
    (["> SCANNING GRID..."], 30, None, 2),
    (["> SCANNING GRID...", "> BYPASS LOCK_"], 55, None, 2),
    (["> SCANNING GRID...", "> BYPASS LOCK..."], 78, None, 2),
    (["> BYPASS LOCK...", "> ACCESS GRANTED"], 100, None, 4),
    (["> BYPASS LOCK...", "> ACCESS GRANTED"], 100, "tear", 1),
    (["> ACCESS GRANTED"], 100, "invert", 1),
    (["> ACCESS GRANTED_"], 100, None, 2),
    (["> ACCESS GRANTED"], 100, None, 1),
    (["> ACCESS GRANTED_"], 100, None, 3),
]


def build():
    a = Anim("WD_Breach_128x64", "ctOS breach terminal")
    rng = rng_for(a.name)
    for lines, pct, fx, t in SEQ:
        a.add(screen(lines, pct, fx, rng), t)
    return a
