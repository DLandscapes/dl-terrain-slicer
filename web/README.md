# Browser build (WebAssembly)

The same app as the desktop version, running entirely inside the visitor's
browser: Pyodide executes the unmodified Python slicer, so no terrain file is
ever uploaded anywhere and no Python is needed on the web server.

The output of `build.py` is a folder of plain static files — exactly what
`digital-landscapes.com` (WordPress.com Atomic, no server-side Python) can
host over SFTP.

## How the one codebase serves two builds

```
app/core.py      all app logic (no FastAPI, no disk I/O)
   |
   +-- app/main.py    FastAPI wrapper   -> desktop / start.bat
   +-- web/bridge.py  JSON wrapper      -> web/worker.js -> Pyodide
```

The frontend is shared too. `app/static/api.js` picks the transport:
`fetch("api/…")` normally, `postMessage` to the worker when `build.py` has
injected `window.TS_MODE = "wasm"` into the page. `app.js` never knows which.

Because both builds run the same `slicer/` package, the DXF output is
identical — `compare_export.py` is there to keep proving it.

## Build it

```
python web/fetch_pyodide.py --version 314.0.3     # once, downloads ~21 MB
python web/build.py --serve                       # -> web/dist, http://localhost:8767
```

Pinned: **Pyodide 314.0.3** (Python 3.14, numpy 2.4.3, shapely 2.1.2,
contourpy 1.3.3) plus tifffile 2026.7.14 and ezdxf 1.4.4 as pure-python
wheels. Since build 31 those wheels travel unpacked inside `slicer.zip`
(~3 MB) instead of as five separate downloads, and there is no
`manifest.json` any more: a start asks the server for fewer files. `web/dist`
ends up at about 21 MB, of which the visitor's browser caches ~13 MB of
runtime on first start.

On Windows, stop the `--serve` server before rebuilding: it holds the files
it is serving and `build.py` cannot replace them (it says so if it happens).

`fetch_pyodide.py` is the only step that uses the network. It puts the pinned
runtime into `web/vendor/` (git-ignored); `build.py` copies it into
`web/dist/` and never downloads anything.

**No CDN, on purpose.** Loading Pyodide from jsDelivr at runtime would send
every visitor's IP address to a third party — the same GDPR problem as
remotely hosted web fonts. The runtime is therefore served from our own
directory, like the site's fonts.

## Deploying

Upload `web/dist/` into a versioned directory and embed that URL in a
WordPress page — on digital-landscapes.com that is `/terrain-slicer-app/v1/`.
All asset paths are relative, so any sub-directory works, with one exception:
never name the directory after the page's own slug (`/terrain-slicer/`).
WordPress serves a real directory before a page of the same name, so the page
would disappear behind it.

The web server must send `.wasm` as `application/wasm`, otherwise the
runtime refuses to start.

## Limits of the browser build

Everything runs in one tab, so the guards in `api.js` apply:

| | limit |
|---|---|
| file size | warn at 50 MB, refuse at 200 MB |
| raster size | warn at 20 M cells, refuse at 60 M cells (read from the TIFF or ASCII-grid header, before decoding) |
| OBJ / STL meshes | OBJ since build 22, when the rasteriser was vectorised; STL since build 32; a dense mesh takes several seconds |

## Verifying a build

1. `python -m pytest tests -q` — `tests/test_bridge.py` exercises the whole
   worker protocol in CPython, no browser needed.
2. `python web/build.py --serve`, open <http://localhost:8767>, load the demo
   terrain, export a ZIP.
3. Export the same terrain and parameters from the desktop app, then
   `python web/compare_export.py desktop.zip browser.zip --tolerance 0.05`.

### Measured parity (build 17, Pyodide 314.0.3)

Every layer that the laser **cuts** is bit-identical between the two builds:

| terrain | cut inner | cut outer | score medium | sheet | contour numbers |
|---|---|---|---|---|---|
| demo, 1:5000 | exact | exact | exact | exact | shifted ≤ 0.047 mm |
| test.tif + polygon/line/point shapefiles, 1:500 | exact (99) | exact (4) | exact (993) | exact | 171 vs 173 polylines |

The contour numbers are the one place where the two builds can disagree, and
only because Pyodide ships numpy 2.4.3 while the desktop venv has 2.5.1: the
last bits of a centroid move a glyph by a few hundredths of a millimetre (two
orders of magnitude under a laser kerf). In the 1:500 test the glue strips
are only 0.9 mm wide — the case the app already warns about — so that same
float noise flips one borderline "does this number fit in the glue zone?"
decision, and one number is engraved on the visible ring instead of the glue
zone. Nothing that is cut moves. Both builds run the identical Python; if
the desktop venv and Pyodide ever ship the same numpy, this disappears.

### Measured parity (build 32: the new import formats)

Same files, same settings, browser vs desktop (`test outputs\2026-11-14 build
32 formats and menu\`): DXF, GeoJSON, GML and GeoPackage give the same layers,
names, kinds and notes; test.tif with all four at 1:500 cuts the same
shapes - three closed rings start at a different vertex (Hausdorff
distance 0.000000 mm, same area), and the light-score difference is the
label flip above, identical without any vector file. A DXF drawn in Rhino 8
over the OBJ: 21 levels, 1 sheet, 17 engraved strokes in both.

## Notes for the next person

* `worker.js` is an **ES module** worker (`{type: "module"}`): current
  Pyodide throws `Classic web workers are not supported` otherwise.
* `pyodide.mjs` dynamically imports `pyodide.asm.mjs` — both must be
  vendored, together with `pyodide.asm.wasm` and `python_stdlib.zip`.
* Only universal (`-py3-none-any`) wheels are packed into the build; `pip`
  otherwise hands you a wheel compiled for the build machine, which cannot
  load in WebAssembly. `build.py` says which wheels it skipped, and stops if a
  wheel needs a real installer (anything under `<name>.data/` but `data/`).
* **The host can refuse the engine's files.** digital-landscapes.com limits
  static downloads per connection and answers HTTP 429 (measured 2026-11-14).
  Since build 31 `api.js` starts a failed or stalled boot again in a fresh
  worker - at most 4 attempts, pauses 5 / 15 / 45 s with jitter - and says
  what went wrong; `worker.js` wraps `fetch`, so a refused download (Pyodide's
  own included) is reported at once instead of leaving Pyodide waiting for
  ever. If a Pyodide upgrade stops calling the global `fetch`, that report
  goes silent and only the watchdog (45 s, then 120 s per step) is left.
* GeoPackage files are read with `sqlite3.Connection.deserialize`; `sqlite3`
  is part of Pyodide's standard library, so no extra download (build 32).
* When deploying an update into the same directory, do not re-upload
  `static/pyodide/` unchanged: a new upload resets its Last-Modified date,
  and the browsers' cache reuse of those files depends on it.
