"""Hatch areas from polygon shapefiles - 24 patterns for landscape plans.

A polygon (lake, road, grassland, ...) is scored onto the VISIBLE part of
each contour layer - the ring that stays exposed after the next layer is
glued on (region_i minus region_{i+1}). Glue zones are never hatched.

Pattern groups:
- linear:   lines, double, dashes, dashdot, cross, trigrid, zigzag
- water:    waves, ripples, scales
- paving:   herringbone, brick, hex, diamonds
- scatter:  dots, rings, stipple, pebbles, plus, ticks, grass, marsh
- abstract: interference, echo

All patterns honour a rotation angle (echo is rotation-invariant). Scatter
symbols use deterministic grid-hash jitter, so re-slicing never reshuffles
them. Element counts are capped so a huge area with tiny spacing cannot
freeze the app - spacing is coarsened instead.

The .shp file is parsed directly (pure Python, main file only - no .shx/.dbf
needed). Supported shape types: 5 Polygon, 15 PolygonZ, 25 PolygonM.
"""
from __future__ import annotations

import math
import struct

import numpy as np
import shapely
from shapely import affinity
from shapely.geometry import Polygon, MultiPolygon, LineString, MultiLineString

HATCH_COLORS = {  # selection -> (DXF layer, preview RGB)
    "blue": ("DLF-01_score_light", "#0000ff"),
    "green": ("DLF-02_score_medium", "#00c800"),
    "cyan": ("DLF-03_score_strong", "#00b4b4"),
    "magenta": ("DLF-04_cut_inner", "#ff00ff"),  # CUT - e.g. holes at point features
}

# key -> (group, display label)
HATCH_PATTERNS = {
    "lines": ("linear", "Lines"),
    "double": ("linear", "Double lines"),
    "dashes": ("linear", "Dashes"),
    "dashdot": ("linear", "Dash-dot"),
    "cross": ("linear", "Crosshatch"),
    "trigrid": ("linear", "Triangle grid"),
    "zigzag": ("linear", "Zigzag"),
    "waves": ("water", "Waves"),
    "ripples": ("water", "Ripples"),
    "scales": ("water", "Fish scales"),
    "herringbone": ("paving", "Herringbone"),
    "brick": ("paving", "Running bond"),
    "hex": ("paving", "Honeycomb"),
    "diamonds": ("paving", "Diamonds"),
    "dots": ("scatter", "Dots"),
    "rings": ("scatter", "Rings"),
    "stipple": ("scatter", "Stipple"),
    "pebbles": ("scatter", "Pebbles"),
    "plus": ("scatter", "Plus marks"),
    "ticks": ("scatter", "Ticks"),
    "grass": ("scatter", "Grass tufts"),
    "marsh": ("scatter", "Marsh reeds"),
    "interference": ("abstract", "Interference"),
    "echo": ("abstract", "Contour echo"),
}

DEFAULT_HATCH = {"pattern": "lines", "spacing_mm": 2.0, "rotation_deg": 45.0,
                 "outline": True, "color": "green"}
DEFAULT_LINE = {"linetype": "dashed", "linetype_scale": 1.0, "color": "green"}
DEFAULT_POINT = {"radius_mm": 2.0, "linetype": "solid", "linetype_scale": 1.0,
                 "color": "green", "point_hatch": "none",
                 "point_hatch_spacing_mm": 1.0}

# on/off run lengths in mm (multiplied by linetype_scale). Dots are 0.4 mm
# ticks - short enough to read as dots, long enough that the laser scores
# them visibly (0.1 mm burns were invisible on cardboard AND in the preview).
LINETYPES = {
    "solid": [],
    "dashed": [3.0, 1.5],
    "dotted": [0.4, 1.1],
    "dashdot": [3.0, 1.1, 0.4, 1.1],
    "dashdotdot": [3.0, 1.1, 0.4, 1.1, 0.4, 1.1],
}


# --------------------------------------------------------------- shapefile

