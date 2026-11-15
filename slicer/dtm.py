"""Load a digital terrain model from a GeoTIFF or an ESRI ASCII grid, without GDAL.

GeoTIFF: the elevation grid via tifffile, the pixel size from the GeoTIFF
tags (ModelPixelScale 33550 / ModelTransformation 34264), nodata from
GDAL_NODATA (42113). ASCII grid (.asc): the six-line header plus the values.
CRS is ignored on purpose: slicing only needs the grid and the cell size in
ground units.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import tifffile

from . import tiffcodecs

# QGIS/GDAL write LZW (often with the float predictor) by default; without
# this, every such file fails with "requires the 'imagecodecs' package".
tiffcodecs.install()


@dataclass
class DTM:
    elevation: np.ndarray      # float32, NaN = nodata, row 0 = north edge
    cell_size: float           # ground units (normally metres) per pixel
    source_name: str = ""
    downsampled_by: int = 1
    # world position (source CRS) of the CENTRE of the first cell of the last
    # row - the grid node that slice_dtm places at local (0, 0); vector layers
    # are moved by exactly this to land on the terrain
    origin_x: float = 0.0
    origin_y: float = 0.0

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


def _pixel_is_point(tif: tifffile.TiffFile) -> bool:
    """GTRasterTypeGeoKey (1025) in the GeoKeyDirectory (34735): 2 = PixelIsPoint,
    i.e. the tiepoint names a cell CENTRE; the default (1, PixelIsArea) names a
    cell's outer corner."""
    tags = tif.pages[0].tags
    if 34735 in tags:
        v = list(tags[34735].value)
        for k in range(4, len(v) - 3, 4):           # entries after the 4-short header
            if v[k] == 1025 and v[k + 1] == 0:      # value stored inline
                return v[k + 3] == 2
    return False


def _nodata_value(tif: tifffile.TiffFile) -> float | None:
    tags = tif.pages[0].tags
    if 42113 in tags:  # GDAL_NODATA (ascii)
        try:
            return float(str(tags[42113].value).strip())
        except ValueError:
            return None
    return None


def _read_page(page):
    """Decode a TIFF page, single-threaded if the platform has no threads.

    Pyodide has no working pthreads, so `threading.Thread.start()` raises
    `RuntimeError: can't start new thread`. tifffile decodes segments through a
    ThreadPoolExecutor whenever `TiffPage.maxworkers` comes back >= 2, and it
    normally clamps that to `TIFF.MAXWORKERS` - which is 1 in the browser, so
    compressed rasters are safe by accident. The UNCOMPRESSED multi-tile branch
    ends in a hardcoded `return 2` that ignores MAXWORKERS altogether, so an
    uncompressed tiled GeoTIFF - exactly what a plain GDAL/QGIS export gives
    you - killed the upload with a message about threads, which tells a
    landscape architect nothing at all.

    Reported from the browser build on a 1400x1400 uncompressed DTM; the
    deflate-compressed version of the same raster loaded fine, which is what
    made it look arbitrary.

    The default is tried first so desktop keeps its parallel decode, and only a
    thread failure falls back. Decoding is not the bottleneck anyway - contour
    complexity is - so the single-threaded path costs little.
    """
    try:
        return page.asarray()
    except RuntimeError as exc:
        if "thread" not in str(exc).lower():
            raise
        return page.asarray(maxworkers=1)


def load_geotiff(path_or_bytes, name: str = "", max_dim: int = 2000) -> tuple[DTM, list[str]]:
    """Load a GeoTIFF DTM. Returns (dtm, warnings)."""
    warnings: list[str] = []
    with tifffile.TiffFile(path_or_bytes) as tif:
        arr = _read_page(tif.pages[0])
        if arr.ndim == 3:  # multi-band: take the first band
            warnings.append(f"raster has {arr.shape[0] if arr.shape[0] < arr.shape[-1] else arr.shape[-1]} bands; using band 1")
            arr = arr[0] if arr.shape[0] <= 4 else arr[..., 0]
        cell = _pixel_scale(tif)
        nodata = _nodata_value(tif)
        tie = _tiepoint(tif)
        centre = _pixel_is_point(tif)
    arr = np.asarray(arr, dtype=np.float32)
    if cell is None:
        cell = 1.0
        warnings.append("no georeferencing tags found; assuming cell size of 1.0 ground unit per pixel")
    x00, y00 = 0.0, (arr.shape[0] - 1) * cell     # no tiepoint: local frame only
    if tie is not None:
        ti, tj, tx, ty = tie
        half = 0.0 if centre else cell / 2        # PixelIsArea ties a cell's outer corner
        x00, y00 = tx - ti * cell + half, ty + tj * cell - half
    return _finish(arr, cell, x00, y00, nodata, name, max_dim, warnings)


def load_asc(data: bytes, name: str = "", max_dim: int = 2000) -> tuple[DTM, list[str]]:
    """Load an ESRI ASCII grid (.asc). Returns (dtm, warnings).

    Header keys (any case, any order): ncols, nrows, xllcorner|xllcenter,
    yllcorner|yllcenter, cellsize, optional nodata_value; then nrows x ncols
    values, north row first."""
    text = data.decode("latin-1")
    head: dict[str, str] = {}
    pos = 0
    for line in text.splitlines(keepends=True):
        parts = line.split()
        if len(parts) == 2 and parts[0][:1].isalpha():
            head[parts[0].lower()] = parts[1]
            pos += len(line)
        elif parts:
            break
        else:
            pos += len(line)
    try:
        cols, rows, cell = int(head["ncols"]), int(head["nrows"]), float(head["cellsize"])
    except KeyError as exc:
        raise ValueError(f"not an ASCII grid: header has no {exc.args[0]}") from None
    values = np.array(text[pos:].split(), dtype=np.float32)
    if values.size != rows * cols:
        raise ValueError(f"ASCII grid header says {rows} x {cols} values, the file holds {values.size}")
    if "xllcenter" in head:
        x00 = float(head["xllcenter"])
        y00 = float(head["yllcenter"]) + (rows - 1) * cell
    else:
        x00 = float(head["xllcorner"]) + cell / 2
        y00 = float(head["yllcorner"]) + (rows - 0.5) * cell
    nodata = float(head["nodata_value"]) if "nodata_value" in head else None
    return _finish(values.reshape(rows, cols), cell, x00, y00, nodata, name, max_dim, [])


def _finish(arr, cell, x00, y00, nodata, name, max_dim, warnings) -> tuple[DTM, list[str]]:
    """Nodata, sentinels and downsampling, shared by every raster reader.
    (x00, y00): world position of the centre of the top-left cell."""
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
    # the kept last row is (rows - 1) cells of the NEW size below the first row
    return DTM(elevation=arr, cell_size=float(cell), source_name=name, downsampled_by=step,
               origin_x=x00, origin_y=y00 - (arr.shape[0] - 1) * cell), warnings
