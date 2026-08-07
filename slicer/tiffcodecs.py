"""Pure-Python TIFF codecs for environments without the `imagecodecs` package.

The browser build runs on Pyodide, which does not ship `imagecodecs` at all,
and the desktop build deliberately avoids it (a ~30 MB binary wheel for two
small functions). Without it, tifffile raises
``<COMPRESSION.LZW: 5> requires the 'imagecodecs' package`` on the files QGIS
and GDAL write BY DEFAULT: LZW compression, usually with the floating-point
predictor for float32 DEMs. A real user hit exactly that on launch day.

tifffile already bundles a fallback module (`tifffile._imagecodecs`) covering
zlib/lzma/zstd/packbits/delta — it is only missing `lzw_decode` and
`floatpred_decode`. `install()` adds these two, implemented here in
numpy/pure Python, onto that fallback module whenever the real package is
absent. tifffile's dispatch then finds them exactly where it already looks,
and its codec-provenance check passes because the functions declare the
fallback module as their home.

The LZW decoder is a port of the pure-Python decoder that shipped in older
tifffile releases (BSD, Christoph Gohlke) - with thanks; decode-only, TIFF
variant: MSB-first bit packing, 9->12 bit codes, ClearCode 256, EOI 257, and
the "early change" code-width bump at table sizes 511/1023/2047.
"""
from __future__ import annotations

import numpy as np


def lzw_decode(data, /, *, buffersize: int = 0, out=None) -> bytes:
    """Decompress TIFF-variant LZW. `out` may be a size hint (ignored)."""
    data = bytes(data)
    if not data:
        return b""

    result: list[bytes] = []
    append = result.append
    table: list[bytes] = []
    prev = b""
    bitw = 9
    bitpos = 0
    maxbits = len(data) * 8

    def init_table() -> list[bytes]:
        return [bytes([i]) for i in range(256)] + [b"", b""]

    while bitpos + bitw <= maxbits:
        i = bitpos >> 3
        # 4 bytes always cover a <=12-bit code at any alignment
        chunk = int.from_bytes(data[i:i + 4].ljust(4, b"\0"), "big")
        code = (chunk >> (32 - bitw - (bitpos & 7))) & ((1 << bitw) - 1)
        bitpos += bitw

        if code == 257:                      # EndOfInformation
            break
        if code == 256:                      # ClearCode
            table = init_table()
            bitw = 9
            prev = b""
            continue
        if not table:                        # stream must start with a Clear
            raise ValueError("corrupt LZW stream: no ClearCode")

        if code < len(table):
            entry = table[code]
            if prev:
                table.append(prev + entry[:1])
        elif code == len(table) and prev:
            entry = prev + prev[:1]
            table.append(entry)
        else:
            raise ValueError("corrupt LZW stream: code out of range")
        append(entry)
        prev = entry

        n = len(table)
        if n == 511:
            bitw = 10
        elif n == 1023:
            bitw = 11
        elif n == 2047:
            bitw = 12

    return b"".join(result)


def floatpred_decode(data, /, *, axis: int = -1, dist: int = 1, out=None):
    """Reverse the TIFF floating-point predictor (tag 317 = 3).

    Encoding was: per scanline, split every sample into its bytes, store all
    high bytes first (big-endian byte planes), then difference the byte
    stream. tifffile hands us the decompressed segment as a numpy array of
    the TARGET dtype - shape (depth, length, width, samples), axis=-2 - whose
    underlying bytes are still in predicted planar form.
    """
    a = np.asarray(data)
    itemsize = a.dtype.itemsize
    if axis % a.ndim != a.ndim - 2:
        raise NotImplementedError("floatpred_decode: expected axis=-2")
    width = a.shape[-2]
    samples = a.shape[-1]
    stride = width * samples * itemsize      # bytes per scanline

    buf = np.ascontiguousarray(a)
    if not buf.flags.writeable:              # tifffile may pass a frombuffer view
        buf = buf.copy()
    b = buf.view(np.uint8).reshape(-1, stride)
    np.add.accumulate(b, axis=1, out=b)      # undo differencing, mod 256

    # byte planes -> per-sample big-endian bytes
    planes = b.reshape(-1, itemsize, width * samples)
    grouped = np.ascontiguousarray(np.moveaxis(planes, 1, 2))
    big = grouped.reshape(-1, itemsize).view(a.dtype.newbyteorder(">"))
    res = big.astype(a.dtype).reshape(a.shape)

    # tifffile always uses the RETURN value (data_array = unpredict(...)), and
    # the `out` it passes can be a read-only frombuffer view - write into it
    # only when that is actually possible.
    if isinstance(out, np.ndarray) and out.flags.writeable:
        out[...] = res
        return out
    return res


# tifffile refuses codecs whose home module it does not recognise - these two
# genuinely live in the fallback module once install() has run, so say so.
lzw_decode.__module__ = "tifffile._imagecodecs"
floatpred_decode.__module__ = "tifffile._imagecodecs"

_installed = False


def install() -> None:
    """Give tifffile LZW + float-predictor support when imagecodecs is absent."""
    global _installed
    if _installed:
        return
    _installed = True
    try:
        import imagecodecs  # noqa: F401  - the real thing; nothing to do
        return
    except ImportError:
        pass
    from tifffile import _imagecodecs as fallback
    if not hasattr(fallback, "lzw_decode"):
        fallback.lzw_decode = lzw_decode
    if not hasattr(fallback, "floatpred_decode"):
        fallback.floatpred_decode = floatpred_decode