def read_shp(data: bytes):
    """Read a .shp main file. Returns (kind, geometry):

    - ("polygon", MultiPolygon)      types 5/15/25
    - ("line", MultiLineString)      types 3/13/23
    - ("point", list[(x, y)])        types 1/11/21/8/18/28
    """
    if len(data) < 100 or struct.unpack(">i", data[0:4])[0] != 9994:
        raise ValueError("not a shapefile (.shp main file expected)")
    shp_type = struct.unpack("<i", data[32:36])[0]
    if shp_type in (5, 15, 25):
        return "polygon", read_shp_polygons(data)
    if shp_type in (3, 13, 23):
        return "line", _read_shp_lines(data)
    if shp_type in (1, 11, 21, 8, 18, 28):
        return "point", _read_shp_points(data)
    raise ValueError(f"unsupported shapefile type {shp_type}")


def _shp_records(data: bytes):
    pos, end = 100, len(data)
    while pos + 8 <= end:
        (_, content_len) = struct.unpack(">ii", data[pos:pos + 8])
        rec = data[pos + 8: pos + 8 + content_len * 2]
        pos += 8 + content_len * 2
        if len(rec) >= 4:
            yield struct.unpack("<i", rec[0:4])[0], rec


def _read_shp_lines(data: bytes) -> MultiLineString:
    lines = []
    for rtype, rec in _shp_records(data):
        if rtype not in (3, 13, 23):
            continue
        n_parts, n_points = struct.unpack("<ii", rec[36:44])
        parts = struct.unpack(f"<{n_parts}i", rec[44:44 + 4 * n_parts])
        pts = np.frombuffer(rec, dtype="<f8", count=n_points * 2,
                            offset=44 + 4 * n_parts).reshape(-1, 2)
        for k in range(n_parts):
            a = parts[k]
            b = parts[k + 1] if k + 1 < n_parts else n_points
            if b - a >= 2:
                lines.append(LineString(pts[a:b]))
    if not lines:
        raise ValueError("shapefile contains no line features")
    return MultiLineString(lines)


def _read_shp_points(data: bytes) -> list[tuple]:
    points = []
    for rtype, rec in _shp_records(data):
        if rtype in (1, 11, 21):        # single point: type, x, y (z/m follow)
            x, y = struct.unpack("<2d", rec[4:20])
            points.append((x, y))
        elif rtype in (8, 18, 28):      # multipoint: type, bbox, n, points
            n = struct.unpack("<i", rec[36:40])[0]
            pts = np.frombuffer(rec, dtype="<f8", count=n * 2, offset=40).reshape(-1, 2)
            points.extend(map(tuple, pts))
    if not points:
        raise ValueError("shapefile contains no point features")
    return points


def read_shp_polygons(data: bytes) -> MultiPolygon:
    """Parse polygons out of a .shp main file (ESRI shapefile spec)."""
    if len(data) < 100 or struct.unpack(">i", data[0:4])[0] != 9994:
        raise ValueError("not a shapefile (.shp main file expected)")
    shp_type = struct.unpack("<i", data[32:36])[0]
    if shp_type not in (5, 15, 25):
        raise ValueError(f"shapefile type {shp_type} is not a polygon type")
    pos, end = 100, len(data)
    outers, holes = [], []
    while pos + 8 <= end:
        (_, content_len) = struct.unpack(">ii", data[pos:pos + 8])
        rec = data[pos + 8: pos + 8 + content_len * 2]
        pos += 8 + content_len * 2
        if len(rec) < 4 or struct.unpack("<i", rec[0:4])[0] not in (5, 15, 25):
            continue
        n_parts, n_points = struct.unpack("<ii", rec[36:44])
        parts = struct.unpack(f"<{n_parts}i", rec[44:44 + 4 * n_parts])
        pts_off = 44 + 4 * n_parts
        pts = np.frombuffer(rec, dtype="<f8", count=n_points * 2,
                            offset=pts_off).reshape(-1, 2)
        for k in range(n_parts):
            a = parts[k]
            b = parts[k + 1] if k + 1 < n_parts else n_points
            ring = pts[a:b]
            if len(ring) < 4:
                continue
            poly = Polygon(ring)
            if not poly.is_valid:
                poly = shapely.make_valid(poly)
            if poly.is_empty:
                continue
            # shapefile winding: clockwise (negative signed area) = outer ring
            x, y = ring[:, 0], ring[:, 1]
            signed_area = 0.5 * np.sum(x[:-1] * y[1:] - x[1:] * y[:-1])
            (outers if signed_area < 0 else holes).append(poly)
    if not outers:
        outers, holes = holes, []  # tolerate reversed winding
    geom = shapely.union_all(outers)
    if holes:
        geom = geom.difference(shapely.union_all(holes))
    geom = shapely.make_valid(geom)
    if geom.is_empty:
        raise ValueError("shapefile contains no polygon area")
    if isinstance(geom, Polygon):
        geom = MultiPolygon([geom])
    return geom


