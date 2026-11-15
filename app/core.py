"""Transport-independent application logic.

Everything the app does between "bytes came in" and "a JSON-able dict goes
out" lives here. It imports no web framework and never touches the disk, so
the exact same code runs behind FastAPI (app/main.py, the desktop app) and
inside a Pyodide Web Worker (web/worker.js, the browser build). Keeping one
implementation is deliberate: the offset-method logic must never drift
between the two builds.

Errors are raised as SlicerError with an HTTP-ish status code; each
transport maps that onto its own error channel.
"""
from __future__ import annotations

import base64
import io
import uuid
from pathlib import Path

from slicer.dtm import load_asc, load_geotiff, DTM
from slicer.params import SliceParams
from slicer.contours import slice_dtm
from slicer.nesting import nest
from slicer.dxfout import export_zip
from app.png import hillshade_png

# Bump together with EXPECTED_BUILD in app/static/app.js and the ?v= asset
# tags in app/static/index.html. The frontend compares this at startup and
# tells the user to restart the server if the running process is older than
# the files on disk.
APP_BUILD = 32

MAX_UPLOADS = 8


class SlicerError(Exception):
    """Expected, user-facing failure. status mirrors the HTTP code."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class Store:
    """In-memory session state: loaded terrains and their feature layers.

    The server keeps one shared Store (a handful of users on a LAN at most);
    the browser build keeps one per worker, i.e. per visitor.
    """

    def __init__(self, max_uploads: int = MAX_UPLOADS):
        self.max_uploads = max_uploads
        self.uploads: dict[str, DTM] = {}
        self.hatches: dict[str, list] = {}

    def get_dtm(self, upload_id: str) -> DTM:
        dtm = self.uploads.get(upload_id)
        if dtm is None:
            raise SlicerError(404, "upload not found - please re-upload the DTM")
        return dtm

    def add(self, dtm: DTM) -> str:
        while len(self.uploads) >= self.max_uploads:
            oldest = next(iter(self.uploads))
            self.uploads.pop(oldest)
            self.hatches.pop(oldest, None)
        uid = uuid.uuid4().hex[:12]
        self.uploads[uid] = dtm
        return uid


def _terrain_response(store: Store, dtm: DTM, warnings: list[str]) -> dict:
    uid = store.add(dtm)
    thumb = hillshade_png(dtm.elevation, dtm.cell_size)
    return {
        "upload_id": uid,
        "name": dtm.source_name,
        "summary": dtm.summary(),
        "warnings": warnings,
        "hillshade": "data:image/png;base64," + base64.b64encode(thumb).decode(),
    }


def load_terrain(store: Store, data: bytes, filename: str) -> dict:
    name = filename or "upload"
    ext = name.lower().rsplit(".", 1)[-1]
    try:
        if ext in ("obj", "stl"):
            from slicer import meshload
            dtm, warnings = (meshload.load_obj if ext == "obj" else meshload.load_stl)(data, name=name)
        elif ext == "asc":
            dtm, warnings = load_asc(data, name=name)
        else:
            dtm, warnings = load_geotiff(io.BytesIO(data), name=name)
    except Exception as exc:
        raise SlicerError(422, f"could not read terrain file: {exc}") from exc
    return _terrain_response(store, dtm, warnings)


def load_demo(store: Store) -> dict:
    from app.demo import demo_dtm
    return _terrain_response(store, demo_dtm(), [])


def add_hatch(store: Store, upload_id: str, data: bytes, filename: str) -> dict:
    """Read a vector file (shapefile, DXF, GeoJSON, GML, GeoPackage) and store
    one layer per kind and source layer: {"layers": [...], "warnings": [...]}."""
    import shapely
    from slicer.vectors import read_vector
    dtm = store.get_dtm(upload_id)
    try:
        layers, warnings = read_vector(data, filename or "features.shp")
    except Exception as exc:
        raise SlicerError(422, f"could not read vector file: {exc}") from exc
    out = []
    for kind, geom, name in layers:
        # world CRS -> DTM-local frame (the terrain's first cell centre at 0, 0)
        if kind == "point":
            xs, ys = [p[0] for p in geom], [p[1] for p in geom]
            minx, miny, maxx, maxy = min(xs), min(ys), max(xs), max(ys)
            local = [(x - dtm.origin_x, y - dtm.origin_y) for x, y in geom]
        else:
            minx, miny, maxx, maxy = geom.bounds
            local = shapely.transform(geom, lambda a: a - [dtm.origin_x, dtm.origin_y])
        overlaps = not (maxx < dtm.origin_x or maxy < dtm.origin_y
                        or minx > dtm.origin_x + dtm.width_world or miny > dtm.origin_y + dtm.height_world)
        hid = uuid.uuid4().hex[:8]
        store.hatches.setdefault(upload_id, []).append(
            {"id": hid, "name": name, "kind": kind, "polys": local})
        out.append({"hatch_id": hid, "name": name, "kind": kind})
        if not overlaps:
            # only now can lon/lat be told from a local CAD drawing near 0, 0
            lonlat = -180 <= minx and maxx <= 180 and -90 <= miny and maxy <= 90
            warnings.append(f"'{name}' lies outside the terrain extent - " + (
                "its coordinates look like longitude/latitude; export it in the terrain's "
                "coordinate system (e.g. UTM metres)" if lonlat else
                "the file probably uses a different coordinate system than the terrain"))
    return {"layers": out, "warnings": warnings}


def remove_hatch(store: Store, upload_id: str, hatch_id: str) -> dict:
    entries = store.hatches.get(upload_id, [])
    store.hatches[upload_id] = [h for h in entries if h["id"] != hatch_id]
    return {"removed": len(entries) != len(store.hatches[upload_id])}


def run(store: Store, upload_id: str, params_dict: dict, progress=None):
    """Slice + nest. Returns (dtm, params, SliceResult, NestResult).

    `progress(done, total)`, if given, is called as the slice advances so a
    transport that can deliver mid-call messages (the browser worker) can
    drive a real countdown and progress ring. Optional: the desktop path
    passes nothing and the frontend falls back to counting up.
    """
    dtm = store.get_dtm(upload_id)
    params = SliceParams.from_dict(params_dict or {})
    errors = params.validate()
    if errors:
        raise SlicerError(422, "; ".join(errors))
    settings_by_id = {h.get("id"): h for h in params.hatch_layers if isinstance(h, dict)}
    hatches = [(stored["polys"],
                settings_by_id.get(stored["id"], {})
                | {"name": stored["name"], "kind": stored.get("kind", "polygon")})
               for stored in store.hatches.get(upload_id, [])]
    try:
        result = slice_dtm(dtm, params, hatches=hatches, progress=progress)
    except Exception as exc:
        # GEOS speaks in C++ exception names. Whatever slips past the
        # finite-ring guard in contours._filled_region, the user gets a
        # sentence they can act on instead of "CGAlgorithmsDD::orientationIndex
        # encountered NaN/Inf numbers".
        name = type(exc).__name__
        if "GEOS" in name or "GEOS" in str(exc) or "shapely" in type(exc).__module__:
            raise SlicerError(
                422,
                "the geometry engine could not build this model - the terrain "
                "is probably very rough for the chosen contour interval. Try a "
                "coarser scale or thicker material, or clip the terrain to a "
                "smaller area.") from exc
        raise
    nested = nest(result.pieces, params.sheet_width_mm, params.sheet_height_mm,
                  params.sheet_margin_mm, params.part_spacing_mm)
    return dtm, params, result, nested


def slice_payload(params, result, nested) -> dict:
    """The /api/slice response body, built from an already-run pipeline."""
    placements = [
        {
            "sheet": pl.sheet,
            "board": pl.piece.board,
            "levels": pl.piece.levels,
            "outer": pl.transform(pl.piece.outer),
            "holes": [pl.transform(h) for h in pl.piece.holes],
            "score": [pl.transform(s) for s in pl.piece.score],
            "labels": [pl.transform(s) for s in pl.piece.labels],
            "hatch": [{"color": e["color"],
                       "lines": [pl.transform(line) for line in e["lines"]]}
                      for e in pl.piece.hatch],
        }
        for pl in nested.placements
    ]
    return {
        "stats": {
            "n_levels": result.n_levels,
            "n_boards": result.n_boards,
            "n_sheets": nested.n_sheets,
            "world_interval": result.world_interval,
            "model_width": result.model_width,
            "model_height": result.model_height,
            "glue_min": result.glue_min,
            "unplaced": len(nested.unplaced),
        },
        "sheet": {"w": params.sheet_width_mm, "h": params.sheet_height_mm,
                  "margin": params.sheet_margin_mm},
        "placements": placements,
        "stack": result.stack,
        "z_levels": result.z_levels,
        "warnings": result.warnings + nested.warnings,
    }


def export_payload(dtm, params, result, nested) -> tuple[bytes, str]:
    """The export ZIP and its download filename, from an already-run pipeline."""
    if nested.n_sheets == 0:
        raise SlicerError(422, "nothing to export - no pieces were placed")
    data = export_zip(nested, params, result)
    name = (Path(dtm.source_name).stem or "terrain") + "_laser.zip"
    return data, name


def do_slice(store: Store, upload_id: str, params_dict: dict, progress=None) -> dict:
    _dtm, params, result, nested = run(store, upload_id, params_dict, progress=progress)
    return slice_payload(params, result, nested)


def do_export(store: Store, upload_id: str, params_dict: dict) -> tuple[bytes, str]:
    """Returns (zip bytes, download filename)."""
    return export_payload(*run(store, upload_id, params_dict))
