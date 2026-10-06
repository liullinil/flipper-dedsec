"""WATCH_DOGS glitch wordmark (white screen, heavy condensed letters, horizontal cuts)."""
from .core import (Anim, INK, PAPER, W, H, canvas, pen, font, text_size, put_text,
                   shift_band, tear, invert, dashes, speckle, tiny, tiny_w, rng_for)

WORD = "WATCH_DOGS"
TAG = "EVERYTHING IS CONNECTED"


def _base(top):
    f = font("impact.ttf", 22)
    w, h = text_size(WORD, f)
    img = canvas(PAPER)
    put_text(img, (W - w) // 2, top, WORD, f)
    return img, (W - w) // 2, w, h


def _signature(img, top, x0, w, rng):
    """The logo's own glitch: two thin slices pushed sideways + hairline streaks."""
    out = shift_band(img, top + 11, top + 12, 2, PAPER)
    out = shift_band(out, top + 14, top + 15, -1, PAPER)
    d = pen(out)
    for y, xs in ((top + 11, (0.12, 0.47, 0.83)), (top + 15, (0.30, 0.66))):
        for fx in xs:
            x = x0 + int(fx * w) + rng.randint(-3, 3)
            d.line([x, y, x + rng.randint(4, 8), y], fill=INK)
    return out


def _cursor(top, x_us, on):
    img = canvas(PAPER)
    if on:
        pen(img).rectangle([x_us, top + 18, x_us + 7, top + 19], fill=INK)
    return img


def build():
    a = Anim("WD_Wordmark_128x64", "WATCH_DOGS glitch wordmark")
    rng = rng_for(a.name)
    top = 21
    base, x0, w, h = _base(top)
    f = font("impact.ttf", 22)
    x_us = x0 + text_size("WATCH", f)[0] + 1
    clean = _signature(base, top, x0, w, rng)

    # underscore blinks like a cursor, then the wordmark tears into place
    a.add(_cursor(top, x_us, True), 2)
    a.add(_cursor(top, x_us, False), 1)
    a.add(_cursor(top, x_us, True), 2)
    burst = canvas(PAPER)
    dashes(burst, rng, 40, (0, top - 2, W, top + h + 2), lmin=3, lmax=22)
    a.add(burst, 1)
    a.add(tear(base, rng, bands=7, maxdx=12, y0=top, y1=top + h, hmin=1, hmax=3, bg=PAPER), 1)
    a.add(tear(clean, rng, bands=4, maxdx=4, y0=top, y1=top + h, hmin=1, hmax=2, bg=PAPER), 1)
    a.add(clean, 6)
    flick = shift_band(clean, top + 3, top + 6, -5, PAPER)
    dashes(flick, rng, 6, (x0, top, x0 + w, top + h), lmin=4, lmax=10)
    a.add(flick, 1)
    a.add(clean, 3)
    a.add(invert(clean), 1)
    a.add(clean, 1)

    # tagline types in under the logo
    for part in (TAG[:10], TAG):
        img = clean.copy()
        tiny(img, (W - tiny_w(TAG)) // 2, 51, part)
        a.add(img, 1 if part != TAG else 6)
    final = img
    a.add(tear(final, rng, bands=8, maxdx=10, y0=top - 2, y1=58, hmin=1, hmax=3, bg=PAPER), 1)
    out = canvas(PAPER)
    dashes(out, rng, 30, (0, top - 2, W, 56), lmin=3, lmax=24)
    speckle(out, rng, 40, (0, top - 4, W, 60))
    a.add(out, 1)
    a.add(_cursor(top, x_us, False), 1)
    return a