# --------------------------------------------------------------- utilities

def _rand(i: int, j: int, salt: int = 0) -> float:
    """Deterministic 0..1 hash of a grid cell - stable across runs."""
    n = (i * 73856093) ^ (j * 19349663) ^ (salt * 83492791)
    n = ((n ^ (n >> 13)) * 1274126177) & 0xFFFFFFFF
    return ((n >> 8) & 0xFFFF) / 65535.0


def _lines_of(geom):
    from .contours import _lines_of as impl
    return impl(geom)


def _circle(cx, cy, r, n=8):
    return [(cx + r * math.cos(2 * math.pi * k / n),
             cy + r * math.sin(2 * math.pi * k / n)) for k in range(n + 1)]


def _cap_line_spacing(s: float, half: float, max_rows: int = 400) -> float:
    return max(s, 2 * half / max_rows)


def _frame(region):
    minx, miny, maxx, maxy = region.bounds
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    half = math.hypot(maxx - minx, maxy - miny) / 2 + 1.0
    return cx, cy, half


# ------------------------------------------------- line-family generators
# All generators work in an axis-aligned frame; rotation is applied by
# rotating the REGION backwards, clipping, then rotating strokes forward.

def _rows(cx, cy, half, s):
    n = int(half / s) + 1
    return [(cy + k * s) for k in range(-n, n + 1)]


def _family_lines(cx, cy, half, s):
    return [LineString([(cx - half, y), (cx + half, y)]) for y in _rows(cx, cy, half, s)]


def _family_double(cx, cy, half, s):
    out = []
    for y in _rows(cx, cy, half, s):
        out.append(LineString([(cx - half, y), (cx + half, y)]))
        out.append(LineString([(cx - half, y + 0.28 * s), (cx + half, y + 0.28 * s)]))
    return out


def _family_dashes(cx, cy, half, s, dash=1.5, gap=0.75):
    out = []
    for y in _rows(cx, cy, half, s):
        x = cx - half
        while x < cx + half:
            out.append(LineString([(x, y), (min(x + dash * s, cx + half), y)]))
            x += (dash + gap) * s
    return out


def _family_dashdot(cx, cy, half, s):
    out = []
    for y in _rows(cx, cy, half, s):
        x = cx - half
        while x < cx + half:
            out.append(LineString([(x, y), (min(x + 1.4 * s, cx + half), y)]))
            dot = x + 1.9 * s
            if dot < cx + half:
                out.append(LineString([(dot, y), (dot + 0.06 * s, y)]))
            x += 2.4 * s
    return out


def _family_zigzag(cx, cy, half, s):
    out = []
    amp = 0.38 * s
    for y in _rows(cx, cy, half, s):
        pts = []
        x, k = cx - half, 0
        while x <= cx + half:
            pts.append((x, y + (amp if k % 2 else -amp)))
            x += s
            k += 1
        out.append(LineString(pts))
    return out


def _family_waves(cx, cy, half, s, lam=3.2, amp=0.35, phase_alt=False):
    out = []
    for r, y in enumerate(_rows(cx, cy, half, s)):
        phase = math.pi if (phase_alt and r % 2) else 0.0
        pts = []
        x = cx - half
        while x <= cx + half:
            pts.append((x, y + amp * s * math.sin(2 * math.pi * x / (lam * s) + phase)))
            x += lam * s / 8
        out.append(LineString(pts))
    return out


