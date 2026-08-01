"""Load a terrain mesh (.obj) and rasterize it to a heightfield DTM.

The mesh is treated like the Grasshopper original's input: a single terrain
surface in real-world units (metres). It is sampled onto a regular grid via
per-triangle barycentric rasterization, after which the whole existing
raster pipeline (contours, boards, nesting, DXF) applies unchanged.

Up axis is auto-detected: of Y and Z, the axis with the smallest extent is
taken as elevation (handles both Blender's Z-up and Y-up exports).
"""
from __future__ import annotations

import io
import math

import numpy as np

from .dtm import DTM


def parse_obj(data: bytes) -> tuple[np.ndarray, np.ndarray]:
    """Return (vertices Nx3 float64, faces Mx3 int32) from OBJ bytes."""
    verts: list[tuple] = []
    faces: list[tuple] = []
    for raw in io.BytesIO(data).read().decode("utf-8", "replace").splitlines():
        if raw.startswith("v "):
            parts = raw.split()
            verts.append((float(parts[1]), float(parts[2]), float(parts[3])))
        elif raw.startswith("f "):
            idx = [int(p.split("/")[0]) for p in raw.split()[1:]]
            idx = [i - 1 if i > 0 else len(verts) + i for i in idx]
            for k in range(1, len(idx) - 1):  # fan-triangulate polygons
                faces.append((idx[0], idx[k], idx[k + 1]))
    if not verts or not faces:
        raise ValueError("OBJ contains no usable geometry (v/f records)")
    return np.asarray(verts, dtype=np.float64), np.asarray(faces, dtype=np.int32)


def _plan_coords(verts: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, str]:
    """Detect the up axis and return (plan_x, plan_y, elevation, axis_name)."""
    ranges = verts.max(axis=0) - verts.min(axis=0)
    if ranges[1] <= ranges[2]:  # Y-up (Blender default OBJ export)
        return verts[:, 0], -verts[:, 2], verts[:, 1], "Y"
    return verts[:, 0], verts[:, 1], verts[:, 2], "Z"


def rasterize(px: np.ndarray, py: np.ndarray, pz: np.ndarray,
              faces: np.ndarray, cell: float) -> tuple[np.ndarray, float, float]:
    """Sample the triangle mesh onto a grid. Returns (grid row0=north, x0, y0).

    Vectorised: the original per-triangle Python loop cost ~23 s on a 500k-face
    mesh, nearly all of it interpreter overhead on tiny arrays. Since the grid
    is matched to the mesh's own density, almost every triangle's bounding box
    covers only a handful of grid nodes - so faces are GROUPED BY WINDOW SHAPE
    and each group is evaluated as one (faces x window-cells) broadcast, with
    the max-z scatter done by np.fmax.at (unbuffered, so duplicate cells across
    faces keep the maximum; fmax, not maximum, so NaN means "empty" and never
    wins). The result is bit-identical to the loop: per-(face, node) arithmetic
    is unchanged and max is order-independent.
    """
    x0, y0 = float(px.min()), float(py.min())
    nx = max(2, int(math.ceil((px.max() - x0) / cell)) + 1)
    ny = max(2, int(math.ceil((py.max() - y0) / cell)) + 1)
    grid = np.full((ny, nx), np.nan, dtype=np.float32)
    flat = grid.ravel()

    fx, fy, fz = px[faces], py[faces], pz[faces]   # (F, 3) each
    det = ((fy[:, 1] - fy[:, 2]) * (fx[:, 0] - fx[:, 2])
           + (fx[:, 2] - fx[:, 1]) * (fy[:, 0] - fy[:, 2]))
    keep = np.abs(det) >= 1e-9                     # drop degenerate triangles

    # integer node windows, exactly as the loop computed them
    i0 = ((fx.min(axis=1) - x0) / cell).astype(np.int64)
    i1 = np.minimum(((fx.max(axis=1) - x0) / cell).astype(np.int64) + 2, nx)
    j0 = ((fy.min(axis=1) - y0) / cell).astype(np.int64)
    j1 = np.minimum(((fy.max(axis=1) - y0) / cell).astype(np.int64) + 2, ny)
    wi, hj = i1 - i0, j1 - j0
    keep &= (wi > 0) & (hj > 0)

    order = np.flatnonzero(keep)
    shape_key = hj[order] * 8192 + wi[order]
    # cap the faces-x-cells broadcast at ~8M float64 elements per slab
    MAX_ELEMS = 8_000_000

    for key in np.unique(shape_key):
        sel = order[shape_key == key]
        h, w = int(key // 8192), int(key % 8192)
        oj, oi = np.divmod(np.arange(h * w), w)    # the window's local nodes
        step = max(1, MAX_ELEMS // (h * w))
        for s in range(0, len(sel), step):
            f = sel[s:s + step]
            I = i0[f, None] + oi[None, :]          # (F, hw) node columns
            J = j0[f, None] + oj[None, :]
            GX = x0 + I * cell
            GY = y0 + J * cell
            xs2 = fx[f, 2, None]
            ys2 = fy[f, 2, None]
            d = det[f, None]
            w0 = ((fy[f, 1, None] - ys2) * (GX - xs2)
                  + (xs2 - fx[f, 1, None]) * (GY - ys2)) / d
            w1 = ((ys2 - fy[f, 0, None]) * (GX - xs2)
                  + (fx[f, 0, None] - xs2) * (GY - ys2)) / d
            w2 = 1.0 - w0 - w1
            mask = (w0 >= -1e-6) & (w1 >= -1e-6) & (w2 >= -1e-6)
            if not mask.any():
                continue
            zval = (w0 * fz[f, 0, None] + w1 * fz[f, 1, None]
                    + w2 * fz[f, 2, None]).astype(np.float32)
            np.fmax.at(flat, (J * nx + I)[mask], zval[mask])
    return grid[::-1], x0, y0  # flip: row 0 = north


def load_obj(data: bytes, name: str = "") -> tuple[DTM, list[str]]:
    warnings: list[str] = []
    verts, faces = parse_obj(data)
    px, py, pz, up = _plan_coords(verts)
    if up == "Y":
        warnings.append("mesh interpreted as Y-up (Blender default); elevation taken from Y")
    # (the old "rasterizing may take a while" warning died with build 22's
    # vectorised rasterizer: 500k faces now sample in well under a second)

    # grid resolution matched to the mesh's own density, bounded for interactivity
    extent = max(px.max() - px.min(), py.max() - py.min())
    target = min(1200, max(250, int(math.sqrt(len(verts)) * 2.2)))
    cell = extent / target
    grid, x0, y0 = rasterize(px, py, pz, faces, cell)
    if not np.isfinite(grid).any():
        raise ValueError("mesh rasterization produced no elevation samples")
    dtm = DTM(elevation=grid, cell_size=float(cell), source_name=name,
              origin_x=x0, origin_y=y0)
    warnings.append(
        f"mesh sampled at {cell:.2f} unit cells ({grid.shape[1]} x {grid.shape[0]} grid); "
        f"mesh units are assumed to be metres")
    return dtm, warnings
