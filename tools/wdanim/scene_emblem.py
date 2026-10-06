"""Brush-ring W emblem: an ink circle sweeps around, then the W is scratched in."""
import math

from .core import (Anim, INK, PAPER, W, H, canvas, pen, tear, invert, dissolve, brush_line,
                   thick_point, rng_for)

CX, CY, R = 64, 38, 24
START = 35          # degrees clockwise from 12 o'clock where the brush lands
SWEEP = 345

STROKES = [
    ((49, 24), (50, 49)),            # left upright
    ((77, 21), (77, 47)),            # right upright
    ((49, 24), (64, 58)),            # big V, left arm
    ((64, 58), (77, 21)),            # big V, right arm
    ((50, 49), (63, 27)),            # inner peak, left
    ((63, 27), (77, 47)),            # inner peak, right
    ((48, 50), (85, 41)),            # the slash
]


def ring(img, share, rng):
    d = pen(img)
    n = int(SWEEP * share)
    for i in range(n + 1):
        t = i / SWEEP
        ang = math.radians(START + i)
        # brush pressure: thin landing, fat body, dry tapering tail
        w = 1.0 + 2.4 * min(1.0, t * 6) * (1.0 - max(0.0, (t - 0.8) / 0.2) * 0.85)
        w += rng.uniform(-0.3, 0.3)
        thick_point(d, CX + R * math.sin(ang), CY - R * math.cos(ang), w / 2, PAPER)
    # bristle gaps near the tail
    if share > 0.9:
        for i in range(8):
            ang = math.radians(START + SWEEP - 30 + i * 4)
            d.point((round(CX + (R + 1) * math.sin(ang)), round(CY - (R + 1) * math.cos(ang))),
                    fill=INK)
    return img


def strokes(img, n, rng_seed):
    d = pen(img)
    for k, (a, b) in enumerate(STROKES[:n]):
        r = rng_for(f"{rng_seed}{k}")
        brush_line(d, a, b, r, w0=1.8, w1=0.9, jitter=0.4, fill=PAPER)
    return img


def frame(share=1.0, n=len(STROKES)):
    img = canvas(INK)
    ring(img, share, rng_for("ring"))
    strokes(img, n, "w")
    return img


def build():
    a = Anim("WD_Emblem_128x64", "Brush ring W emblem", band=INK)
    rng = rng_for(a.name)
    a.add(canvas(INK), 1)
    for share in (0.25, 0.5, 0.75):
        a.add(frame(share, 0), 1)
    a.add(frame(1.0, 0), 1)
    a.add(frame(1.0, 2), 1)
    a.add(frame(1.0, 4), 1)
    full = frame()
    a.add(full, 5)
    a.add(tear(full, rng, bands=5, maxdx=6, bg=INK), 1)
    a.add(full, 3)
    a.add(invert(full), 1)
    a.add(full, 3)
    a.add(dissolve(full, 0.5, INK), 1)
    a.add(dissolve(full, 0.85, INK), 1)
    return a
