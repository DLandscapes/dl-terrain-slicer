"""DL TerrainSlicer - local web app (FastAPI).

Thin HTTP layer only: all real work lives in app/core.py, which the
browser (Pyodide) build runs unchanged. Keep it that way - logic added
here would be missing from the web version.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import core
from app.core import APP_BUILD, SlicerError

app = FastAPI(title="DL TerrainSlicer")
STATIC = Path(__file__).resolve().parent / "static"

_store = core.Store()


class SliceRequest(BaseModel):
    upload_id: str
    params: dict = {}


def _guard(fn, *args):
    try:
        return fn(*args)
    except SlicerError as exc:
        raise HTTPException(exc.status, exc.message) from exc


@app.get("/api/version")
def version():
    return {"build": APP_BUILD}


@app.get("/")
def index():
    # never cache the shell, so asset version bumps always take effect
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    data = await file.read()
    return _guard(core.load_terrain, _store, data, file.filename or "upload")


@app.post("/api/demo")
def demo():
    return _guard(core.load_demo, _store)


@app.post("/api/hatch")
async def hatch_upload(upload_id: str, file: UploadFile = File(...)):
    data = await file.read()
    return _guard(core.add_hatch, _store, upload_id, data,
                  file.filename or "features.shp")


@app.delete("/api/hatch/{upload_id}/{hatch_id}")
def hatch_remove(upload_id: str, hatch_id: str):
    return _guard(core.remove_hatch, _store, upload_id, hatch_id)


@app.post("/api/slice")
def do_slice(req: SliceRequest):
    return _guard(core.do_slice, _store, req.upload_id, req.params)


@app.post("/api/export")
def do_export(req: SliceRequest):
    data, name = _guard(core.do_export, _store, req.upload_id, req.params)
    return Response(
        content=data, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}"'})


app.mount("/static", StaticFiles(directory=STATIC), name="static")
