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
    """Sample the triangle mesh onto a grid. Returns (grid row0=north, x0, y0)."""
    x0, y0 = float(px.min()), float(py.min())
    nx = max(2, int(math.ceil((px.max() - x0) / cell)) + 1)
    ny = max(2, int(math.ceil((py.max() - y0) / cell)) + 1)
    grid = np.full((ny, nx), np.nan, dtype=np.float32)

    fx, fy, fz = px[faces], py[faces], pz[faces]   # (F, 3) each
    eps = 1e-9
    for i in range(len(faces)):
        xs, ys, zs = fx[i], fy[i], fz[i]
        det = (ys[1] - ys[2]) * (xs[0] - xs[2]) + (xs[2] - xs[1]) * (ys[0] - ys[2])
        if abs(det) < eps:
            continue
        i0 = int((xs.min() - x0) / cell)
        i1 = int((xs.max() - x0) / cell) + 2
        j0 = int((ys.min() - y0) / cell)
        j1 = int((ys.max() - y0) / cell) + 2
        gx = x0 + np.arange(i0, min(i1, nx)) * cell
        gy = y0 + np.arange(j0, min(j1, ny)) * cell
        if not len(gx) or not len(gy):
            continue
        GX, GY = np.meshgrid(gx, gy)
        w0 = ((ys[1] - ys[2]) * (GX - xs[2]) + (xs[2] - xs[1]) * (GY - ys[2])) / det
        w1 = ((ys[2] - ys[0]) * (GX - xs[2]) + (xs[0] - xs[2]) * (GY - ys[2])) / det
        w2 = 1.0 - w0 - w1
        mask = (w0 >= -1e-6) & (w1 >= -1e-6) & (w2 >= -1e-6)
        if not mask.any():
            continue
        zval = w0 * zs[0] + w1 * zs[1] + w2 * zs[2]
        window = grid[j0:min(j1, ny), i0:min(i1, nx)]
        window[...] = np.where(mask, np.fmax(window, zval.astype(np.float32)), window)
    return grid[::-1], x0, y0  # flip: row 0 = north


def load_obj(data: bytes, name: str = "") -> tuple[DTM, list[str]]:
    warnings: list[str] = []
    verts, faces = parse_obj(data)
    px, py, pz, up = _plan_coords(verts)
    if up == "Y":
        warnings.append("mesh interpreted as Y-up (Blender default); elevation taken from Y")
    if len(faces) > 250_000:
        warnings.append(
            f"dense mesh ({len(faces):,} faces) - rasterizing may take a while; "
            f"a decimated export (e.g. '-lower-res') is faster and usually just as good")

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
