"""DXF export: one file per sheet with the six DLF laser-pass layers.

Layer scheme and colors match the Rhino baking setup of the Grasshopper
original (DL-Contour_offset_method_011.gh) so existing laser pass
configurations keep working.
"""
from __future__ import annotations

import io
import zipfile
from datetime import date

import ezdxf

from .params import SliceParams
from .nesting import NestResult

# (name, ACI color, true color RGB) - pass order 0..5 as in the GH legend
DLF_LAYERS = [
    ("DLF-00_engrave",      7, (0, 0, 0)),        # engraved (hatch)             - black
    ("DLF-01_score_light",  5, (0, 0, 255)),      # (A) labels                   - blue
    ("DLF-02_score_medium", 3, (0, 255, 0)),      # (B) score next contour       - green
    ("DLF-03_score_strong", 4, (0, 255, 255)),    # graphics                     - cyan
    ("DLF-04_cut_inner",    6, (255, 0, 255)),    # (C) cut contours/inner lines - magenta
    ("DLF-05_cut_outer",    1, (255, 0, 0)),      # (D) board outline, last pass - red
]
SHEET_LAYER = ("DLF-99_sheet", 8, (128, 128, 128))  # non-cutting helper


def _new_doc():
    doc = ezdxf.new("R2010", setup=False)
    doc.header["$INSUNITS"] = 4   # millimetres
    doc.header["$MEASUREMENT"] = 1
    for name, aci, rgb in DLF_LAYERS + [SHEET_LAYER]:
        layer = doc.layers.add(name)
        layer.color = aci
        layer.rgb = rgb
    return doc


def sheet_to_dxf(nest: NestResult, sheet_index: int, params: SliceParams) -> bytes:
    doc = _new_doc()
    msp = doc.modelspace()

    def add(points, layer, force_closed=False):
        if len(points) < 2:
            return
        closed = force_closed or (len(points) > 3 and points[0] == points[-1])
        if closed and points[0] == points[-1]:
            points = points[:-1]
        msp.add_lwpolyline(points, format="xy", close=closed,
                           dxfattribs={"layer": layer})

    if params.sheet_outline:
        w, h = nest.sheet_width, nest.sheet_height
        add([(0, 0), (w, 0), (w, h), (0, h)], SHEET_LAYER[0], force_closed=True)

    for pl in nest.placements:
        if pl.sheet != sheet_index:
            continue
        p = pl.piece
        add(pl.transform(p.outer), "DLF-05_cut_outer", force_closed=True)
        for line in p.holes:
            add(pl.transform(line), "DLF-04_cut_inner")
        for line in p.score:
            add(pl.transform(line), "DLF-02_score_medium")
        for stroke in p.labels:
            add(pl.transform(stroke), "DLF-01_score_light")
        if p.hatch:
            from .hatch import HATCH_COLORS
            for entry in p.hatch:
                layer = HATCH_COLORS.get(entry["color"], HATCH_COLORS["green"])[0]
                for line in entry["lines"]:
                    add(pl.transform(line), layer)

    buf = io.StringIO()
    doc.write(buf)
    return buf.getvalue().encode("utf-8")


def cutting_report(nest: NestResult, params: SliceParams, slice_result, dtm_summary: dict) -> str:
    lines = [
        "DL TerrainSlicer - cutting report",
        f"date: {date.today().isoformat()}",
        "",
        f"scale 1:{params.scale:g}   material thickness {params.thickness_mm:g} mm   "
        f"vertical exaggeration {params.vertical_exaggeration:g}",
        f"real-world contour interval: {slice_result.world_interval:g} (DTM units, normally m)",
        f"model footprint: {slice_result.model_width:.0f} x {slice_result.model_height:.0f} mm, "
        f"{slice_result.n_levels} contour levels on {slice_result.n_boards} boards (offset method)",
        f"sheet: {params.sheet_width_mm:g} x {params.sheet_height_mm:g} mm, "
        f"margin {params.sheet_margin_mm:g} mm, spacing {params.part_spacing_mm:g} mm",
        f"sheets needed: {nest.n_sheets}",
    ]
    if slice_result.glue_min is not None:
        lines.append(f"narrowest glue strip (score line to back cutline): "
                     f"{slice_result.glue_min:.1f} mm")
    if params.labels_enabled and params.label_density > 0:
        lines.append("labels: hidden in the glue zones - covered by the next ring once "
                     "glued; topmost rings stay unnumbered" if params.label_hidden
                     else "labels: engraved on the visible rings")
    zl = slice_result.z_levels
    if zl:
        shown = ", ".join(f"{z:g}" for z in zl[1:15]) + (", ..." if len(zl) > 15 else "")
        mode = "0 m altitude (origin)" if params.base_mode == "zero" else "lowest point of DTM"
        lines.append(f"slicing base: {mode}; base plate at {zl[0]:g}, contours at: {shown}")
    lines += [
        "",
        "laser passes (per DXF layer, cut outlines LAST):",
        "  0  DLF-00_engrave       black    engraving (reserved)",
        "  1  DLF-01_score_light   blue     (A) contour-number labels",
        "  2  DLF-02_score_medium  green    (B) next contour - glue reference",
        "  3  DLF-03_score_strong  cyan     graphics (reserved)",
        "  4  DLF-04_cut_inner     magenta  (C) contour cutlines",
        "  5  DLF-05_cut_outer     red      (D) board outline - run last",
        "  (DLF-99_sheet is the sheet boundary - do not cut)",
        "",
        "boards per sheet:",
    ]
    for s in range(nest.n_sheets):
        entries = [f"board {pl.piece.board} (contours {pl.piece.levels})"
                   for pl in nest.placements if pl.sheet == s]
        lines.append(f"  sheet_{s + 1:02d}.dxf: " + "; ".join(entries))
    if nest.unplaced:
        lines.append("")
        lines.append(f"NOT PLACED (board larger than sheet): boards "
                     f"{sorted(p.board for p in nest.unplaced)} - "
                     f"increase sheet size or reduce model scale")
    for w in slice_result.warnings + nest.warnings:
        lines.append(f"note: {w}")
    lines.append("")
    lines.append("assembly (offset method): every contour is cut exactly once - the ring")
    lines.append("between two cutlines on board k is model layer k, k+N, ... Start with the")
    lines.append("lowest ring of board 0, then glue the next board's ring onto it, aligned")
    lines.append("to the green score line; the engraved number is the assembly order.")
    return "\n".join(lines)


def export_zip(nest: NestResult, params: SliceParams, slice_result, dtm_summary: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for s in range(nest.n_sheets):
            zf.writestr(f"sheet_{s + 1:02d}.dxf", sheet_to_dxf(nest, s, params))
        zf.writestr("cutting_report.txt", cutting_report(nest, params, slice_result, dtm_summary))
    return buf.getvalue()
