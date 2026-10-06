"""Tray icon: DedSec-style hooded skull with a status-coloured ring."""
from PIL import Image, ImageDraw

_cache = {}


def make_icon(ring="#f0b400", size=64):
    if ring in _cache:
        return _cache[ring]
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([1, 1, size - 2, size - 2], fill="#111111", outline=ring, width=5)
    # hood
    d.ellipse([16, 9, 48, 45], fill="#e8e8e8")
    d.polygon([(17, 33), (47, 33), (54, 56), (10, 56)], fill="#e8e8e8")
    d.ellipse([21, 15, 43, 41], fill="#111111")
    # skull
    d.ellipse([24, 17, 40, 32], fill="#e8e8e8")
    d.rectangle([27, 30, 37, 37], fill="#e8e8e8")
    d.rectangle([27, 22, 30, 26], fill="#111111")
    d.rectangle([34, 22, 37, 26], fill="#111111")
    for x in (29, 32, 35):
        d.line([x, 34, x, 37], fill="#111111", width=1)
    _cache[ring] = img
    return img
