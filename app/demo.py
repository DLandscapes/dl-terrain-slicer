"""Built-in demo terrain so the app can be tried without a GeoTIFF at hand."""
from __future__ import annotations

import numpy as np

from slicer.dtm import DTM


def demo_dtm(size: int = 300, cell: float = 5.0) -> DTM:
    """Two gaussian hills and a depression, ~60 m relief over 1.5 x 1.5 km."""
    y, x = np.mgrid[0:size, 0:size].astype(np.float64)
    c = size / 2
    z = 100.0 + 55.0 * np.exp(-(((x - c * 0.7) ** 2 + (y - c * 0.8) ** 2) / (2 * (size / 6) ** 2)))
    z += 35.0 * np.exp(-(((x - c * 1.5) ** 2 + (y - c * 1.3) ** 2) / (2 * (size / 8) ** 2)))
    z -= 18.0 * np.exp(-(((x - c * 1.1) ** 2 + (y - c * 0.45) ** 2) / (2 * (size / 12) ** 2)))
    return DTM(elevation=z.astype(np.float32), cell_size=cell, source_name="demo-terrain")
