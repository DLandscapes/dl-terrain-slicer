"""Generate synthetic GeoTIFF DTMs for testing.

Run directly to write the files into tests/data/.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import tifffile

DATA_DIR = Path(__file__).resolve().parent / "data"


def _write_geotiff(path: Path, arr: np.ndarray, cell: float, nodata: float | None = None):
    extratags = [
        # ModelPixelScaleTag: sx, sy, sz
        (33550, "d", 3, (cell, cell, 0.0)),
        # ModelTiepointTag: raster (0,0,0) -> arbitrary world origin
        (33922, "d", 6, (0.0, 0.0, 0.0, 500000.0, 6600000.0, 0.0)),
    ]
    if nodata is not None:
        extratags.append((42113, "s", 0, str(nodata)))
    path.parent.mkdir(parents=True, exist_ok=True)
    tifffile.imwrite(path, arr.astype(np.float32), extratags=extratags)


def make_hills(path: Path | None = None, size: int = 300, cell: float = 5.0) -> Path:
    """Gaussian hills + a depression + a nodata border corner. ~60 m relief."""
    path = path or DATA_DIR / "hills.tif"
    y, x = np.mgrid[0:size, 0:size].astype(np.float64)
    cx, cy = size / 2, size / 2
    z = 100.0 + 55.0 * np.exp(-(((x - cx * 0.7) ** 2 + (y - cy * 0.8) ** 2) / (2 * (size / 6) ** 2)))
    z += 35.0 * np.exp(-(((x - cx * 1.5) ** 2 + (y - cy * 1.3) ** 2) / (2 * (size / 8) ** 2)))
    z -= 18.0 * np.exp(-(((x - cx * 1.1) ** 2 + (y - cy * 0.45) ** 2) / (2 * (size / 12) ** 2)))
    nodata = -9999.0
    z[:size // 8, :size // 8] = nodata  # nodata corner
    _write_geotiff(path, z, cell, nodata)
    return path


def make_ramp(path: Path | None = None, size: int = 50, cell: float = 10.0) -> Path:
    """Plain linear ramp, 0..20 m: contour counts are exactly predictable."""
    path = path or DATA_DIR / "ramp.tif"
    y, _ = np.mgrid[0:size, 0:size].astype(np.float64)
    z = 20.0 * y / (size - 1)
    _write_geotiff(path, z, cell)
    return path


if __name__ == "__main__":
    print(make_hills())
    print(make_ramp())
