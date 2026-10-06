"""DEDSEC decrypt: tall pixel glitch letters resolve out of mirrored junk glyphs."""
from PIL import Image

from .core import (Anim, INK, PAPER, W, H, TOP, canvas, pen, tiny, tear, invert, shift_band,
                   rng_for)

CELL = 3
GLYPHS = {
    "D": ["####.", "#..##", "#...#", "#...#", "#...#", "#...#",
          "#...#", "#...#", "#...#", "#..##", "####."],
    "E": ["#####", "#....", "#....", "#....", "####.", "#....",
          "#....", "#....", "#....", "#....", "#####"],
    "S": [".####", "#....", "#....", "#....", ".###.", "....#",
          "....#", "....#", "....#", "....#", "####."],
    "C": [".####", "#....", "#....", "#....", "#....", "#....",
          "#....", "#....", "#....", "#....", ".####"],
}
WORD = "DEDSEC"
GW, GH = 5 * CELL, 11 * CELL
GAP = 4
X0 = (W - (len(WORD) * GW + (len(WORD) - 1) * GAP)) // 2
Y0 = 14
LIFT = (0, 2, -1, 1, 0, 2)          # ragged baseline per letter


def glyph_img(rows, rng, cuts=2):
    img = Image.new("1", (GW + 6, GH), PAPER)
    d = pen(img)
    shifts = [0] * len(rows)
    for _ in range(cuts):
        if cuts == 1:
            shifts[rng.randrange(3, 8)] = rng.choice((-1, 2))
        else:
            shifts[rng.randrange(len(rows))] = rng.choice((-2, 2, 3))
    for r, row in enumerate(rows):
        for c, ch in enumerate(row):
            if ch == "#":
                x = 3 + c * CELL + shifts[r]
                d.rectangle([x, r * CELL, x + CELL - 1, r * CELL + CELL - 1], fill=INK)
    return img


def junk(rng):
    rows = list(GLYPHS[rng.choice("DESC")])
    if rng.random() < 0.6:
        rows = [r[::-1] for r in rows]
    if rng.random() < 0.4:
        rows = rows[::-1]
    return rows


def word_frame(resolved, rng, seed_junk, bottom=""):
    img = canvas(PAPER)
    jr = rng_for(f"junk{seed_junk}")
    for i, ch in enumerate(WORD):
        rows = GLYPHS[ch] if i < resolved else junk(jr)
        g = glyph_img(rows, rng_for(f"g{i}{'ok' if i < resolved else seed_junk}"),
                      1 if i < resolved else 3)
        img.paste(Image.composite(Image.new("1", g.size, INK), img.crop(
            (X0 + i * (GW + GAP) - 3, Y0 + LIFT[i], X0 + i * (GW + GAP) - 3 + g.width,
             Y0 + LIFT[i] + g.height)), g.convert("L").point(lambda p: 255 - p)),
            (X0 + i * (GW + GAP) - 3, Y0 + LIFT[i]))
    if bottom:
        tiny(img, X0, 56, bottom)
    return img


def build():
    a = Anim("WD_DedSec_Decrypt_128x64", "DEDSEC decrypt")
    rng = rng_for(a.name)
    a.add(word_frame(0, rng, 1, "DECRYPTING"), 2)
    for n in range(1, 6):
        a.add(word_frame(n, rng, n + 1, "DECRYPTING" + "." * (n % 4)), 1)
    clean = word_frame(6, rng, 0)
    on, off = clean.copy(), clean.copy()
    tiny(on, X0, 56, "> JOIN US_")
    tiny(off, X0, 56, "> JOIN US")
    a.add(on, 4)
    a.add(tear(on, rng, bands=5, maxdx=7, y0=Y0, y1=Y0 + GH + 3, hmin=2, hmax=4, bg=PAPER), 1)
    a.add(off, 2)
    a.add(on, 2)
    a.add(invert(off), 1)
    a.add(off, 2)
    a.add(shift_band(tear(on, rng, bands=9, maxdx=12, y0=TOP, bg=PAPER), 27, 33, 9, PAPER), 1)
    a.add(word_frame(0, rng, 7, "DECRYPTING"), 1)
    return a
