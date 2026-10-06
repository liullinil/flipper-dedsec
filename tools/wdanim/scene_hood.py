"""DedSec hoodie: skull-masked hooded figure over a halftone gradient, graffiti DEDSEC tag."""
from PIL import Image, ImageDraw

from .core import (Anim, INK, PAPER, W, H, TOP, canvas, pen, font, text_size, tear, invert,
                   dissolve, on_matrix, CLUSTER4, rng_for)


def halftone(phase=0):
    img = canvas(PAPER)
    px = img.load()
    for y in range(TOP, H):
        level = 0.08 + 0.56 * ((y - TOP) / (H - 1 - TOP))
        for x in range(W):
            if on_matrix(x + phase, y + phase, level, CLUSTER4):
                px[x, y] = INK
    return img


def figure_mask():
    """INK = figure body (to be painted white with a black rim)."""
    m = Image.new("1", (W, H), PAPER)
    d = ImageDraw.Draw(m)
    d.ellipse([46, 14, 82, 51], fill=INK)                      # hood
    d.polygon([(47, 37), (81, 37), (96, 47), (106, 55), (112, 64), (16, 64), (22, 55),
               (32, 47)], fill=INK)                            # shoulders
    return m


def draw_figure(img, eyes="dark"):
    m = figure_mask()
    rim = Image.new("1", (W, H), PAPER)
    for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2), (-1, -1), (1, 1), (-1, 1), (1, -1)):
        rim.paste(Image.new("1", (W, H), INK), (dx, dy), Image.eval(m, lambda p: 255 - p))
    out = Image.composite(img, Image.new("1", (W, H), INK), rim)        # black rim
    out = Image.composite(out, Image.new("1", (W, H), PAPER), m)       # white body
    d = pen(out)
    # body shading: dots thicken towards the right flank
    for y in range(41, H):
        for x in range(76, 114):
            if m.getpixel((x, y)) == INK and on_matrix(x, y, (x - 76) / 70, CLUSTER4):
                d.point((x, y), fill=INK)
    d.ellipse([54, 22, 74, 44], fill=INK)                    # face opening
    d.ellipse([58, 25, 70, 37], fill=PAPER)                  # skull cranium
    d.rectangle([60, 35, 68, 40], fill=PAPER)                # jaw
    if eyes == "dark":
        d.rectangle([60, 29, 62, 32], fill=INK)
        d.rectangle([66, 29, 68, 32], fill=INK)
    elif eyes == "glow":
        d.rectangle([59, 28, 63, 33], fill=INK)
        d.rectangle([65, 28, 69, 33], fill=INK)
        d.point((61, 30), fill=PAPER)
        d.point((67, 30), fill=PAPER)
    elif eyes == "x":
        for cx in (61, 67):
            d.line([cx - 1, 29, cx + 1, 31], fill=INK)
            d.line([cx + 1, 29, cx - 1, 31], fill=INK)
    d.polygon([(64, 33), (63, 35), (65, 35)], fill=INK)      # nose
    for x in (62, 64, 66):
        d.line([x, 38, x, 40], fill=INK)                     # teeth
    return out


def tag_layer(dy=0, dx=0, rise=0.12):
    """Graffiti DEDSEC: white letters, thick black outline, sheared to rise to the right."""
    f = font("impact.ttf", 16)
    s = "DEDSEC"
    w, h = text_size(s, f)
    pad = 3
    lay = Image.new("L", (w + 2 * pad, h + 2 * pad), 0)      # 0 transparent, 128 rim, 255 fill
    d = ImageDraw.Draw(lay)
    d.fontmode = "1"
    x0, y0, _, _ = f.getbbox(s)
    d.text((pad - x0, pad - y0), s, font=f, fill=128, stroke_width=2, stroke_fill=128)
    d.text((pad - x0, pad - y0), s, font=f, fill=255)
    extra = int(lay.width * rise) + 1
    sheared = Image.new("L", (lay.width, lay.height + extra), 0)
    for i in range(lay.width):
        col = lay.crop((i, 0, i + 1, lay.height))
        sheared.paste(col, (i, extra - int(i * rise)))
    return sheared, (W - sheared.width) // 2 + dx, 36 + dy


def put_tag(img, dy=0, dx=0):
    lay, x, y = tag_layer(dy, dx)
    out = img.copy()
    px = out.load()
    lp = lay.load()
    for j in range(lay.height):
        for i in range(lay.width):
            v = lp[i, j]
            X, Y = x + i, y + j
            if v and 0 <= X < W and 0 <= Y < H:
                # fill gets a light halftone like a sprayed stencil
                px[X, Y] = INK if v == 128 else PAPER
    return out


def build():
    a = Anim("WD_DedSec_Hood_128x64", "DedSec hooded skull")
    rng = rng_for(a.name)
    bg0, bg1 = halftone(0), halftone(2)
    a.add(bg0, 1)
    a.add(bg1, 1)
    a.add(tear(draw_figure(bg0), rng, bands=6, maxdx=8, y0=TOP), 1)
    a.add(draw_figure(bg1), 3)
    a.add(draw_figure(bg1, "glow"), 1)
    a.add(draw_figure(bg0), 2)
    a.add(tear(put_tag(draw_figure(bg0), dy=-4, dx=6), rng, bands=4, maxdx=6, y0=TOP), 1)
    full = [put_tag(draw_figure(b)) for b in (bg0, bg1)]
    for i in range(6):
        a.add(full[i // 2 % 2], 1)
    a.add(tear(full[0], rng, bands=5, maxdx=7, y0=TOP), 1)
    a.add(full[1], 2)
    a.add(put_tag(draw_figure(bg1, "x")), 1)
    a.add(full[1], 1)
    a.add(invert(full[0]), 1)
    a.add(full[0], 2)
    a.add(dissolve(full[0], 0.5, INK), 1)
    a.add(dissolve(bg1, 0.75, INK), 1)
    return a
