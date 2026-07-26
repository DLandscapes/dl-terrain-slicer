"""Download the pinned Pyodide subset and the pure-python wheels into web/vendor.

    python web/fetch_pyodide.py --version 0.28.3

This is the ONLY step that touches the network, and it runs on the
developer's machine, never in a visitor's browser: the finished build serves
every byte from digital-landscapes.com itself. That is deliberate - loading
a runtime from a CDN would hand the visitor's IP address to a third party
(the same reason the site self-hosts its fonts).

Downloaded:
  * the Pyodide runtime (loader, wasm, stdlib, lock file)
  * the wheels for numpy / contourpy / shapely and whatever the lock file
    says they depend on - these contain compiled code and must come from the
    matching Pyodide build
  * pure-python wheels (tifffile, ezdxf and its imports) straight from PyPI,
    pinned to the versions in requirements.txt

Nothing is deleted: existing files are kept unless --force is given.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "web" / "vendor"

# worker.js is an ES module and imports pyodide.mjs - current Pyodide
# refuses to run in a classic worker
# pyodide.mjs dynamically imports pyodide.asm.mjs, which loads the wasm
RUNTIME_FILES = ["pyodide.mjs", "pyodide.asm.mjs", "pyodide.asm.wasm",
                 "python_stdlib.zip", "pyodide-lock.json"]
# present in some releases only - fetched when they exist, ignored otherwise
OPTIONAL_FILES = ["pyodide.js", "pyodide.asm.js", "pyodide.mjs.map",
                  "pyodide.js.map", "pyodide-lock.json.map"]
NEEDED_PACKAGES = ["numpy", "contourpy", "shapely"]

# pure python, not part of the Pyodide distribution; versions follow
# requirements.txt (ezdxf pulls pyparsing / typing_extensions / fonttools)
PYPI_WHEELS = [
    "tifffile==2026.7.14",
    "ezdxf==1.4.4",
    "pyparsing==3.3.2",
    "typing_extensions==4.16.0",
    "fonttools==4.63.0",
]


def get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as r:
        return r.read()


def download(url: str, dest: Path, force: bool) -> int:
    if dest.exists() and not force:
        print(f"  keep     {dest.name} ({dest.stat().st_size / 1024 / 1024:.1f} MB)")
        return dest.stat().st_size
    data = get(url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    print(f"  fetched  {dest.name} ({len(data) / 1024 / 1024:.1f} MB)")
    return len(data)


def resolve(lock: dict, names: list[str]) -> list[str]:
    """Package names plus everything they depend on, per the lock file."""
    packages = lock["packages"]
    seen: set[str] = set()
    todo = list(names)
    while todo:
        name = todo.pop()
        key = name.lower()
        if key in seen:
            continue
        entry = packages.get(key) or packages.get(name)
        if entry is None:
            raise SystemExit(f"{name} is not in this Pyodide distribution")
        seen.add(key)
        todo.extend(entry.get("depends", []))
    return sorted(seen)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", required=True,
                    help="Pyodide version to pin, e.g. 0.28.3")
    ap.add_argument("--base", default="https://cdn.jsdelivr.net/pyodide",
                    help="download mirror (build time only)")
    ap.add_argument("--force", action="store_true", help="re-download existing files")
    ap.add_argument("--skip-wheels", action="store_true",
                    help="skip the PyPI wheels (pip is used for those)")
    args = ap.parse_args()

    base = f"{args.base}/v{args.version}/full"
    out = VENDOR / "pyodide"
    out.mkdir(parents=True, exist_ok=True)
    total = 0

    print(f"Pyodide {args.version} from {base}")
    for name in RUNTIME_FILES:
        total += download(f"{base}/{name}", out / name, args.force)
    for name in OPTIONAL_FILES:
        try:
            total += download(f"{base}/{name}", out / name, args.force)
        except Exception as exc:  # noqa: BLE001 - genuinely optional
            print(f"  skip     {name} ({exc})")

    lock = json.loads((out / "pyodide-lock.json").read_bytes())
    wanted = resolve(lock, NEEDED_PACKAGES)
    print(f"packages: {', '.join(wanted)}")
    for key in wanted:
        entry = lock["packages"][key]
        file_name = entry["file_name"]
        total += download(f"{base}/{file_name}", out / file_name, args.force)

    (out / "VERSION.txt").write_text(args.version + "\n", encoding="utf-8")

    if not args.skip_wheels:
        wheels = VENDOR / "wheels"
        wheels.mkdir(parents=True, exist_ok=True)
        print("pure-python wheels from PyPI:")
        # --platform any forces the pure-python wheel: pip would otherwise
        # pick the compiled wheel for THIS machine (fonttools ships both),
        # and a win_amd64 wheel is useless inside WebAssembly
        cmd = [sys.executable, "-m", "pip", "download", "--no-deps",
               "--only-binary", ":all:", "--platform", "any",
               "--python-version", "3.14", "--implementation", "py",
               "--abi", "none", "-d", str(wheels), *PYPI_WHEELS]
        subprocess.check_call(cmd)
        for p in sorted(wheels.glob("*.whl")):
            total += p.stat().st_size
            print(f"  {p.name}")

    print(f"\ntotal vendored: {total / 1024 / 1024:.1f} MB in {VENDOR}")
    print("next: python web/build.py --serve")
    return 0


if __name__ == "__main__":
    sys.exit(main())
