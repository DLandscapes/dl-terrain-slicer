# DL Terrain Slicer

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.23510542.svg)](https://doi.org/10.5281/zenodo.23510542)

Turns a digital terrain model into laser-ready DXF files for a stacked
cardboard contour model, using the
[horizontal contour offset method](https://digital-landscapes.com/horizontal-contour-model-offset-method/).
A standalone replacement for the Grasshopper definition
`DL-Contour_offset_method_011.gh` — no Rhino, no GDAL, no GIS licence.

## Getting started

**What you need:** a terrain model — a **GeoTIFF** (`.tif`) or **ASCII grid**
(`.asc`) from QGIS, or an **OBJ** / **STL** mesh from Rhino or Blender.
Optionally vector files for areas, lines or points you want scored onto the
model: shapefiles, **DXF** (drawn in Rhino or AutoCAD), **GeoJSON**, **GML** or
**GeoPackage**. You can try everything without your own data first.

### 1. Download

In the right-hand sidebar, under **Releases**, click the newest release
(titled "DL Terrain Slicer v…") — the *name*, not just the sidebar box.
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

The menu on the left holds the work in order — **Import, Slice, Nest, Label,
Engrave, Export** — one section open at a time; a closed section shows its
settings beside its title. Drag the menu by its header; **⊟** (or Esc) folds it
away, **☰** brings it back. The readings (glue strip, sheets, warnings) sit at
the bottom right.

1. Click **load demo terrain** to see how it works before using your own file.
2. For your own model, drag a terrain file onto the drop area in **Import**.
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
one already running — and opens the browser for you. Drop a terrain (or click
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

- **GeoTIFF DTM** — georeferenced (pixel scale + tiepoint read from tags;
  PixelIsArea and PixelIsPoint both honoured)
- **ASCII grid** (`.asc`, Esri / GDAL AAIGrid) — `xllcorner` or `xllcenter`
  headers, `NODATA_value`; reads exactly like the same grid as a GeoTIFF
- **OBJ / STL mesh** — terrain surfaces from Rhino or Blender (STL binary or
  ASCII); rasterized to a heightfield matched to the mesh density (up axis
  auto-detected). Mesh units are assumed to be metres — STL has no units and
  CAD exports are often in millimetres, so the app says so. Dense meshes
  (>250k faces) work but a decimated export is faster.

## Feature layers (vector files)

Vector files feed the Engrave section: **areas** get hatching (below),
**lines** become scored linework with real dash/dot linetypes (solid,
dashed, dotted, dash-dot, dash-dot-dot + dash scale) and **points** become
circles with radius, linetype, optional interior hatch and colors including
magenta = CUT (e.g. tree-dowel holes). Each layer gets its own settings
card; drops in the wrong section are sorted automatically by geometry type.

| format | what becomes a card |
|---|---|
| shapefile (`.shp`) | the file |
| **DXF** (Rhino, AutoCAD, QGIS) | each DXF layer × kind: closed curves, circles, ellipses and hatches → area (a closed curve inside another cuts a hole, as CAD hatching does), open curves → line, POINTs → points; Z is dropped; text and blocks are left out with a note; a drawing not in metres gets a note |
| **GeoJSON** (`.geojson`, `.json`) | each kind in the file |
| **GML** 2 / 3 (`.gml`) | each kind in the file; polygon holes kept |
| **GeoPackage** (`.gpkg`) | each table × kind |

No reprojection: a layer must share the terrain's coordinate system. A
DXF drawn in Rhino over an imported OBJ/STL lands exactly where it was
drawn (measured: 0.000 m); a layer in longitude/latitude over a UTM terrain
is caught and explained.

## Hatch areas (multiple layers)

Load any number of area layers (lakes, roads, grasslands, …) —
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
tiny spacings cannot freeze the app. The layers must share the terrain's
coordinate system: works out of the box with a GeoTIFF from the same GIS
project, and with a DXF drawn over the mesh in Rhino; a GIS layer fits an
OBJ/STL mesh only if the mesh keeps world coordinates (a centered Blender
export will not align — the app warns instead of guessing).

## GUI typography

Source Sans 3 (headings/UI) + Quattrocento Sans (body) — the
digital-landscapes.com relaunch brand pair, served locally from
`app/static/fonts/` (OFL licenses included).

## Phase 2 ideas

SVG input (left out on purpose: DXF keeps real coordinates), fixed-N-boards
compatibility mode, label text along curves, tighter true-polygon nesting.

## Citing

Releases are archived on Zenodo, each with its own DOI (from v1.0.7):

- **The tool, any version:** concept DOI
  [10.5281/zenodo.23510542](https://doi.org/10.5281/zenodo.23510542) —
  always resolves to the latest release.
- **One specific release:** its version DOI, e.g. v1.0.7 =
  [10.5281/zenodo.23510543](https://doi.org/10.5281/zenodo.23510543).

GitHub's **Cite this repository** button (from `CITATION.cff`) gives a
ready-made reference for the release it describes.

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
Pyodide, three.js, the Hershey stroke fonts, the Source Sans 3 and
Quattrocento Sans typefaces (OFL), and the Feather icons in the menu
(MIT, © Cole Bemis). The menu follows DL-TerrainDiversity's.

Developed by Digital Landscapes **with AI assistance**. Design decisions,
the offset method itself and all verification are Digital Landscapes' own.
