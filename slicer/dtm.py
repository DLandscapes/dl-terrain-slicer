"""Load a digital terrain model from a GeoTIFF without GDAL.

Reads the elevation grid with tifffile and pulls the pixel size from the
GeoTIFF tags (ModelPixelScale 33550 / ModelTransformation 34264). Nodata
comes from GDAL_NODATA (42113). CRS is ignored on purpose: slicing only
needs the grid and the cell size in ground units.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import tifffile


@dataclass
class DTM:
    elevation: np.ndarray      # float32, NaN = nodata, row 0 = north edge
    cell_size: float           # ground units (normally metres) per pixel
    source_name: str = ""
    downsampled_by: int = 1
    origin_x: float = 0.0      # world coordinate of the grid's west edge (source CRS)
    origin_y: float = 0.0      # world coordinate of the grid's south edge (source CRS)

    @property
    def zmin(self) -> float:
        return float(np.nanmin(self.elevation))

    @property
    def zmax(self) -> float:
        return float(np.nanmax(self.elevation))

    @property
    def width_world(self) -> float:
        return self.elevation.shape[1] * self.cell_size

    @property
    def height_world(self) -> float:
        return self.elevation.shape[0] * self.cell_size

    def summary(self) -> dict:
        rows, cols = self.elevation.shape
        return {
            "rows": rows,
            "cols": cols,
            "cell_size": self.cell_size,
            "zmin": self.zmin,
            "zmax": self.zmax,
            "width_world": self.width_world,
            "height_world": self.height_world,
            "downsampled_by": self.downsampled_by,
            "nodata_fraction": float(np.isnan(self.elevation).mean()),
        }


def _pixel_scale(tif: tifffile.TiffFile) -> float | None:
    page = tif.pages[0]
    tags = page.tags
    if 33550 in tags:  # ModelPixelScaleTag: (sx, sy, sz)
        sx, sy = tags[33550].value[0], tags[33550].value[1]
        if sx > 0 and sy > 0:
            if abs(sx - sy) > 1e-6 * max(sx, sy):
                return float((sx + sy) / 2.0)
            return float(sx)
    if 34264 in tags:  # ModelTransformationTag: 4x4 affine, row-major
        m = tags[34264].value
        sx = (m[0] ** 2 + m[4] ** 2) ** 0.5
        if sx > 0:
            return float(sx)
    return None


def _tiepoint(tif: tifffile.TiffFile) -> tuple[float, float, float, float] | None:
    """Raw ModelTiepoint (i, j, X, Y): raster point (i,j) sits at world (X,Y)."""
    tags = tif.pages[0].tags
    if 33922 in tags:  # ModelTiepointTag: i, j, k, X, Y, Z
        v = tags[33922].value
        if len(v) >= 6:
            return float(v[0]), float(v[1]), float(v[3]), float(v[4])
    return None


def _nodata_value(tif: tifffile.TiffFile) -> float | None:
    tags = tif.pages[0].tags
    if 42113 in tags:  # GDAL_NODATA (ascii)
        try:
            return float(str(tags[42113].value).strip())
        except ValueError:
            return None
    return None


def load_geotiff(path_or_bytes, name: str = "", max_dim: int = 2000) -> tuple[DTM, list[str]]:
    """Load a GeoTIFF DTM. Returns (dtm, warnings)."""
    warnings: list[str] = []
    with tifffile.TiffFile(path_or_bytes) as tif:
        arr = tif.pages[0].asarray()
        if arr.ndim == 3:  # multi-band: take the first band
            warnings.append(f"raster has {arr.shape[0] if arr.shape[0] < arr.shape[-1] else arr.shape[-1]} bands; using band 1")
            arr = arr[0] if arr.shape[0] <= 4 else arr[..., 0]
        cell = _pixel_scale(tif)
        nodata = _nodata_value(tif)
        tie = _tiepoint(tif)
    arr = np.asarray(arr, dtype=np.float32)
    if cell is None:
        cell = 1.0
        warnings.append("no georeferencing tags found; assuming cell size of 1.0 ground unit per pixel")
    origin_x = origin_y = 0.0
    if tie is not None:
        ti, tj, tx, ty = tie
        origin_x = tx - ti * cell                      # west edge (col 0)
        origin_y = (ty + tj * cell) - arr.shape[0] * cell  # south edge (below last row)
    if nodata is not None:
        arr[arr == np.float32(nodata)] = np.nan
    # common unflagged nodata sentinels
    for sentinel in (-9999.0, -32767.0, -32768.0, 3.4028235e38, -3.4028235e38):
        if np.any(arr == np.float32(sentinel)):
            arr[arr == np.float32(sentinel)] = np.nan
            warnings.append(f"treated raw value {sentinel} as nodata")
    if not np.isfinite(arr).any():
        raise ValueError("raster contains no valid elevation values")

    step = 1
    while max(arr.shape) // step > max_dim:
        step += 1
    if step > 1:
        arr = arr[::step, ::step]
        cell *= step
        warnings.append(f"raster downsampled by factor {step} for interactive use (cell size now {cell:g})")
    return DTM(elevation=arr, cell_size=float(cell), source_name=name, downsampled_by=step,
               origin_x=origin_x, origin_y=origin_y), warnings
