"""Shelf packing of pieces onto material sheets.

Bounding-box shelf packing with optional 90-degree rotation: simple,
predictable, and safe for laser work (no polygon overlap possible since
bounding boxes never overlap). Pieces that exceed the usable sheet even
rotated are reported instead of silently dropped.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .contours import Board


@dataclass
class Placement:
    piece: Board
    sheet: int
    dx: float        # translation applied after optional rotation, sheet mm
    dy: float
    rotated: bool    # 90 degrees counter-clockwise

    def transform(self, pts: list[tuple]) -> list[tuple]:
        x0, y0, x1, y1 = self.piece.bounds
        out = []
        for x, y in pts:
            lx, ly = x - x0, y - y0
            if self.rotated:
                w = y1 - y0
                lx, ly = w - ly, lx
            out.append((round(lx + self.dx, 3), round(ly + self.dy, 3)))
        return out


@dataclass
class NestResult:
    placements: list[Placement]
    n_sheets: int
    sheet_width: float
    sheet_height: float
    unplaced: list[Board] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def nest(pieces: list[Board], sheet_w: float, sheet_h: float,
         margin: float, spacing: float) -> NestResult:
    usable_w = sheet_w - 2 * margin
    usable_h = sheet_h - 2 * margin
    warnings: list[str] = []
    unplaced: list[Board] = []

    def fits(w, h):
        return w <= usable_w and h <= usable_h

    todo = []
    for p in pieces:
        w, h = p.size
        if fits(w, h):
            todo.append((p, False))
        elif fits(h, w):
            todo.append((p, True))
        else:
            unplaced.append(p)
    if unplaced:
        ids = ", ".join(str(p.board) for p in unplaced[:8])
        warnings.append(
            f"{len(unplaced)} board(s) larger than the usable sheet "
            f"({usable_w:g} x {usable_h:g} mm), boards: {ids} - "
            f"increase sheet size or reduce model scale")

    # big pieces first; prefer the orientation that is wider than tall
    order = []
    for p, must_rotate in todo:
        w, h = p.size
        rot = must_rotate
        if not must_rotate and h > w and fits(h, w):
            rot = True
        pw, ph = (h, w) if rot else (w, h)
        order.append((p, rot, pw, ph))
    order.sort(key=lambda t: t[3], reverse=True)  # by placed height

    placements: list[Placement] = []
    sheet = 0
    cx, cy, shelf_h = 0.0, 0.0, 0.0
    for p, rot, pw, ph in order:
        placed = False
        while not placed:
            if cx + pw <= usable_w and cy + ph <= usable_h:
                placements.append(Placement(p, sheet, margin + cx, margin + cy, rot))
                cx += pw + spacing
                shelf_h = max(shelf_h, ph)
                placed = True
            elif cy + shelf_h + spacing + ph <= usable_h and shelf_h > 0:
                cy += shelf_h + spacing
                cx, shelf_h = 0.0, 0.0
            else:
                sheet += 1
                cx, cy, shelf_h = 0.0, 0.0, 0.0
    n_sheets = (max(pl.sheet for pl in placements) + 1) if placements else 0
    return NestResult(placements=placements, n_sheets=n_sheets,
                      sheet_width=sheet_w, sheet_height=sheet_h,
                      unplaced=unplaced, warnings=warnings)
