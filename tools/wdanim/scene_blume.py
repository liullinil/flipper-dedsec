"""Blume / ctOS boot: hex-cube mark draws itself over a noisy city waveform, BLUME, ctOS online."""
import math

from .core import (Anim, INK, PAPER, W, H, canvas, pen, tiny, tiny_w, tear, invert,
                   shift_band, rng_for)

CX, CY, R = 64, 28, 14
WAVE_Y = 38


def hex_pts(cx, cy, r):
    return [(cx + r * math.sin(math.radians(60 * k)), cy - r * math.cos(math.radians(60 * k)))
            for k in range(6)]


def _wave(rng):
    """Sparse, dim (dotted) vertical bars - a city skyline / audio trace."""
    cols = []
    level = 2
    for x in range(W):
        level = max(0, min(9, level + rng.choice((-2, -1, 0, 0, 1, 2))))
        spike = rng.random() < 0.06
        cols.append(level + (rng.randint(3, 7) if spike else 0))
    return cols


def _draw_wave(img, cols, rng):
    """Grainy horizon: dotted columns, no solid base line."""
    px = img.load()
    for x, hgt in enumerate(cols):
        for k in range(hgt):
            y = WAVE_Y - k
            if (x + y) % 2 == 0 and rng.random() < 0.8:
                px[x, y] = PAPER
        if rng.random() < 0.35:
            px[x, WAVE_Y + 1 + rng.randint(0, 2)] = PAPER


def _mark(img, edges=6, spokes=False, inner=False):
    """Cube with a cube-shaped notch in its front corner (nested isometric outlines)."""
    d = pen(img)
    v = [(round(x), round(y)) for x, y in hex_pts(CX, CY, R)]
    vi = [(round(x), round(y)) for x, y in hex_pts(CX, CY, R / 2)]
    d.polygon(v, fill=INK)  # the wave runs behind the logo
    for k in range(edges):
        d.line([v[k], v[(k + 1) % 6]], fill=PAPER)
    if spokes:
        for k in (1, 3, 5):
            d.line([v[k], vi[k]], fill=PAPER)
    if inner:
        for k in range(6):
            d.line([vi[k], vi[(k + 1) % 6]], fill=PAPER)
        for k in (0, 2, 4):
            d.line([(CX, CY), vi[k]], fill=PAPER)
    return img


def _frame(wave, rng, edges=0, spokes=False, inner=False, word="", status=""):
    img = canvas(INK)
    _draw_wave(img, wave, rng)
    if edges:
        _mark(img, edges, spokes, inner)
    if word:
        tiny(img, (W - tiny_w("BLUME", 5)) // 2, 47, word, PAPER, gap=5)
    if status:
        tiny(img, (W - tiny_w("CTOS 2.0 // ONLINE")) // 2, 57, status, PAPER)
    return img


def build():
    a = Anim("WD_Blume_ctOS_128x64", "Blume hex mark and ctOS boot", band=INK)
    rng = rng_for(a.name)
    waves = [_wave(rng) for _ in range(3)]

    def fr(i, **kw):
        return _frame(waves[i % 3], rng_for(f"w{i % 3}"), **kw)

    a.add(fr(0), 1)
    a.add(fr(1), 1)
    a.add(fr(2, edges=2), 1)
    a.add(fr(0, edges=4), 1)
    a.add(fr(1, edges=6), 1)
    a.add(fr(2, edges=6, spokes=True), 1)
    full = dict(edges=6, spokes=True, inner=True)
    for i in range(4):
        a.add(fr(i, **full), 1)
    for i, word in enumerate(("B", "BLU", "BLUME")):
        a.add(fr(i + 1, word=word, **full), 1)
    for i in range(4):
        a.add(fr(i, word="BLUME", **full), 1)
    for i, st in enumerate(("CTOS 2.0", "CTOS 2.0 // ONLINE")):
        a.add(fr(i + 1, word="BLUME", status=st, **full), 1)
    for i in range(4):
        a.add(fr(i, word="BLUME", status="CTOS 2.0 // ONLINE", **full), 1)
    last = fr(0, word="BLUME", status="CTOS 2.0 // ONLINE", **full)
    a.add(tear(last, rng, bands=7, maxdx=9, bg=INK), 1)
    a.add(shift_band(invert(last), 20, 36, 4, PAPER), 1)
    a.add(fr(2), 1)
    return a
