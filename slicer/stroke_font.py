"""Single-stroke label fonts for laser engraving.

Glyph data comes from the classic Hershey fonts (see hershey_data.py) -
true single-stroke plotter fonts: the laser draws every line exactly once.
Three faces are available:

- "simplex": Hershey Simplex - clean sans, fewest points (default)
- "roman":   Hershey Roman   - serif face, more detailed
- "script":  Hershey Script  - cursive single stroke

`simplify` (0..1) reduces control points with Douglas-Peucker: straight
runs lose only redundant collinear points, curved parts get coarser as the
value rises, staying legible up to ~1.0.
"""
from __future__ import annotations

from .hershey_data import FONTS

DEFAULT_FONT = "simplex"
FONT_NAMES = list(FONTS)


def _rdp(points: list[tuple], tol: float) -> list[tuple]:
    """Douglas-Peucker polyline simplification."""
    if tol <= 0 or len(points) < 3:
        return points
    (x0, y0), (x1, y1) = points[0], points[-1]
    dx, dy = x1 - x0, y1 - y0
    norm = (dx * dx + dy * dy) ** 0.5
    dmax, idx = 0.0, 0
    for i in range(1, len(points) - 1):
        px, py = points[i]
        if norm < 1e-12:
            d = ((px - x0) ** 2 + (py - y0) ** 2) ** 0.5
        else:
            d = abs(dx * (y0 - py) - dy * (x0 - px)) / norm
        if d > dmax:
            dmax, idx = d, i
    if dmax <= tol:
        return [points[0], points[-1]]
    left = _rdp(points[: idx + 1], tol)
    right = _rdp(points[idx:], tol)
    return left[:-1] + right


def _glyph(font: str, ch: str):
    table = FONTS.get(font, FONTS[DEFAULT_FONT])
    return table.get(ch) or table.get("-") or (0.5, [])


def text_strokes(text: str, height: float, x: float = 0.0, y: float = 0.0,
                 font: str = DEFAULT_FONT, simplify: float = 0.0
                 ) -> list[list[tuple[float, float]]]:
    """Polylines for `text` at glyph height `height`, baseline starting at (x, y).

    simplify 0..1: control-point reduction (0 = full Hershey detail).
    """
    tol = max(0.0, min(1.0, simplify)) * 0.045  # in em units; ~0.27 mm at 6 mm height
    strokes = []
    cx = x
    for ch in text:
        adv, glyph = _glyph(font, ch)
        for poly in glyph:
            pts = _rdp(poly, tol) if tol > 0 else poly
            strokes.append([(cx + px * height, y + py * height) for px, py in pts])
        cx += adv * height
    return strokes


def text_width(text: str, height: float, font: str = DEFAULT_FONT) -> float:
    return sum(_glyph(font, ch)[0] for ch in text) * height