def _family_scales(cx, cy, half, s):
    out = []
    r = 0.8 * s
    for row, y in enumerate(_rows(cx, cy, half, 0.7 * r)):
        shift = r * 0.85 if row % 2 else 0.0
        x = cx - half + shift
        while x < cx + half:
            pts = [(x + r * math.cos(a), y + r * 0.55 * math.sin(a))
                   for a in [math.pi * k / 8 for k in range(9)]]
            out.append(LineString(pts))
            x += 2 * r * 0.85
    return out


def _family_herringbone(cx, cy, half, s):
    out = []
    n = int(half / s) + 1
    for i in range(-n, n + 1):
        for j in range(-n, n + 1):
            x0, y0 = cx + i * s, cy + j * s
            if (i + j) % 2 == 0:
                out.append(LineString([(x0, y0), (x0 + s, y0 + s)]))
            else:
                out.append(LineString([(x0, y0 + s), (x0 + s, y0)]))
    return out


def _family_brick(cx, cy, half, s):
    out = _family_lines(cx, cy, half, s)
    bw = 2 * s
    n = int(half / s) + 1
    for j in range(-n, n + 1):
        y = cy + j * s
        off = bw / 2 if j % 2 else 0.0
        x = cx - half + off
        while x < cx + half:
            out.append(LineString([(x, y), (x, y + s)]))
            x += bw
    return out


def _family_hex(cx, cy, half, s):
    """Honeycomb from three consecutive edges per cell (avoids double lines)."""
    a = 0.7 * s
    out = []
    ni = int(half / (1.5 * a)) + 2
    nj = int(half / (math.sqrt(3) * a)) + 2
    for i in range(-ni, ni + 1):
        for j in range(-nj, nj + 1):
            hx = cx + 1.5 * a * i
            hy = cy + math.sqrt(3) * a * j + (math.sqrt(3) / 2 * a if i % 2 else 0)
            v = [(hx + a * math.cos(math.pi / 3 * k), hy + a * math.sin(math.pi / 3 * k))
                 for k in range(6)]
            out.append(LineString([v[0], v[1], v[2], v[3]]))
    return out


def _family_ripples(cx, cy, half, s):
    return _family_waves(cx, cy, half, s, lam=2.0, amp=0.5, phase_alt=True)


def _family_cross(cx, cy, half, s):
    base = MultiLineString([list(ls.coords) for ls in _family_lines(cx, cy, half, s)])
    return list(base.geoms) + list(affinity.rotate(base, 90, origin=(cx, cy)).geoms)


def _family_trigrid(cx, cy, half, s):
    base = MultiLineString([list(ls.coords) for ls in _family_lines(cx, cy, half, 1.2 * s)])
    out = list(base.geoms)
    for ang in (60, 120):
        out.extend(affinity.rotate(base, ang, origin=(cx, cy)).geoms)
    return out


def _family_interference(cx, cy, half, s):
    base = MultiLineString([list(ls.coords)
                            for ls in _family_waves(cx, cy, half, s, lam=2.6, amp=0.4)])
    out = list(base.geoms)
    out.extend(affinity.rotate(base, 60, origin=(cx, cy)).geoms)
    return out


_LINE_FAMILIES = {
    "lines": _family_lines,
    "double": _family_double,
    "dashes": _family_dashes,
    "dashdot": _family_dashdot,
    "cross": _family_cross,
    "zigzag": _family_zigzag,
    "waves": _family_waves,
    "ripples": _family_ripples,
    "herringbone": _family_herringbone,
    "brick": _family_brick,
    "hex": _family_hex,
    "trigrid": _family_trigrid,
    "interference": _family_interference,
    "scales": _family_scales,
}


# --------------------------------------------------- scatter symbol makers
# Each returns (grid_x, grid_y, staggered, jitter_frac, margin, make(cx, cy, s, r1, r2))
# where r1/r2 are deterministic 0..1 randoms for the cell.

def _sym_dots(s):
    r = max(0.15, 0.12 * s)
    return s, s, False, 0.0, r, lambda x, y, r1, r2: [_circle(x, y, r, 6)]


