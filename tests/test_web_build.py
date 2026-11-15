"""web/build.py packs the pure-python wheels into slicer.zip (build 31), so the
browser downloads one archive instead of six. A wheel without compiled code is
installed by unpacking it - these tests hold that claim to account."""
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "web"))

import build  # noqa: E402


def _wheel(path: Path, members: dict) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, text in members.items():
            zf.writestr(name, text)
    return path


def test_slicer_zip_carries_unpacked_wheels(tmp_path):
    whl = _wheel(tmp_path / "demo-1.0-py3-none-any.whl", {
        "demo/__init__.py": "X = 1\n",
        "demo-1.0.dist-info/METADATA": "Name: demo\n",
        "demo-1.0.data/data/share/man/man1/demo.1": "man page\n",
    })
    out = build.write_slicer_zip(tmp_path / "slicer.zip", [whl])
    names = zipfile.ZipFile(out).namelist()
    assert "bridge.py" in names and "slicer/contours.py" in names
    assert "demo/__init__.py" in names and "demo-1.0.dist-info/METADATA" in names
    assert not any(".data/" in n for n in names)          # the man page stays behind
    assert len(names) == len(set(names))

    bad = _wheel(tmp_path / "bad-1.0-py3-none-any.whl",
                 {"bad-1.0.data/scripts/run": "#!/bin/sh\n"})
    with pytest.raises(SystemExit, match="needs a real installer"):
        build.write_slicer_zip(tmp_path / "bad.zip", [bad])


@pytest.mark.skipif(not (ROOT / "web" / "vendor" / "wheels").exists(),
                    reason="web/vendor not fetched (as in CI)")
def test_real_wheels_import_from_the_unpacked_zip(tmp_path):
    wheels = sorted((ROOT / "web" / "vendor" / "wheels").glob("*-py3-none-any.whl"))
    zipfile.ZipFile(build.write_slicer_zip(tmp_path / "slicer.zip", wheels)).extractall(tmp_path / "x")
    code = ("import sys; sys.path.insert(0, sys.argv[1]); "
            "import bridge, ezdxf, tifffile, fontTools, pyparsing, typing_extensions; "
            "mods = [bridge, ezdxf, tifffile, fontTools, pyparsing, typing_extensions]; "
            "print('\\n'.join(m.__file__ for m in mods))")
    files = subprocess.run([sys.executable, "-c", code, str(tmp_path / "x")],
                           capture_output=True, text=True, check=True).stdout.split()
    assert len(files) == 6
    assert all(Path(f).resolve().is_relative_to((tmp_path / "x").resolve()) for f in files)
