# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build of the desktop download.

    pyinstaller terrainslicer.spec

Produces a one-FOLDER build in dist/DL-Terrain-Slicer. One folder rather than
one file: it starts noticeably faster and nothing in the app ever reads from
its own bundle at runtime, so there is no reason to pay the unpack cost on
every launch.

Verify a build with:  dist/DL-Terrain-Slicer/DL-Terrain-Slicer --selftest
"""
from PyInstaller.utils.hooks import collect_all, collect_submodules

NAME = "DL-Terrain-Slicer"

# The frontend is the only thing read from disk at runtime (app/main.py's
# resource_path). Keep the layout identical to the source tree so the same
# path works frozen and unfrozen.
datas = [("app/static", "app/static")]
binaries = []
hiddenimports = []

# shapely carries the GEOS libraries next to its extension modules; missing
# them is the classic frozen-build failure and it only shows up at RUNTIME,
# which is what --selftest exists to catch.
for package in ("shapely", "contourpy"):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

# these resolve imports lazily, so static analysis alone misses parts of them
for package in ("tifffile", "ezdxf", "uvicorn"):
    hiddenimports += collect_submodules(package)

# imported inside functions in app/core.py, invisible to the dependency graph
hiddenimports += ["app.core", "app.demo", "app.png",
                  "slicer.meshload", "slicer.hatch", "slicer.hershey_data"]

a = Analysis(
    ["launcher.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # nothing in the app plots, tests itself or opens a GUI toolkit
    excludes=["matplotlib", "tkinter", "PySide6", "PyQt5", "PIL", "pytest",
              "IPython", "notebook"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX-packed binaries trip antivirus heuristics
    console=True,       # the console window IS the stop button, as in start.bat
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=NAME,
)
