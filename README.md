# DL Terrain Slicer

Turns a digital terrain model into laser-ready DXF files for a stacked
cardboard contour model, using the
[horizontal contour offset method](https://digital-landscapes.com/horizontal-contour-model-offset-method/).
A standalone replacement for the Grasshopper definition
`DL-Contour_offset_method_011.gh` — no Rhino, no GDAL, no GIS licence.

## Getting started

**What you need:** a terrain model as a **GeoTIFF** (`.tif`) — export one from
QGIS. Optionally shapefiles (`.shp`) for areas, lines or points you want
scored onto the model. You can try everything without your own data first.

### 1. Download

In the right-hand sidebar, under **Releases**, click the newest release
(e.g. "DL Terrain Slicer v0.1.0") — the *name*, not just the sidebar box.
That opens the release page. Scroll to the bottom, to **Assets**, and pick
the file for your computer:

| Your computer | File |
|---|---|
| Windows | `DL-Terrain-Slicer-…-windows-x64.zip` |
| Mac with M1/M2/M3/M4 | `DL-Terrain-Slicer-…-macos-arm64.zip` |
| Mac with Intel | `DL-Terrain-Slicer-…-macos-x64.zip` |

> ⚠️ **Not "Source code (zip)" or "Source code (tar.gz)".** GitHub adds those
> two to every release automatically, and they sit right below the file you
> want. They contain the Python source, which needs Python installed and a
> setup step — download one of those by mistake and you get a `start.bat`
> that reports a missing environment and closes. The app is the
> `DL-Terrain-Slicer-…` file.

### 2. Unpack and run

Unpack the zip somewhere ordinary — Desktop or Documents. **Do not run it
from inside the zip**; the app needs the whole folder. Inside you get
`DL-Terrain-Slicer` (the program) next to an `_internal` folder — keep them
together, the program will not start on its own.

Then run **`DL-Terrain-Slicer`**. There is no `start.bat` here; that file
belongs to the source code and needs Python.

The first launch your system will warn you that the app is unsigned. It is
not dangerous — signing requires a yearly developer subscription this project
does not have.

- **Windows** — "Windows protected your PC": *More info* → *Run anyway*
- **macOS** — right-click the app → *Open* → *Open*. If macOS still refuses,
  run `xattr -d com.apple.quarantine <unpacked folder>` once in Terminal.

### 3. Use it

A console window opens and your browser opens the app. **Leave the console
window alone** — it is the app's engine, and closing it stops the app.

1. Click **load demo terrain** to see how it works before using your own file.
2. For your own model, drag a GeoTIFF onto the drop area.
3. Set the **scale** (500 for 1:500) and the **material thickness**. Together
   these decide the real-world contour interval, shown in the panel.
4. Set the **number of boards** — the trade-off at the heart of the method:
   more boards means wider rings and more glue surface, but more material.
5. Watch **narrowest glue strip**. Under about 2 mm the model becomes
   difficult to glue; raise the number of boards until it is comfortable.
6. Look at the **Sheets** and **Stack 3D** views. Stack 3D can step through
   the assembly ring by ring.
7. Click **Export DXF (ZIP)**.

### 4. Before you cut

The ZIP holds one DXF per material sheet plus `cutting_report.txt` with your
settings and assembly notes. **Open the DXF and check the dimensions are what
you expect before putting anything in the laser.** Layers are named by laser
pass — run `DLF-05_cut_outer`, the board outline, last.

Your terrain files never leave your computer: the app does all its work
locally and never sends anything anywhere.

## How the offset method works

The app reads a **GeoTIFF digital terrain model**, slices it into contour
levels at material-thickness intervals and distributes them onto **N boards
following the offset method**: board k carries contours k, k+N, k+2N, … as
in-place cutlines. Each cutline does double duty — the back cutline of ring i
is the outer cutline of ring i+N — so **every contour is cut exactly once**
and the pieces are rings, not solid slabs, which is what saves material and
laser time. Boards are then packed onto material sheets and exported as **one
DXF per sheet** with the six DLF laser-pass layers:

| pass | layer | color | content |
|---|---|---|---|
| 0 | DLF-00_engrave | black | engraving (reserved for phase 2) |
| 1 | DLF-01_score_light | blue (0,0,255) | (A) contour-number labels (single-stroke) |
| 2 | DLF-02_score_medium | green (0,255,0) | (B) next contour — glue reference |
| 3 | DLF-03_score_strong | cyan | graphics (reserved for phase 2) |
| 4 | DLF-04_cut_inner | magenta (255,0,255) | (C) contour cutlines |
| 5 | DLF-05_cut_outer | red (255,0,0) | (D) board outline — run last |

`DLF-99_sheet` is the sheet boundary for orientation only — never cut it.

## Run from source

Double-click `start.bat` (Windows), or from any platform:

```
python launcher.py
```

It picks a free port starting at 8765 — so a second copy never fights the
one already running — and opens the browser for you. Drop a GeoTIFF (or click
*load demo terrain*), tune the parameters, check the **Sheets** and **Stack**
previews, then **Export DXF (ZIP)**. The ZIP contains `sheet_XX.dxf` files
plus a `cutting_report.txt` with assembly notes.

First-time setup: `python -m venv .venv` then
`.venv\Scripts\pip install -r requirements.txt`
(if a wheel is missing for your Python, use `py -3.12 -m venv .venv`).

## Build the downloads yourself

The packaged builds on the [releases page](../../releases) are produced by
GitHub Actions from `terrainslicer.spec`. Building one locally is two
commands:

```
pip install -r requirements-build.txt
pyinstaller terrainslicer.spec
```

The result lands in `dist/DL-Terrain-Slicer/`;
`DL-Terrain-Slicer --selftest` starts it, checks it answers, and exits —
that is exactly what CI runs on every build.

## Browser version

The same app also builds into a folder of static files that runs entirely in
the visitor's browser (Pyodide/WebAssembly) — no server-side Python, no
upload of anyone's terrain data. See [web/README.md](web/README.md).

All application logic lives in `app/core.py`; `app/main.py` is only the
FastAPI wrapper around it and `web/bridge.py` only the browser wrapper, so
both builds slice with exactly the same code.

## Parameters

- **Scale** and **material thickness** determine the real-world contour
  interval: `interval = thickness · scale / 1000 / vertical exaggeration`
  (2 mm at 1:500 → 1 m).
- **Number of boards (N ≥ 2)**: the offset-method trade-off. More boards →
  wider rings → more glue surface, but more material. The app reports the
  **narrowest glue strip** (distance between the green score line and the
  back cutline) so you can pick the lowest N that still glues well —
  this replaces "measure the distance between cut and score line" by hand.
- **Min curve length** drops slivers (GH: "delete if length less then 10 mm").
- **Slicing base**: contours either start at the DTM's lowest point or lie
  on absolute multiples of the interval (0 m altitude origin).
- Labels are engraved with genuine **Hershey single-stroke fonts** (public
  domain plotter fonts — the laser draws every line exactly once): Simplex,
  Roman or Script, selectable in the UI. A **control-points slider** thins
  the curved glyph parts (Douglas-Peucker) for faster engraving; straight
  strokes are untouched. Default sheet size is 1000 x 700 mm (Trotec bed).
- **Hidden labels** (default): each ring's number is engraved in the glue
  zone that the next ring covers, so the assembled model shows no numbers.
  Where the glue strip is too narrow the number falls back to the visible
  ring (noted in the cutting report); topmost rings stay unnumbered.
- The **Stack 3D** tab is a real orbit/zoom/pan viewport (vendored Three.js,
  fully offline) showing the physical rings on a ground grid. Assembly
  aids: **click a ring** to highlight it and see "contour N · board K ·
  height"; shade rings **per source board** to see the offset method;
  **explode** the stack vertically; or **step through the assembly** ring
  by ring.

## Tests

```
.venv\Scripts\python.exe -m pytest tests -q
```

Synthetic GeoTIFFs (gaussian hills with a depression and nodata border, plus
a linear ramp with exactly predictable contour counts) are generated by
`tests/make_test_dtm.py`.

## Terrain inputs

- **GeoTIFF DTM** — georeferenced (pixel scale + tiepoint read from tags)
- **OBJ mesh** — Blender-style terrain surface exports; rasterized to a
  heightfield matched to the mesh density (up axis auto-detected). Mesh
  units are assumed to be metres. Dense meshes (>250k faces) work but a
  decimated export is faster.

## Feature layers (polygon / line / point shapefiles)

Besides polygon hatch areas (below), **line shapefiles** become scored
linework with real dash/dot linetypes (solid, dashed, dotted, dash-dot,
dash-dot-dot + dash scale) and **point shapefiles** become circles with
radius, linetype, optional interior hatch and colors including magenta =
CUT (e.g. tree-dowel holes). Each file gets its own settings card; drops
in the wrong section are sorted automatically by detected geometry type.

## Hatch areas (polygon shapefiles, multiple layers)

Load any number of polygon `.shp` files (lakes, roads, grasslands, …) —
each becomes its own card with independent settings: **24 patterns** in five
groups (Linear: lines, double lines, dashes, dash-dot, crosshatch, triangle
grid, zigzag · Water: waves, ripples, fish scales · Paving: herringbone,
running bond, honeycomb, diamonds · Scatter & vegetation: dots, rings,
stipple, pebbles, plus marks, ticks, grass tufts, marsh reeds · Abstract:
interference, contour echo), plus spacing, rotation, outline toggle and
color: blue → `DLF-01_score_light`, green → `DLF-02_score_medium`,
cyan → `DLF-03_score_strong`.

Hatching lands only on the **visible ring** of every layer — never on glue
zones, so the scoring survives assembly. Scatter patterns use deterministic
jitter (re-slicing never reshuffles them) and element counts are capped so
tiny spacings cannot freeze the app. The shapefiles must share the
terrain's coordinate system: works out of the box with a GeoTIFF from the
same GIS project; with an OBJ mesh only if the mesh keeps world coordinates
(a centered Blender export will not align — the app warns instead of
guessing).

## GUI typography

Source Sans 3 (headings/UI) + Quattrocento Sans (body) — the
digital-landscapes.com relaunch brand pair, served locally from
`app/static/fonts/` (OFL licenses included).

## Phase 2 ideas

STL input, importing extra engrave linework (DXF/SVG → DLF-00),
fixed-N-boards compatibility mode, label text along curves, tighter
true-polygon nesting.

## License & disclaimer

Free software under the **GNU GPLv3** (see [LICENSE](LICENSE)) —
© Digital Landscapes, https://digital-landscapes.com/.

Provided **as is, without warranty**. Verify dimensions and the cutting
report before cutting; laser operation and material safety are the
operator's responsibility. See [DISCLAIMER.md](DISCLAIMER.md). All terrain
processing happens locally — no data is collected or transmitted.

## Credits

Built on open source, with thanks to the people who maintain it:
Python, NumPy, Shapely, ContourPy, tifffile, ezdxf, FastAPI, uvicorn,
Pyodide, three.js, the Hershey stroke fonts, and the Source Sans 3 and
Quattrocento Sans typefaces (OFL).

Developed by Digital Landscapes **with AI assistance** (Anthropic's Claude —
see the co-author trailers in the commit history). Design decisions, the
offset method itself and all verification are Digital Landscapes' own.
