"""The browser build's Python side, tested without a browser.

web/bridge.py is what the Pyodide worker calls. It imports nothing from
Pyodide, so the whole protocol - JSON envelopes, the slice cache, the error
mapping - runs in plain CPython here. What is left for the browser test is
only whether Pyodide itself behaves, not whether the app logic does.
"""
import json
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "web"))

import bridge  # noqa: E402
from app import core  # noqa: E402
from tests.make_test_dtm import make_hills  # noqa: E402


def call(fn, *args):
    env = json.loads(fn(*args))
    assert env["ok"], env.get("error")
    return env["result"]


def fail(fn, *args):
    env = json.loads(fn(*args))
    assert not env["ok"]
    return env


# 1500 m of test terrain at 1:5000 -> a 300 mm model that fits the sheet
PARAMS = {"scale": 5000, "thickness_mm": 2,
          "sheet_width_mm": 600, "sheet_height_mm": 400}


@pytest.fixture
def upload_id():
    """The same small test terrain the pipeline tests use, through the bridge."""
    raw = Path(make_hills()).read_bytes()
    return call(bridge.upload, "hills.tif", raw)["upload_id"]


def test_version_matches_app_build():
    assert call(bridge.version)["build"] == core.APP_BUILD


def test_demo_returns_a_usable_terrain():
    data = call(bridge.demo)
    assert data["upload_id"]
    assert data["hillshade"].startswith("data:image/png;base64,")
    assert data["summary"]["cols"] > 0


def test_upload_geotiff_bytes():
    raw = Path(make_hills()).read_bytes()
    data = call(bridge.upload, "hills.tif", raw)
    assert data["summary"]["cell_size"] == 5.0


def test_slice_payload_is_json_serialisable(upload_id):
    payload = json.dumps({"upload_id": upload_id, "params": PARAMS})
    result = call(bridge.do_slice, payload)
    assert result["stats"]["n_boards"] == 4
    assert result["placements"]
    # the worker hands this straight to JSON.parse - no numpy types allowed
    json.dumps(result)


def test_export_matches_the_desktop_pipeline(upload_id):
    payload = json.dumps({"upload_id": upload_id, "params": PARAMS})
    meta = call(bridge.do_export, payload)
    data = bridge.take_export()
    assert meta["filename"].endswith("_laser.zip")
    assert meta["size"] == len(data)
    names = zipfile.ZipFile(__import__("io").BytesIO(data)).namelist()
    assert "cutting_report.txt" in names
    assert any(n.startswith("sheet_") and n.endswith(".dxf") for n in names)
    # the bytes are handed over once, then released
    assert bridge.take_export() == b""


def test_slice_is_cached_and_invalidated(upload_id):
    payload = json.dumps({"upload_id": upload_id, "params": PARAMS})
    call(bridge.do_slice, payload)
    first = bridge._cache["value"]
    call(bridge.do_slice, payload)
    assert bridge._cache["value"] is first          # same parameters -> reused
    other = json.dumps({"upload_id": upload_id,
                        "params": {**PARAMS, "n_boards": 5}})
    call(bridge.do_slice, other)
    assert bridge._cache["value"] is not first      # changed -> re-sliced


def test_unknown_upload_is_a_clean_error():
    env = fail(bridge.do_slice, json.dumps({"upload_id": "nope", "params": PARAMS}))
    assert env["status"] == 404
    assert "re-upload" in env["error"]


def test_invalid_params_are_reported(upload_id):
    env = fail(bridge.do_slice,
               json.dumps({"upload_id": upload_id, "params": {"n_boards": 1}}))
    assert env["status"] == 422
    assert "boards" in env["error"]


def test_broken_terrain_file_is_reported():
    env = fail(bridge.upload, "broken.tif", b"not a tiff at all")
    assert env["status"] == 422
    assert "could not read terrain file" in env["error"]
