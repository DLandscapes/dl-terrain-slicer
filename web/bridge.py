"""Python side of the browser build.

worker.js calls these functions; they wrap app/core.py and hand back JSON
strings, exactly like FastAPI does in the desktop build. No new logic lives
here - if something has to change in the way the app behaves, it changes in
app/core.py so both builds get it.

Slicing is by far the expensive step, so the last (upload, params) run is
cached: the user exports the same parameters they just previewed, and
without the cache the export would slice everything a second time.
"""
from __future__ import annotations

import json
import traceback

from app import core

_store = core.Store()
_cache: dict = {"key": None, "value": None}
_export: dict = {"data": b""}


def _ok(result):
    return json.dumps({"ok": True, "result": result})


def _fail(status: int, message: str):
    return json.dumps({"ok": False, "status": status, "error": message})


def _guard(fn):
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except core.SlicerError as exc:
            return _fail(exc.status, exc.message)
        except MemoryError:
            return _fail(500, "out of memory - this terrain is too large for "
                              "the browser version, please use the desktop version")
        except Exception as exc:  # noqa: BLE001 - surfaced to the user, not swallowed
            traceback.print_exc()
            return _fail(500, f"{type(exc).__name__}: {exc}")
    return wrapper


_post_progress = None   # None = not resolved, False = unavailable, else the js module


def _slice_progress(done, total):
    """Report slice progress to the page, straight out of the worker.

    Python runs on the worker thread, so calling postMessage from inside the
    slicing loop delivers to the main thread while the slice is still going -
    which is the whole point: the page cannot show a countdown or fill a ring
    without knowing how far along the work actually is.

    Best-effort by design. If `js` is unavailable (this module is imported by
    the test suite in plain CPython) the callback is simply inert, and the
    frontend falls back to counting up.
    """
    global _post_progress
    if _post_progress is False:
        return
    try:
        if _post_progress is None:
            import js
            _post_progress = js
        js = _post_progress
        js.self.postMessage(js.Object.fromEntries(js.Array.of(
            js.Array.of("type", "progress"),
            js.Array.of("step", "slice"),
            js.Array.of("done", done),
            js.Array.of("total", total),
        )))
    except Exception:
        # Resolved once, not per tick: this fires 3x per contour level, and a
        # 400-contour model would otherwise attempt (and fail) the import 1200
        # times under CPython, where `js` does not exist at all.
        _post_progress = False


def _run(upload_id: str, params: dict, progress=None):
    key = (upload_id, json.dumps(params, sort_keys=True, default=str))
    if _cache["key"] != key:
        _cache["value"] = core.run(_store, upload_id, params, progress=progress)
        _cache["key"] = key
    return _cache["value"]


def _drop_cache():
    _cache["key"] = None
    _cache["value"] = None


@_guard
def version():
    return _ok({"build": core.APP_BUILD})


@_guard
def demo():
    _drop_cache()
    return _ok(core.load_demo(_store))


@_guard
def upload(name, data):
    _drop_cache()
    return _ok(core.load_terrain(_store, bytes(data), str(name)))


@_guard
def hatch_add(upload_id, name, data):
    _drop_cache()
    return _ok(core.add_hatch(_store, str(upload_id), bytes(data), str(name)))


@_guard
def hatch_remove(upload_id, hatch_id):
    _drop_cache()
    return _ok(core.remove_hatch(_store, str(upload_id), str(hatch_id)))


@_guard
def do_slice(payload_json):
    payload = json.loads(payload_json)
    _dtm, params, result, nested = _run(payload["upload_id"], payload.get("params") or {},
                                        progress=_slice_progress)
    return _ok(core.slice_payload(params, result, nested))


@_guard
def do_export(payload_json):
    payload = json.loads(payload_json)
    data, name = core.export_payload(*_run(payload["upload_id"], payload.get("params") or {}))
    _export["data"] = data
    return _ok({"filename": name, "size": len(data)})


def take_export() -> bytes:
    """Hand the ZIP bytes to JS and let go of them (they can be megabytes)."""
    data = _export["data"]
    _export["data"] = b""
    return data
