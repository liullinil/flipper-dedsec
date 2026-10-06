"""Operator mask: two LED eye panels cycle through emoticons (slow, with blinks)."""
from .core import Anim, INK, PAPER, W, H, canvas, pen, tiny_center, rng_for

EYE_L, EYE_R = (38, 31), (90, 31)


def eye(d, c, kind, rng):
    cx, cy = c
    d.rounded_rectangle([cx - 20, cy - 13, cx + 20, cy + 13], radius=5, outline=PAPER, width=2)
    w = 4
    if kind == ">":
        d.line([cx - 8, cy - 7, cx + 6, cy], fill=PAPER, width=w)
        d.line([cx + 6, cy, cx - 8, cy + 7], fill=PAPER, width=w)
    elif kind == "<":
        d.line([cx + 8, cy - 7, cx - 6, cy], fill=PAPER, width=w)
        d.line([cx - 6, cy, cx + 8, cy + 7], fill=PAPER, width=w)
    elif kind == "+":
        d.line([cx, cy - 8, cx, cy + 8], fill=PAPER, width=w)
        d.line([cx - 9, cy, cx + 9, cy], fill=PAPER, width=w)
    elif kind == "-":
        d.line([cx - 9, cy, cx + 9, cy], fill=PAPER, width=w)
    elif kind == "^":
        d.line([cx - 8, cy + 6, cx, cy - 6], fill=PAPER, width=w)
        d.line([cx, cy - 6, cx + 8, cy + 6], fill=PAPER, width=w)
    elif kind == "o":
        d.ellipse([cx - 8, cy - 8, cx + 8, cy + 8], outline=PAPER, width=3)
    elif kind == "x":
        d.line([cx - 8, cy - 7, cx + 8, cy + 7], fill=PAPER, width=w)
        d.line([cx + 8, cy - 7, cx - 8, cy + 7], fill=PAPER, width=w)
    elif kind == "scan":
        for yy in range(cy - 10, cy + 11, 2):
            if rng.random() < 0.7:
                d.line([cx - 16 + rng.randint(0, 5), yy, cx + 16 - rng.randint(0, 5), yy],
                       fill=PAPER)


def face(le, re, rng):
    img = canvas(INK)
    d = pen(img)
    glitch = le == "scan"
    jx = rng.choice((-3, 3)) if glitch else 0
    eye(d, (EYE_L[0] + jx, EYE_L[1]), le, rng)
    eye(d, (EYE_R[0] - jx, EYE_R[1]), re, rng)
    if glitch:
        for _ in range(4):
            yy = rng.randint(14, 60)
            d.rectangle([0, yy, W, yy + 1], fill=PAPER)
    tiny_center(img, 52, "C Y P R U S", PAPER)
    return img


SEQ = [  # (left, right, ticks @ 2 fps)
    ("o", "o", 4), ("-", "-", 1), ("o", "o", 3),
    (">", "<", 4), ("-", "-", 1),
    ("^", "^", 4), ("scan", "scan", 1),
    ("x", "x", 3), ("scan", "scan", 1),
    ("+", "+", 3), ("-", "-", 1),
]


def build():
    a = Anim("WD_Operator_128x64", "LED eye mask", band=INK)
    rng = rng_for(a.name)
    for le, re, t in SEQ:
        a.add(face(le, re, rng), t)
    return a
