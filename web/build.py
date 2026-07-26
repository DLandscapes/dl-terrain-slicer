"""Assemble the browser (WebAssembly) build into web/dist.

    python web/build.py            # build into web/dist
    python web/build.py --serve    # build, then serve it on port 8767

The result is a folder of plain static files: copy it onto any web server
(for digital-landscapes.com: SFTP into a versioned sub-directory) and it
runs entirely in the visitor's browser. No Python on the server, no CDN,
no upload of anyone's terrain data.

Layout produced:

    dist/
      index.html            the app shell, with window.TS_MODE = "wasm"
      static/
        app.js api.js style.css logo-dl.png fonts/ vendor/   (as shipped)
        worker.js           Pyodide host
        slicer.zip          slicer/ + app/ + bridge.py
        manifest.json       what worker.js has to load
        pyodide/            pinned runtime + numpy/shapely/contourpy wheels
        wheels/             pure-python wheels (tifffile, ezdxf, ...)

Run web/fetch_pyodide.py once to populate web/vendor - that step downloads
from the internet, this one never does.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
VENDOR = WEB / "vendor"
DIST = WEB / "dist"
MARKER = ".dl-terrainslicer-dist"   # only a folder carrying this is ever wiped

# copied verbatim from app/static into dist/static
STATIC_FILES = ["app.js", "api.js", "style.css", "logo-dl.png"]
STATIC_DIRS = ["fonts", "vendor"]

# packaged for pyodide.unpackArchive(); paths are relative to the repo root
ZIP_MEMBERS = [
    "slicer/__init__.py", "slicer/dtm.py", "slicer/params.py", "slicer/contours.py",
    "slicer/nesting.py", "slicer/dxfout.py", "slicer/hatch.py", "slicer/meshload.py",
    "slicer/stroke_font.py", "slicer/hershey_data.py",
    # app/ has no __init__.py - it is an implicit namespace package, which
    # works the same once the archive is unpacked next to sys.path
    "app/core.py", "app/demo.py", "app/png.py",
    "web/bridge.py",
]

# loaded from the pinned pyodide distribution (native code - must match it)
PYODIDE_PACKAGES = ["numpy", "contourpy", "shapely"]


def build_number() -> int:
    text = (ROOT / "app" / "core.py").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("APP_BUILD"):
            return int(line.split("=")[1].strip())
    raise SystemExit("could not read APP_BUILD from app/core.py")


def check_versions(build: int) -> None:
    """The three build markers must agree, or the app warns the user at start."""
    app_js = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    index = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    if f"EXPECTED_BUILD = {build}" not in app_js:
        raise SystemExit(f"app.js EXPECTED_BUILD does not match APP_BUILD={build}")
    if f"?v={build}" not in index:
        raise SystemExit(f"index.html ?v= tags do not match APP_BUILD={build}")


def clean_dist() -> None:
    if not DIST.exists():
        return
    if not (DIST / MARKER).exists():
        raise SystemExit(
            f"{DIST} exists but carries no {MARKER} marker - refusing to delete "
            f"a folder this script did not create. Remove or rename it yourself.")
    try:
        shutil.rmtree(DIST)
    except PermissionError as exc:
        raise SystemExit(
            f"cannot replace {DIST}: {exc.filename} is open in another process.\n"
            f"On Windows the --serve development server locks the files it is "
            f"serving - stop it (Ctrl+C) and build again.") from exc


def copy_static() -> None:
    src = ROOT / "app" / "static"
    dst = DIST / "static"
    dst.mkdir(parents=True, exist_ok=True)
    for name in STATIC_FILES:
        shutil.copy2(src / name, dst / name)
    for name in STATIC_DIRS:
        shutil.copytree(src / name, dst / name)
    shutil.copy2(WEB / "worker.js", dst / "worker.js")


def write_index(build: int) -> None:
    html = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    marker = '<!-- TS_MODE is injected here by web/build.py for the browser build -->'
    if marker not in html:
        raise SystemExit("index.html lost the TS_MODE injection marker")
    inject = (f'<script>window.TS_MODE = "wasm"; '
              f'window.TS_ASSET_V = "{build}";</script>')
    (DIST / "index.html").write_text(html.replace(marker, inject), encoding="utf-8")


def write_slicer_zip() -> None:
    out = DIST / "static" / "slicer.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in ZIP_MEMBERS:
            path = ROOT / rel
            if not path.exists():
                raise SystemExit(f"missing source file: {rel}")
            # bridge.py sits next to the packages inside the archive
            arc = "bridge.py" if rel == "web/bridge.py" else rel
            zf.write(path, arc)
    return out


def copy_runtime() -> tuple[list[str], list[str]]:
    """Copy the vendored runtime into dist. Returns (packages, wheel files)."""
    src = VENDOR / "pyodide"
    if not src.exists():
        print("! web/vendor/pyodide is missing - run web/fetch_pyodide.py first.")
        print("  Building the rest anyway; the page will not start without it.")
        return PYODIDE_PACKAGES, []
    shutil.copytree(src, DIST / "static" / "pyodide")
    wheels_src = VENDOR / "wheels"
    wheels: list[str] = []
    if wheels_src.exists():
        dst = DIST / "static" / "wheels"
        dst.mkdir(parents=True, exist_ok=True)
        for p in sorted(wheels_src.glob("*.whl")):
            # only universal wheels run in WebAssembly - a compiled wheel for
            # the build machine (pip picks those by default) would break the
            # page at load time, so leave it behind and say so
            if not p.name.endswith("-py3-none-any.whl"):
                print(f"! skipping platform-specific wheel {p.name}")
                continue
            shutil.copy2(p, dst / p.name)
            wheels.append(p.name)
    return PYODIDE_PACKAGES, wheels


def human(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB" if n >= 1024 * 1024 else f"{n / 1024:.0f} kB"


def tree_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--serve", action="store_true",
                    help="serve dist/ on http://localhost:8767 after building")
    ap.add_argument("--port", type=int, default=8767)
    args = ap.parse_args()

    build = build_number()
    check_versions(build)
    clean_dist()
    DIST.mkdir(parents=True)
    (DIST / MARKER).write_text(
        "Generated by web/build.py - safe to delete, safe to overwrite.\n",
        encoding="utf-8")

    copy_static()
    write_index(build)
    zip_path = write_slicer_zip()
    packages, wheels = copy_runtime()
    (DIST / "static" / "manifest.json").write_text(
        json.dumps({"build": build, "packages": packages, "wheels": wheels},
                   indent=2), encoding="utf-8")

    print(f"built dist for build {build}")
    print(f"  slicer.zip   {human(zip_path.stat().st_size)}")
    print(f"  pyodide      {len(packages)} packages, {len(wheels)} extra wheels")
    print(f"  total        {human(tree_size(DIST))}")
    print(f"  -> {DIST}")

    if args.serve:
        import http.server
        # .wasm must be served with the right type or the runtime will not start
        http.server.SimpleHTTPRequestHandler.extensions_map[".wasm"] = "application/wasm"

        class Handler(http.server.SimpleHTTPRequestHandler):
            # keep-alive, so the worker's downloads are not serialised behind
            # the page's own requests
            protocol_version = "HTTP/1.1"

            def __init__(self, *a, **kw):
                super().__init__(*a, directory=str(DIST), **kw)

            def end_headers(self):
                # development server only: never let the browser hold on to a
                # rebuilt file (the ?v= tag alone does not change within a build)
                self.send_header("Cache-Control", "no-store")
                super().end_headers()

            def log_message(self, fmt, *a):  # quieter console
                pass

        # threading: the page and the worker fetch at the same time
        with http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as httpd:
            print(f"serving http://localhost:{args.port}/  (Ctrl+C to stop)")
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                print("\nstopped")


if __name__ == "__main__":
    sys.exit(main())
