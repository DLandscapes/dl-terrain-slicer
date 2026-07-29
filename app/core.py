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

from slicer.dtm import load_geotiff, DTM
from slicer.params import SliceParams
from slicer.contours import slice_dtm
from slicer.nesting import nest
from slicer.dxfout import export_zip
from app.png import hillshade_png

# Bump together with EXPECTED_BUILD in app/static/app.js and the ?v= asset
# tags in app/static/index.html. The frontend compares this at startup and
# tells the user to restart the server if the running process is older than
# the files on disk.
APP_BUILD = 19

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
    try:
        if name.lower().endswith(".obj"):
            from slicer.meshload import load_obj
            dtm, warnings = load_obj(data, name=name)
        else:
            dtm, warnings = load_geotiff(io.BytesIO(data), name=name)
    except Exception as exc:
        raise SlicerError(422, f"could not read terrain file: {exc}") from exc
    return _terrain_response(store, dtm, warnings)


def load_demo(store: Store) -> dict:
    from app.demo import demo_dtm
    return _terrain_response(store, demo_dtm(), [])


def add_hatch(store: Store, upload_id: str, data: bytes, filename: str) -> dict:
    import shapely
    from slicer.hatch import read_shp
    dtm = store.get_dtm(upload_id)
    try:
        kind, geom = read_shp(data)
    except Exception as exc:
        raise SlicerError(422, f"could not read shapefile: {exc}") from exc
    # world CRS -> DTM-local frame (x east of west edge, y north of south edge)
    if kind == "point":
        local = [(x - dtm.origin_x, y - dtm.origin_y) for x, y in geom]
        xs = [p[0] for p in local]
        ys = [p[1] for p in local]
        minx, miny, maxx, maxy = min(xs), min(ys), max(xs), max(ys)
        n_features = len(local)
    else:
        local = shapely.transform(geom, lambda a: a - [dtm.origin_x, dtm.origin_y])
        minx, miny, maxx, maxy = local.bounds
        n_features = len(local.geoms)
    overlaps = not (maxx < 0 or maxy < 0
                    or minx > dtm.width_world or miny > dtm.height_world)
    hid = uuid.uuid4().hex[:8]
    name = Path(filename or "features.shp").stem
    store.hatches.setdefault(upload_id, []).append(
        {"id": hid, "name": name, "kind": kind, "polys": local})
    return {
        "hatch_id": hid,
        "name": name,
        "kind": kind,
        "n_features": n_features,
        "overlaps_terrain": overlaps,
        "warnings": [] if overlaps else [
            f"'{name}' lies outside the terrain extent - the shapefile "
            f"probably uses a different coordinate system than the terrain file"],
    }


def remove_hatch(store: Store, upload_id: str, hatch_id: str) -> dict:
    entries = store.hatches.get(upload_id, [])
    store.hatches[upload_id] = [h for h in entries if h["id"] != hatch_id]
    return {"removed": len(entries) != len(store.hatches[upload_id])}


def run(store: Store, upload_id: str, params_dict: dict):
    """Slice + nest. Returns (dtm, params, SliceResult, NestResult)."""
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
    result = slice_dtm(dtm, params, hatches=hatches)
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
    data = export_zip(nested, params, result, dtm.summary())
    name = (Path(dtm.source_name).stem or "terrain") + "_laser.zip"
    return data, name


def do_slice(store: Store, upload_id: str, params_dict: dict) -> dict:
    _dtm, params, result, nested = run(store, upload_id, params_dict)
    return slice_payload(params, result, nested)


def do_export(store: Store, upload_id: str, params_dict: dict) -> tuple[bytes, str]:
    """Returns (zip bytes, download filename)."""
    return export_payload(*run(store, upload_id, params_dict))