def _sym_rings(s):
    r = 0.45 * s
    return 1.5 * s, 1.3 * s, True, 0.0, r, lambda x, y, r1, r2: [_circle(x, y, r, 10)]


def _sym_stipple(s):
    r = max(0.12, 0.07 * s)
    return 0.62 * s, 0.62 * s, False, 0.45, r, lambda x, y, r1, r2: [_circle(x, y, r, 5)]


def _sym_pebbles(s):
    return 1.15 * s, 1.15 * s, True, 0.32, 0.55 * s, (
        lambda x, y, r1, r2: [_circle(x, y, (0.22 + 0.3 * r1) * s, 9)])


def _sym_plus(s):
    a = 0.3 * s
    return 1.3 * s, 1.3 * s, True, 0.0, a, (
        lambda x, y, r1, r2: [[(x - a, y), (x + a, y)], [(x, y - a), (x, y + a)]])


def _sym_ticks(s):
    ln = 0.55 * s
    return 0.95 * s, 0.95 * s, False, 0.4, ln, (
        lambda x, y, r1, r2: [[(x - ln / 2 * math.cos(math.pi * r1),
                                y - ln / 2 * math.sin(math.pi * r1)),
                               (x + ln / 2 * math.cos(math.pi * r1),
                                y + ln / 2 * math.sin(math.pi * r1))]])


def _sym_grass(s):
    h = 0.62 * s
    def make(x, y, r1, r2):
        lean = (r1 - 0.5) * 0.35
        return [[(x, y), (x + h * math.sin(a + lean), y + h * math.cos(a + lean))]
                for a in (-0.32, 0.0, 0.30)]
    return 1.35 * s, 1.15 * s, True, 0.3, h, make


def _sym_marsh(s):
    w, h = 0.55 * s, 0.45 * s
    def make(x, y, r1, r2):
        out = [[(x - w, y), (x + w, y)]]
        for dx in (-0.5 * w, 0.0, 0.5 * w):
            out.append([(x + dx, y), (x + dx, y + h)])
        return out
    return 2.1 * s, 1.55 * s, True, 0.15, max(w, h), make


def _sym_diamonds(s):
    w, h = 0.42 * s, 0.65 * s
    return 1.5 * s, 1.7 * s, True, 0.0, max(w, h), (
        lambda x, y, r1, r2: [[(x, y - h), (x + w, y), (x, y + h), (x - w, y), (x, y - h)]])


_SYMBOLS = {
    "dots": _sym_dots,
    "rings": _sym_rings,
    "stipple": _sym_stipple,
    "pebbles": _sym_pebbles,
    "plus": _sym_plus,
    "ticks": _sym_ticks,
    "grass": _sym_grass,
    "marsh": _sym_marsh,
    "diamonds": _sym_diamonds,
}


# --------------------------------------------------------------- assembly

