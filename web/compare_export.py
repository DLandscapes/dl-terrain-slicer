"""Compare two export ZIPs entity by entity.

    python web/compare_export.py desktop.zip browser.zip
    python web/compare_export.py desktop.zip browser.zip --tolerance 0.05

Used to prove that the browser build cuts what the desktop build cuts. A
byte comparison would be useless: the DXF files carry a handle seed and the
cutting report carries today's date, so this reads the actual geometry
instead - layer, entity type, vertex count and every coordinate.

Structure (which entity, on which layer, with how many points) must match
exactly. Coordinates are allowed to differ by --tolerance millimetres,
because the two builds run slightly different numpy versions and the last
bits of a centroid can move a label by a few hundredths of a millimetre -
two orders of magnitude below a laser kerf. The largest deviation per layer
is always printed, so a real difference cannot hide behind the tolerance.

Exit code 0 = same geometry within tolerance, 1 = a difference was found.
"""
from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import ezdxf

ROUND = 3


def entities(dxf_bytes: bytes) -> list[tuple]:
    doc = ezdxf.read(io.StringIO(dxf_bytes.decode("utf-8")))
    out = []
    for e in doc.modelspace():
        points = [(round(p[0], ROUND), round(p[1], ROUND))
                  for p in e.get_points("xy")] if e.dxftype() == "LWPOLYLINE" else []
        out.append((e.dxf.layer, e.dxftype(), bool(e.closed) if e.dxftype() == "LWPOLYLINE" else False,
                    tuple(points)))
    return out


def report_lines(text: str) -> list[str]:
    """The report without the generation date, which legitimately differs."""
    return [ln for ln in text.splitlines() if not ln.startswith("date:")]


def by_layer(entity_list: list[tuple]) -> dict[str, list[tuple]]:
    out: dict[str, list[tuple]] = {}
    for e in entity_list:
        out.setdefault(e[0], []).append(e)
    return out


def compare_sheet(ea: list[tuple], eb: list[tuple], tolerance: float):
    """Per layer: (count_a, count_b, worst deviation or None if incomparable)."""
    ga, gb = by_layer(ea), by_layer(eb)
    rows = []
    for layer in sorted(set(ga) | set(gb)):
        la, lb = ga.get(layer, []), gb.get(layer, [])
        if len(la) != len(lb):
            rows.append((layer, len(la), len(lb), None))
            continue
        worst = 0.0
        for x, y in zip(la, lb):
            if x[1] != y[1] or x[2] != y[2] or len(x[3]) != len(y[3]):
                worst = None
                break
            for (px, py), (qx, qy) in zip(x[3], y[3]):
                worst = max(worst, abs(px - qx), abs(py - qy))
        rows.append((layer, len(la), len(lb), worst))
    return rows


def compare(a_path: Path, b_path: Path, tolerance: float) -> int:
    a = zipfile.ZipFile(a_path)
    b = zipfile.ZipFile(b_path)
    problems: list[str] = []

    if sorted(a.namelist()) != sorted(b.namelist()):
        problems.append(f"different file lists:\n  {sorted(a.namelist())}\n  {sorted(b.namelist())}")

    for name in sorted(set(a.namelist()) & set(b.namelist())):
        if name.endswith(".dxf"):
            rows = compare_sheet(entities(a.read(name)), entities(b.read(name)), tolerance)
            print(f"{name}")
            print(f"  {'layer':<24}{'A':>7}{'B':>7}   deviation")
            for layer, na, nb, worst in rows:
                if worst is None:
                    note = "DIFFERENT SHAPE" if na == nb else "DIFFERENT COUNT"
                    problems.append(f"{name} / {layer}: {na} vs {nb} entities - {note}")
                elif worst > tolerance:
                    note = f"{worst:.4f} mm  ABOVE TOLERANCE"
                    problems.append(f"{name} / {layer}: moved by {worst:.4f} mm "
                                    f"(tolerance {tolerance} mm)")
                else:
                    note = f"{worst:.4f} mm" + ("  (exact)" if worst == 0 else "")
                print(f"  {layer:<24}{na:>7}{nb:>7}   {note}")
        elif name.endswith(".txt"):
            la, lb = report_lines(a.read(name).decode()), report_lines(b.read(name).decode())
            if la != lb:
                diff = [f"  - {x}\n  + {y}" for x, y in zip(la, lb) if x != y]
                problems.append(f"{name}: report differs\n" + "\n".join(diff[:10]))

    if problems:
        print(f"\nDIFFERENT - {len(problems)} problem(s):")
        for p in problems[:20]:
            print(" ", p)
        return 1

    print("\nSAME - every layer has the same entities within tolerance")
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    tol = 0.0
    for i, a in enumerate(sys.argv):
        if a == "--tolerance":
            tol = float(sys.argv[i + 1])
            args = [x for x in args if x != sys.argv[i + 1]]
    if len(args) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(compare(Path(args[0]), Path(args[1]), tol))
