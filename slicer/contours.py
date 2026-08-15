"""Core slicing: DTM -> contour levels -> offset-method boards.

Implements the "horizontal contour model (offset method)" of
digital-landscapes.com, as in the Grasshopper original:

- N boards, each the size of the model footprint, cut IN PLACE.
- Board k carries contour levels k, k+N, k+2N, ... as cut lines. Each cut
  line does double duty: contour i+N is the back cutline (C2) of ring i and
  at the same time the outer cutline (C1) of ring i+N - every contour is
  cut exactly once in the whole model.
- Score line (B) = contour i+1, engraved between the cutlines as the glue
  reference for the ring supplied by the next board.
- Glue surface = strip between B and C2; more boards -> wider strip.

Laser passes: (A) labels blue, (B) score green, (C) inner cuts magenta,
(D) board outline red. All geometry in model millimetres.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import contourpy
import shapely
from shapely.geometry import Point, Polygon, MultiPolygon, LineString, MultiLineString, GeometryCollection
from shapely.ops import polylabel, nearest_points, linemerge

from .dtm import DTM
from .params import SliceParams
from . import stroke_font


@dataclass
class Board:
    board: int                 # board index 0..N-1
    levels: list[int]          # contour levels cut on this board
    outer: list[tuple]         # board outline (model footprint), closed
    holes: list[list[tuple]]   # cut lines (C): contours + nodata holes; closed if first==last
    score: list[list[tuple]]   # score lines (B): next-contour glue references
    labels: list[list[tuple]]  # engraved level-number strokes (A)
    hatch: list[dict] = field(default_factory=list)  # [{color, lines:[polyline,..]}] per hatch layer

    @property
    def bounds(self):
        xs = [p[0] for p in self.outer]
        ys = [p[1] for p in self.outer]
        return min(xs), min(ys), max(xs), max(ys)

    @property
    def size(self):
        x0, y0, x1, y1 = self.bounds
        return x1 - x0, y1 - y0


@dataclass
class SliceResult:
    pieces: list[Board]        # one entry per board (per footprint polygon)
    n_levels: int
    n_boards: int
    world_interval: float
    model_width: float
    model_height: float
    stack: list[dict] = field(default_factory=list)   # per-level RING outlines for the 3D preview
    glue_min: float | None = None                     # narrowest glue strip, mm
    z_levels: list[float] = field(default_factory=list)  # real-world elevation of each level
    warnings: list[str] = field(default_factory=list)


def _filled_region(gen, lower: float, upper: float,
                   dropped: list | None = None) -> MultiPolygon:
    """Region where lower <= elevation < upper, as a MultiPolygon (grid coords).

    Rings carrying a non-finite coordinate are DISCARDED rather than handed to
    GEOS. On rugged terrain contourpy can emit a NaN/Inf vertex in a degenerate
    ring, and GEOS answers with

        IllegalArgumentException: CGAlgorithmsDD::orientationIndex
        encountered NaN/Inf numbers

    which aborts the whole slice with a message that means nothing to a user.
    It showed up in the WebAssembly build (numpy 2.4.3) on terrain the desktop
    build (numpy 2.5.1) handled - the same arithmetic either side of a
    tolerance. A ring with a NaN vertex has no usable geometry anyway, so
    dropping it loses nothing real; the count is reported so the user is told
    rather than silently given a slightly different model.
    """
    points_list, offsets_list = gen.filled(lower, upper)
    polys = []
    for pts, offs in zip(points_list, offsets_list):
        rings = [pts[offs[j]:offs[j + 1]] for j in range(len(offs) - 1)]
        rings = [r for r in rings if len(r) >= 4]
        good = []
        for r in rings:
            if np.isfinite(r).all():
                good.append(r)
            elif dropped is not None:
                dropped.append(1)
        # a lost OUTER ring takes its holes with it - the polygon is gone
        if not good or len(good) != len(rings) and not np.isfinite(rings[0]).all():
            continue
        poly = Polygon(good[0], good[1:])
        if not poly.is_valid:
            poly = shapely.make_valid(poly)
        polys.append(poly)
    merged = shapely.union_all(polys) if polys else Polygon()
    if merged.is_empty:
        return MultiPolygon()
    if isinstance(merged, Polygon):
        return MultiPolygon([merged])
    if isinstance(merged, GeometryCollection):
        merged = MultiPolygon([g for g in merged.geoms if isinstance(g, Polygon)])
    return merged


def _simplify_safe(geom, tol: float, stubborn: list | None = None):
    """Simplify, but never let simplification kill the slice.

    GEOS can raise

        IllegalArgumentException: CGAlgorithmsDD::orientationIndex
        encountered NaN/Inf numbers

    from inside TopologyPreservingSimplifier on geometry that is perfectly
    VALID and whose coordinates are all finite - the NaN is produced by the
    double-double robust predicates themselves, not present in the input. It
    is a WebAssembly-only failure (the wheel's GEOS is built for a floating
    point environment the DD arithmetic behaves differently in), and it needs
    a level with an enormous vertex count to show up: the real report was a
    DEM-of-difference where ~90% of cells sit at zero, so the contour nearest
    zero traced every speckle of noise - 27,357 vertices on one level of
    fourteen, and that one level aborted the entire model.

    Simplification is an optimisation - it shortens the laser path - never a
    correctness requirement. So: try topology-preserving, fall back to plain
    Douglas-Peucker (a different code path), and if GEOS refuses both, keep the
    geometry unsimplified. A longer cut on one level beats no model at all.
    The caller reports the count so the user is told rather than left guessing.
    """
    if geom.is_empty:
        return geom
    try:
        return shapely.simplify(geom, tol, preserve_topology=True)
    except Exception:
        pass
    try:
        out = shapely.simplify(geom, tol, preserve_topology=False)
        # DP can fold a ring onto itself; only take it if it survived intact.
        # This path DID simplify, so it is not counted as stubborn.
        if not out.is_empty and out.is_valid:
            return out
    except Exception:
        pass
    if stubborn is not None:
        stubborn.append(1)
    return geom


def _coords(line) -> list[tuple]:
    return [(round(x, 3), round(y, 3)) for x, y in line.coords]


def _lines_of(geom) -> list[LineString]:
    if geom.is_empty:
        return []
    if isinstance(geom, LineString):
        return [geom]
    if isinstance(geom, (MultiLineString, GeometryCollection)):
        out = []
        for g in geom.geoms:
            out.extend(_lines_of(g))
        return out
    if hasattr(geom, "boundary"):
        return _lines_of(geom.boundary)
    return []


def _label_anchor(ring_area, width: float, height: float, min_height: float = 2.0):
    """Best anchor for a label of width x height inside the visible ring.

    Returns (point, scale) or (None, 0). The label may shrink to fit narrow
    rings but never below min_height (the label-density slider steers this).
    """
    need_r = 0.5 * math.hypot(width, height)
    s_min = max(0.05, min(1.0, min_height / height))
    min_area = width * height * s_min * s_min * 0.5
    candidates = []
    if ring_area is not None and not ring_area.is_empty:
        geoms = ring_area.geoms if hasattr(ring_area, "geoms") else [ring_area]
        candidates = sorted((g for g in geoms if isinstance(g, Polygon) and g.area > min_area),
                            key=lambda g: g.area, reverse=True)
    best = (None, 0.0)
    for cand in candidates[:6]:
        try:
            pt = polylabel(cand, tolerance=max(0.5, height / 4))
        except Exception:
            pt = cand.representative_point()
        r = pt.distance(cand.boundary)
        if r >= need_r * 0.9:
            return pt, 1.0
        scale = r / need_r
        if scale > best[1]:
            best = (pt, scale)
    pt, scale = best
    if pt is not None and scale * height >= min_height:
        return pt, scale
    return None, 0.0


def slice_dtm(dtm: DTM, params: SliceParams, hatches=None) -> SliceResult:
    """hatches: optional list of (polys, settings) - polys is a (Multi)Polygon
    in DTM-local world coordinates (x east from the west edge, y north from
    the south edge), settings a dict per hatch.DEFAULT_HATCH. Each layer is
    hatched onto the visible ring of every level."""
    warnings: list[str] = []
    interval = params.world_interval
    zmin, zmax = dtm.zmin, dtm.zmax
    eps_z = 1e-6 * interval
    if params.base_mode == "zero":
        # contours at absolute multiples of the interval (aligned to 0 m altitude);
        # level 0 stays the base plate at the terrain minimum
        levels = [zmin]
        z = (math.floor(zmin / interval) + 1) * interval
        while z <= zmax + eps_z and len(levels) < params.max_levels:
            levels.append(z)
            z += interval
        capped = z <= zmax + eps_z
    else:
        n_wanted = int(math.floor((zmax - zmin) / interval)) + 1
        levels = [zmin + i * interval for i in range(min(n_wanted, params.max_levels))]
        capped = n_wanted > params.max_levels
    if capped:
        warnings.append(
            f"terrain needs more than {params.max_levels} layers; capped "
            f"- increase material thickness or scale")
    n_levels = len(levels)

    rows, cols = dtm.elevation.shape
    cell = dtm.cell_size
    x = np.arange(cols, dtype=np.float64) * cell
    y = (rows - 1 - np.arange(rows, dtype=np.float64)) * cell
    X, Y = np.meshgrid(x, y)
    z = np.ma.masked_invalid(dtm.elevation)
    gen = contourpy.contour_generator(
        x=X, y=Y, z=z, name="serial", corner_mask=True,
        fill_type=contourpy.FillType.OuterOffset)

    top = zmax + 10.0 * interval
    eps = 1e-9 * max(1.0, abs(zmax))
    dropped: list = []
    regions = [_filled_region(gen, lv - eps if i else zmin - 1.0, top, dropped)
               for i, lv in enumerate(levels)]
    if dropped:
        warnings.append(
            f"{len(dropped)} degenerate contour ring(s) skipped - the terrain "
            f"has spots too rough for the contour interval; the model is "
            f"otherwise complete")

    # world -> model mm, then simplify
    f = params.world_to_model
    scaled = [shapely.transform(r, lambda a: a * f) if not r.is_empty else r for r in regions]
    tol = params.simplify_mm
    if tol > 0:
        stubborn: list = []
        scaled = [_simplify_safe(r, tol, stubborn) for r in scaled]
        if stubborn:
            warnings.append(
                f"{len(stubborn)} contour level(s) could not be simplified and "
                f"are cut at full detail - the laser path is longer there. This "
                f"happens on levels that trace a very large flat area (a "
                f"difference raster, or water). The model itself is complete.")
    scaled = [MultiPolygon([r]) if isinstance(r, Polygon) else r for r in scaled]

    footprint = scaled[0]
    if footprint.is_empty:
        raise ValueError("model footprint is empty - no valid elevation data")
    # contour stretches that coincide with the board edge must not be cut twice
    fp_boundary = footprint.boundary
    buf_r = max(0.3, tol * 2 + 0.1)
    edge_buf = fp_boundary.buffer(buf_r)
    snap_dist = buf_r + 0.2

    N = max(2, int(params.n_boards))
    min_len = params.min_length_mm

    # label density -> smallest label height still placed:
    # 0 = off; 0..0.5 shrinks the requirement from full size down to 2 mm
    # (the long-standing default); 0.5..1 keeps relaxing towards 0.7 mm so
    # at 1.0 practically every ring receives a (possibly tiny) number.
    labels_on = params.labels_enabled and params.label_density > 0.0
    H = params.label_height_mm
    d = params.label_density
    if d <= 0.5:
        label_min_h = H - (H - 2.0) * (d / 0.5)
    else:
        label_min_h = 2.0 - (2.0 - 0.7) * ((d - 0.5) / 0.5)
    label_min_h = max(0.5, min(label_min_h, H))

    boundaries = [r.boundary if not r.is_empty else None for r in scaled]

    _trim_cache: dict[int, list] = {}

    def trimmed(level_idx: int) -> list[list[tuple]]:
        """Contour lines of a level, minus the parts lying along the board edge.

        Trimming shortens lines that merely CROSS the edge zone, so open ends
        are snapped back onto the outline - cuts must meet the red border with
        no gap, otherwise the rings do not separate.
        """
        if level_idx in _trim_cache:
            return _trim_cache[level_idx]
        _trim_cache[level_idx] = _trimmed_uncached(level_idx)
        return _trim_cache[level_idx]

    def _trimmed_uncached(level_idx: int) -> list[list[tuple]]:
        b = boundaries[level_idx]
        if b is None:
            return []
        pieces = _lines_of(b.difference(edge_buf))
        if not pieces:
            return []
        # difference() splits every ring at its start vertex - merge continuous
        # runs back together so only genuine edge-trimmed ends remain
        merged = linemerge(MultiLineString(pieces))
        out = []
        for ls in _lines_of(merged):
            if ls.length < min_len:
                continue
            coords = list(ls.coords)
            if coords[0] != coords[-1]:  # open: the ends were eaten by the edge buffer
                for at_end in (False, True):
                    pt = Point(coords[-1] if at_end else coords[0])
                    if 1e-9 < pt.distance(fp_boundary) <= snap_dist:
                        snap = nearest_points(fp_boundary, pt)[0]
                        if at_end:
                            coords.append((snap.x, snap.y))
                        else:
                            coords.insert(0, (snap.x, snap.y))
            out.append([(round(px, 3), round(py, 3)) for px, py in coords])
        return out

    # feature layers (polygon hatch / line / point): DTM-local world -> model mm
    from .hatch import (DEFAULT_HATCH, DEFAULT_LINE, DEFAULT_POINT,
                        circles_for_points, disks_for_points)
    hatch_prepped = []  # dicts: {mode: "poly"|"lines", geom(s), cfg}

    def _no_overlap(cfg):
        warnings.append(
            f"layer '{cfg.get('name', cfg.get('id', '?'))}' does not overlap the "
            f"terrain - check that the shapefile shares the terrain's coordinate system")

    for geom, settings in (hatches or []):
        settings = {k: v for k, v in (settings or {}).items() if v is not None}
        kind = settings.get("kind", "polygon")
        if kind == "point":
            cfg = {**DEFAULT_POINT, **settings}
            pts = [(x * f, y * f) for x, y in (geom or [])]
            if not pts:
                continue
            radius = max(0.2, float(cfg["radius_mm"]))
            circles = circles_for_points(pts, radius)
            if circles.intersection(footprint).is_empty:
                _no_overlap(cfg)
            else:
                prep = {"mode": "lines", "geom": circles, "cfg": cfg}
                if cfg.get("point_hatch", "none") != "none":
                    prep["fill"] = disks_for_points(pts, radius)
                hatch_prepped.append(prep)
        elif kind == "line":
            if geom is None or geom.is_empty:
                continue
            cfg = {**DEFAULT_LINE, **settings}
            lm = shapely.transform(geom, lambda a: a * f)
            if lm.intersection(footprint).is_empty:
                _no_overlap(cfg)
            else:
                hatch_prepped.append({"mode": "lines", "geom": lm, "cfg": cfg})
        else:
            if geom is None or geom.is_empty:
                continue
            cfg = {**DEFAULT_HATCH, **settings}
            hm_full = shapely.transform(geom, lambda a: a * f)
            hm = shapely.make_valid(hm_full).intersection(footprint)
            if hm.is_empty:
                _no_overlap(cfg)
            else:
                hatch_prepped.append({"mode": "poly", "geom": hm,
                                      "boundary": hm_full.boundary, "cfg": cfg})

    boards: list[Board] = []
    n_label_skipped = 0
    n_label_fallback = 0
    for k in range(N):
        board_levels = [i for i in range(n_levels)
                        if i % N == k and not scaled[i].is_empty
                        and (i > 0 or params.base_plate)]
        if not board_levels:
            continue
        cuts: list[list[tuple]] = []
        score: list[list[tuple]] = []
        labels: list[list[tuple]] = []
        hatch_entries: list[dict] = [
            {"color": prep["cfg"]["color"], "lines": []} for prep in hatch_prepped]
        for i in board_levels:
            if i > 0:  # level 0's contour IS the board edge
                cuts.extend(trimmed(i))
            if i + 1 < n_levels:
                score.extend(trimmed(i + 1))
            if labels_on:
                text = str(i)
                lw = stroke_font.text_width(text, params.label_height_mm, params.label_font)
                nxt = scaled[i + 1] if i + 1 < n_levels else MultiPolygon()
                back = scaled[i + N] if i + N < n_levels else MultiPolygon()
                # label EVERY disjoint physical piece of this level (the ring
                # between contour i and back cutline i+N) - a level split over
                # several hills gets its number on each part
                piece_area = scaled[i].difference(back) if not back.is_empty else scaled[i]
                piece_polys = sorted(
                    (g for g in (piece_area.geoms if hasattr(piece_area, "geoms") else [piece_area])
                     if isinstance(g, Polygon)),
                    key=lambda g: g.area, reverse=True)

                def _place(area) -> bool:
                    pt, scale = _label_anchor(area, lw * 1.4, params.label_height_mm * 1.6,
                                              min_height=label_min_h)
                    if pt is None:
                        return False
                    h = params.label_height_mm * scale
                    lw2 = stroke_font.text_width(text, h, params.label_font)
                    labels.extend(
                        [(round(px, 3), round(py, 3)) for px, py in s]
                        for s in stroke_font.text_strokes(
                            text, h, pt.x - lw2 / 2, pt.y - h / 2,
                            font=params.label_font, simplify=params.label_simplify))
                    return True

                placed_any = False
                for poly in piece_polys[:20]:
                    if params.label_hidden and nxt.is_empty:
                        # topmost piece: nothing ever covers it, so a number
                        # would stay visible - leave the summit clean
                        continue
                    if params.label_hidden:
                        # glue zone between score line (i+1) and back cutline
                        # (i+N): the next ring covers it once glued
                        if _place(poly.intersection(nxt)):
                            placed_any = True
                        elif _place(poly.difference(nxt)):
                            placed_any = True
                            n_label_fallback += 1
                    else:
                        if _place(poly.difference(nxt) if not nxt.is_empty else poly):
                            placed_any = True
                if piece_polys and not placed_any and not (params.label_hidden and nxt.is_empty):
                    n_label_skipped += 1
            if hatch_prepped:
                from .hatch import generate_hatch, apply_linetype
                nxt2 = scaled[i + 1] if i + 1 < n_levels else MultiPolygon()
                visible = scaled[i].difference(nxt2) if not nxt2.is_empty else scaled[i]
                for entry, prep in zip(hatch_entries, hatch_prepped):
                    cfg = prep["cfg"]
                    if prep["mode"] == "lines":
                        clip = prep["geom"].intersection(visible)
                        if not clip.is_empty:
                            entry["lines"].extend(apply_linetype(
                                clip, cfg["linetype"], float(cfg["linetype_scale"])))
                        if "fill" in prep:  # hatch inside the point circles
                            fill_clip = prep["fill"].intersection(visible)
                            if not fill_clip.is_empty:
                                entry["lines"].extend(generate_hatch(
                                    fill_clip, cfg["point_hatch"],
                                    max(0.2, float(cfg["point_hatch_spacing_mm"])), 0.0))
                        continue
                    clip = prep["geom"].intersection(visible)
                    if clip.is_empty:
                        continue
                    outline = prep["boundary"].intersection(visible) if cfg["outline"] else None
                    entry["lines"].extend(generate_hatch(
                        clip, cfg["pattern"], float(cfg["spacing_mm"]),
                        float(cfg["rotation_deg"]), outline_geom=outline))
        # one physical board per footprint polygon (footprint is normally a single rectangle)
        for fp in footprint.geoms:
            boards.append(Board(
                board=k, levels=board_levels,
                outer=_coords(fp.exterior),
                holes=[_coords(r) for r in fp.interiors if r.length >= min_len] + cuts,
                score=score, labels=labels,
                hatch=[e for e in hatch_entries if e["lines"]]))

    # narrowest glue strip: between score line (i+1) and back cutline (i+N),
    # measured on the edge-trimmed lines (contours meeting at the board edge
    # would otherwise report a false 0)
    def _trim_geom(idx: int):
        lines = [LineString(c) for c in trimmed(idx) if len(c) >= 2]
        return MultiLineString(lines) if lines else None

    glue_min = None
    for i in range(n_levels):
        if i + N < n_levels:
            g1, g2 = _trim_geom(i + 1), _trim_geom(i + N)
            if g1 is not None and g2 is not None:
                d = g1.distance(g2)
                glue_min = d if glue_min is None else min(glue_min, d)
    if glue_min is not None and glue_min < 5.0:
        warnings.append(
            f"narrowest glue strip is only {glue_min:.1f} mm (between green score line and back "
            f"cutline) - increase the number of boards for more glue surface")
    if n_label_skipped:
        warnings.append(f"{n_label_skipped} ring(s) too narrow for a label - engrave skipped there")
    if n_label_fallback:
        warnings.append(
            f"{n_label_fallback} label(s) did not fit the glue zone - placed on the visible "
            f"ring instead (more boards widen the glue strips)")

    # stack preview: the PHYSICAL rings - layer i spans from contour i to
    # contour i+N (its back cutline), so the underside of the model steps
    # like the top surface, offset by N layers
    stack = []
    for i, region in enumerate(scaled):
        if region.is_empty or (i == 0 and not params.base_plate):
            continue
        ring = region
        if i + N < n_levels and not scaled[i + N].is_empty:
            ring = shapely.make_valid(region.difference(scaled[i + N]))
        geoms = ring.geoms if hasattr(ring, "geoms") else [ring]
        for poly in geoms:
            if isinstance(poly, Polygon) and not poly.is_empty:
                stack.append({"level": i, "outer": _coords(poly.exterior),
                              "holes": [_coords(r) for r in poly.interiors]})

    return SliceResult(
        pieces=boards, n_levels=n_levels, n_boards=N, world_interval=interval,
        model_width=dtm.width_world * f, model_height=dtm.height_world * f,
        stack=stack, glue_min=glue_min, z_levels=[round(z, 3) for z in levels],
        warnings=warnings)
