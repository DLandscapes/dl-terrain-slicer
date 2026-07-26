"""Tiny grayscale PNG encoder (pure Python, for the hillshade thumbnail)."""
from __future__ import annotations

import struct
import zlib

import numpy as np


def encode_gray_png(img: np.ndarray) -> bytes:
    """img: 2-D uint8 array -> PNG bytes."""
    h, w = img.shape
    raw = b"".join(b"\x00" + img[r].tobytes() for r in range(h))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)  # 8-bit grayscale
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def hillshade_png(elevation: np.ndarray, cell: float, max_dim: int = 400) -> bytes:
    """Simple hillshade (az 315, alt 45) of a DTM with NaN nodata."""
    e = elevation
    step = max(1, int(np.ceil(max(e.shape) / max_dim)))
    e = e[::step, ::step]
    filled = np.where(np.isfinite(e), e, np.nanmean(e))
    gy, gx = np.gradient(filled, cell * step)
    az, alt = np.radians(315.0), np.radians(45.0)
    slope = np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)
    shade = (np.sin(alt) * np.cos(slope)
             + np.cos(alt) * np.sin(slope) * np.cos(az - aspect))
    img = np.clip((shade * 0.5 + 0.5) * 255, 0, 255).astype(np.uint8)
    img[~np.isfinite(e)] = 255
    return encode_gray_png(img)