def _pattern_strokes(region, pattern: str, spacing: float, min_len: float) -> list[list[tuple]]:
    """Strokes for `pattern` clipped to `region`, axis-aligned frame."""
    cx, cy, half = _frame(region)

    if pattern == "echo":
        out = []
        k = 1
        while k < 300:
            inner = region.buffer(-k * spacing)
            if inner.is_empty:
                break
            for ls in _lines_of(inner.boundary):
                if ls.length >= min_len:
                    out.append(list(ls.coords))
            k += 1
        return out

    if pattern in _LINE_FAMILIES:
        s = _cap_line_spacing(spacing, half)
        fam = _LINE_FAMILIES[pattern](cx, cy, half, s)
        # clip each line SEPARATELY (vectorized): a collective overlay would
        # node crossing lines into thousands of tiny laser moves
        shapely.prepare(region)
        clipped = shapely.intersection(np.array(fam, dtype=object), region)
        out = []
        for geom in clipped:
            for ls in _lines_of(geom):
                if ls.length >= min_len:
                    out.append(list(ls.coords))
        return out

    if pattern in _SYMBOLS:
        gx, gy, stagger, jitter, margin, make = _SYMBOLS[pattern](spacing)
        # cap the symbol count for huge areas
        while (2 * half / gx) * (2 * half / gy) > 6000:
            gx *= 1.5
            gy *= 1.5
        safe = region.buffer(-margin)
        if safe.is_empty:
            return []
        ni, nj = int(half / gx) + 1, int(half / gy) + 1
        xs, ys, rs1, rs2 = [], [], [], []
        for i in range(-ni, ni + 1):
            for j in range(-nj, nj + 1):
                x = cx + i * gx + (gx / 2 if (stagger and j % 2) else 0)
                y = cy + j * gy
                if jitter:
                    x += (_rand(i, j, 1) - 0.5) * jitter * gx * 2
                    y += (_rand(i, j, 2) - 0.5) * jitter * gy * 2
                xs.append(x)
                ys.append(y)
                rs1.append(_rand(i, j, 3))
                rs2.append(_rand(i, j, 4))
        inside = shapely.contains_xy(safe, xs, ys)
        out = []
        for x, y, r1, r2, ok in zip(xs, ys, rs1, rs2, inside):
            if ok:
                out.extend(make(x, y, r1, r2))
        return out

    raise ValueError(f"unknown hatch pattern {pattern!r}")


def generate_hatch(region, pattern: str, spacing: float, rotation_deg: float,
                   outline_geom=None, min_len: float = 1.0) -> list[list[tuple]]:
    """Fill `region` (model mm) with a hatch pattern; returns polylines.

    Rotation is handled by rotating the region into the pattern frame and
    rotating the strokes back, so every pattern honours the angle.
    """
    strokes: list[list[tuple]] = []
    if region is None or region.is_empty or spacing <= 0:
        return strokes

    if outline_geom is not None and not outline_geom.is_empty:
        for ls in _lines_of(outline_geom):
            if ls.length >= min_len:
                strokes.append([(round(x, 3), round(y, 3)) for x, y in ls.coords])

    rot = rotation_deg % 360.0
    cx, cy, _ = _frame(region)
    work = affinity.rotate(region, -rot, origin=(cx, cy)) if rot else region
    raw = _pattern_strokes(work, pattern, spacing, min_len)
    if rot:
        ca, sa = math.cos(math.radians(rot)), math.sin(math.radians(rot))
        raw = [[(cx + (x - cx) * ca - (y - cy) * sa,
                 cy + (x - cx) * sa + (y - cy) * ca) for x, y in line] for line in raw]
    strokes.extend([[(round(x, 3), round(y, 3)) for x, y in line] for line in raw])
    return strokes


# ------------------------------------------------------ line & point styling

def apply_linetype(geom, linetype: str, scale: float = 1.0) -> list[list[tuple]]:
    """Turn linework into REAL dash/dot segments (the laser scores exactly
    what the preview shows - no reliance on CAD linetype support)."""
    from shapely.ops import substring
    pattern = [p * max(0.05, scale) for p in LINETYPES.get(linetype, [])]
    out = []
    for ls in _lines_of(geom):
        if not pattern:
            out.append([(round(x, 3), round(y, 3)) for x, y in ls.coords])
            continue
        L = ls.length
        s, i = 0.0, 0
        while s < L:
            run = max(0.05, pattern[i % len(pattern)])
            if i % 2 == 0:  # pen down
                piece = substring(ls, s, min(s + run, L))
                if piece.geom_type == "LineString" and len(piece.coords) >= 2:
                    out.append([(round(x, 3), round(y, 3)) for x, y in piece.coords])
            s += run
            i += 1
    return out


def circles_for_points(points: list[tuple], radius: float, segments: int = 24):
    """Point features -> circle outlines (LineStrings) of the given radius."""
    rings = []
    for x, y in points:
        rings.append(LineString(_circle(x, y, radius, segments)))
    return MultiLineString(rings)


def disks_for_points(points: list[tuple], radius: float, segments: int = 24) -> MultiPolygon:
    """Point features -> filled circle areas (for hatching the interiors)."""
    return MultiPolygon([Polygon(_circle(x, y, radius, segments)) for x, y in points])
