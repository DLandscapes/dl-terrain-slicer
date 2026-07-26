"""DL TerrainSlicer - local web app (FastAPI)."""
from __future__ import annotations

import base64
import io
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from slicer.dtm import load_geotiff, DTM
from slicer.params import SliceParams
from slicer.contours import slice_dtm
from slicer.nesting import nest
from slicer.dxfout import export_zip
from app.png import hillshade_png

app = FastAPI(title="DL TerrainSlicer")
STATIC = Path(__file__).resolve().parent / "static"

# Bump together with the ?v= asset version in index.html and EXPECTED_BUILD
# in app.js. The frontend compares this at startup and tells the user to
# restart the server if the running process is older than the files on disk.
APP_BUILD = 16


@app.get("/api/version")
def version():
    return {"build": APP_BUILD}

_uploads: dict[str, DTM] = {}  # in-memory session store
_hatches: dict[str, list] = {}  # upload_id -> [{id, name, polys(local coords)}]
_MAX_UPLOADS = 8


class SliceRequest(BaseModel):
    upload_id: str
    params: dict = {}


def _get_dtm(upload_id: str) -> DTM:
    dtm = _uploads.get(upload_id)
    if dtm is None:
        raise HTTPException(404, "upload not found - please re-upload the DTM")
    return dtm


def _run(req: SliceRequest):
    dtm = _get_dtm(req.upload_id)
    params = SliceParams.from_dict(req.params)
    errors = params.validate()
    if errors:
        raise HTTPException(422, "; ".join(errors))
    settings_by_id = {h.get("id"): h for h in params.hatch_layers if isinstance(h, dict)}
    hatches = [(stored["polys"],
                settings_by_id.get(stored["id"], {})
                | {"name": stored["name"], "kind": stored.get("kind", "polygon")})
               for stored in _hatches.get(req.upload_id, [])]
    result = slice_dtm(dtm, params, hatches=hatches)
    nested = nest(result.pieces, params.sheet_width_mm, params.sheet_height_mm,
                  params.sheet_margin_mm, params.part_spacing_mm)
    return dtm, params, result, nested


@app.get("/")
def index():
    # never cache the shell, so asset version bumps always take effect
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    data = await file.read()
    name = file.filename or "upload"
    try:
        if name.lower().endswith(".obj"):
            from slicer.meshload import load_obj
            dtm, warnings = load_obj(data, name=name)
        else:
            dtm, warnings = load_geotiff(io.BytesIO(data), name=name)
    except Exception as exc:
        raise HTTPException(422, f"could not read terrain file: {exc}") from exc
    while len(_uploads) >= _MAX_UPLOADS:
        _uploads.pop(next(iter(_uploads)))
    uid = uuid.uuid4().hex[:12]
    _uploads[uid] = dtm
    thumb = hillshade_png(dtm.elevation, dtm.cell_size)
    return {
        "upload_id": uid,
        "name": dtm.source_name,
        "summary": dtm.summary(),
        "warnings": warnings,
        "hillshade": "data:image/png;base64," + base64.b64encode(thumb).decode(),
    }


@app.post("/api/demo")
def demo():
    from app.demo import demo_dtm
    dtm = demo_dtm()
    while len(_uploads) >= _MAX_UPLOADS:
        _uploads.pop(next(iter(_uploads)))
    uid = uuid.uuid4().hex[:12]
    _uploads[uid] = dtm
    thumb = hillshade_png(dtm.elevation, dtm.cell_size)
    return {
        "upload_id": uid,
        "name": dtm.source_name,
        "summary": dtm.summary(),
        "warnings": [],
        "hillshade": "data:image/png;base64," + base64.b64encode(thumb).decode(),
    }


@app.post("/api/hatch")
async def hatch_upload(upload_id: str, file: UploadFile = File(...)):
    import shapely
    from slicer.hatch import read_shp
    dtm = _get_dtm(upload_id)
    data = await file.read()
    try:
        kind, geom = read_shp(data)
    except Exception as exc:
        raise HTTPException(422, f"could not read shapefile: {exc}") from exc
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
    name = Path(file.filename or "features.shp").stem
    _hatches.setdefault(upload_id, []).append(
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


@app.delete("/api/hatch/{upload_id}/{hatch_id}")
def hatch_remove(upload_id: str, hatch_id: str):
    entries = _hatches.get(upload_id, [])
    _hatches[upload_id] = [h for h in entries if h["id"] != hatch_id]
    return {"removed": len(entries) != len(_hatches[upload_id])}


@app.post("/api/slice")
def do_slice(req: SliceRequest):
    dtm, params, result, nested = _run(req)
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


@app.post("/api/export")
def do_export(req: SliceRequest):
    dtm, params, result, nested = _run(req)
    if nested.n_sheets == 0:
        raise HTTPException(422, "nothing to export - no pieces were placed")
    data = export_zip(nested, params, result, dtm.summary())
    name = (Path(dtm.source_name).stem or "terrain") + "_laser.zip"
    return Response(
        content=data, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}"'})


app.mount("/static", StaticFiles(directory=STATIC), name="static")
