"""End-to-end tests: GeoTIFF -> slices -> nesting -> DXF."""
import io
import math
import sys
import zipfile
from pathlib import Path

import ezdxf
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from slicer.dtm import load_geotiff
from slicer.params import SliceParams
from slicer.contours import slice_dtm
from slicer.nesting import nest
from slicer.dxfout import export_zip, DLF_LAYERS
from tests.make_test_dtm import make_hills, make_ramp


@pytest.fixture(scope="module")
def hills():
    dtm, warnings = load_geotiff(str(make_hills()))
    return dtm


@pytest.fixture(scope="module")
def ramp():
    dtm, _ = load_geotiff(str(make_ramp()))
    return dtm


def default_params(**over):
    base = dict(scale=1000, thickness_mm=2, sheet_width_mm=600, sheet_height_mm=400)
    base.update(over)
    return SliceParams(**base)


def test_geotiff_loading(hills):
    assert hills.cell_size == 5.0
    assert hills.elevation.shape == (300, 300)
    s = hills.summary()
    assert s["nodata_fraction"] > 0          # the nodata corner survived
    assert 80.0 < s["zmin"] < 100.0          # depression dips below 100
    assert 150.0 < s["zmax"] < 160.0


def all_levels(result):
    out = set()
    for p in result.pieces:
        out |= set(p.levels)
    return out


def test_contour_count_matches_interval(ramp):
    # ramp 0..20 m, 1:1000 at 2 mm thickness -> interval 2 m -> 11 levels (0,2,...,20)
    params = default_params()
    assert params.world_interval == 2.0
    result = slice_dtm(ramp, params)
    expected = math.floor(20.0 / 2.0) + 1
    assert result.n_levels == expected
    assert all_levels(result) == set(range(expected))


def test_scale_changes_interval(ramp):
    # 1:500 at 2 mm -> 1 m interval -> 21 levels
    result = slice_dtm(ramp, default_params(scale=500))
    assert result.n_levels == 21


def test_base_plate_toggle(ramp):
    with_base = slice_dtm(ramp, default_params())
    without = slice_dtm(ramp, default_params(base_plate=False))
    assert 0 in all_levels(with_base)
    assert 0 not in all_levels(without)


def test_score_lines_present(hills):
    # every board carries green glue-reference lines (next contour of each ring)
    result = slice_dtm(hills, default_params())
    assert all(p.score for p in result.pieces)


def test_min_length_filter(hills):
    loose = slice_dtm(hills, default_params(min_length_mm=0.0, labels_enabled=False))
    strict = slice_dtm(hills, default_params(min_length_mm=500.0, labels_enabled=False))

    def n_rings(res):
        return sum(1 + len(p.holes) + len(p.score) for p in res.pieces)

    assert n_rings(strict) < n_rings(loose)
    for p in strict.pieces:
        for hole in p.holes:
            length = sum(math.dist(hole[i], hole[i + 1]) for i in range(len(hole) - 1))
            assert length >= 500.0 * 0.9


def test_labels_engraved(hills):
    result = slice_dtm(hills, default_params())
    labelled = [p for p in result.pieces if p.labels]
    assert labelled, "no piece received a label"
    for p in labelled:  # label strokes must lie within the piece bbox
        x0, y0, x1, y1 = p.bounds
        for stroke in p.labels:
            for x, y in stroke:
                assert x0 - 1 <= x <= x1 + 1 and y0 - 1 <= y <= y1 + 1


def test_label_density_slider(hills):
    # oversized labels make ring space scarce, so the density slider bites
    def n_labels(density):
        result = slice_dtm(hills, default_params(label_density=density,
                                                 label_height_mm=40))
        return sum(len(p.labels) for p in result.pieces)

    none = n_labels(0.0)
    strict = n_labels(0.1)   # only near-full-size labels
    normal = n_labels(0.5)   # long-standing default behaviour
    every = n_labels(1.0)    # force labels onto even the narrowest rings
    assert none == 0
    assert strict <= normal <= every
    assert every > 0 and every > strict


def test_hidden_labels_in_glue_zone(ramp):
    # ramp bands are analytically known: level z starts at model y = (1 - z/20) * 490.
    # With hidden labels (default) every number must lie in its ring's glue zone
    # (between contour i+1 and back cutline i+N) and never on the visible band.
    N = 3
    # scale 1500 -> interval 3 m -> levels 0,3,...,18: the top level keeps a
    # full-width band (an interval landing exactly on the 20 m maximum would
    # make the top region degenerate and force fallbacks)
    params = default_params(n_boards=N, scale=1500)
    result = slice_dtm(ramp, params)
    assert any(p.labels for p in result.pieces)
    assert not any("glue zone" in w for w in result.warnings), "unexpected fallback"
    zl = result.z_levels
    extent = 490.0 * params.world_to_model  # 49 cells * 10 m, in model mm

    def y_of(z):
        return (1.0 - z / 20.0) * extent

    tol = 6.0  # half a grid cell of contour jitter + simplify
    for p in result.pieces:
        allowed, forbidden = [], []
        for i in p.levels:
            if i + 1 >= len(zl):
                continue  # topmost ring stays unnumbered in hidden mode
            hi = y_of(zl[i + 1])
            lo = y_of(zl[i + N]) if i + N < len(zl) else 0.0
            allowed.append((lo - tol, hi + tol))
            forbidden.append((hi + tol, y_of(zl[i]) - tol))
        for stroke in p.labels:
            for x, y in stroke:
                assert any(lo <= y <= hi for lo, hi in allowed), \
                    f"label point y={y} outside every glue zone of board {p.board}"
                assert not any(lo < y < hi for lo, hi in forbidden), \
                    f"label point y={y} on a visible band of board {p.board}"


def test_label_hidden_toggle_moves_labels(hills):
    hidden = slice_dtm(hills, default_params())
    visible = slice_dtm(hills, default_params(label_hidden=False))

    def pts(res):
        return {pt for p in res.pieces for s in p.labels for pt in s}

    assert pts(hidden) and pts(visible)
    assert pts(hidden) != pts(visible)


def test_hidden_label_fallback_warns(tmp_path):
    # terrain with a steep top (narrow rings) above a gentle foot (wide rings):
    # the highest gentle ring's glue zone lies in the steep part and cannot take
    # a number - it must fall back to the visible ring and warn about it
    import numpy as np
    from tests.make_test_dtm import _write_geotiff

    size = 60
    t = np.arange(size, dtype=np.float64)
    z_line = np.where(t < 6, 20.0 - 2.0 * t, np.maximum(0.0, 8.0 - 0.25 * (t - 6)))
    z = np.tile(z_line[:, None], (1, size))
    path = tmp_path / "two_slope.tif"
    _write_geotiff(path, z, 10.0)
    dtm, _ = load_geotiff(str(path))
    result = slice_dtm(dtm, default_params(n_boards=2, label_height_mm=8,
                                           label_density=0.05))
    assert any("glue zone" in w for w in result.warnings)


def test_offset_method_boards(hills):
    # 31 levels on 4 boards: board k cuts contours k, k+4, k+8, ... in place
    result = slice_dtm(hills, default_params(n_boards=4))
    assert result.n_boards == 4
    assert len(result.pieces) == 4          # single-polygon footprint -> one piece per board
    seen = []
    for p in result.pieces:
        for lv in p.levels:
            assert lv % 4 == p.board        # correct board assignment
        seen.extend(p.levels)
    # every contour belongs to exactly one board -> each is cut exactly once
    assert sorted(seen) == sorted(set(seen))
    assert set(seen) == set(range(result.n_levels))
    # all boards share the same in-place outline (the model footprint)
    outers = {tuple(p.outer) for p in result.pieces}
    assert len(outers) == 1
    assert result.glue_min is not None and result.glue_min >= 0


def test_score_matches_next_boards_cutline(hills):
    # registration principle: the green score line on board k (contour i+1) is
    # exactly the magenta cutline of board (k+1) mod N, in the same plan position
    result = slice_dtm(hills, default_params(n_boards=4, labels_enabled=False))
    by_board = {p.board: p for p in result.pieces}
    checked = 0
    for k, p in by_board.items():
        nxt = by_board.get((k + 1) % result.n_boards)
        if nxt is None:
            continue
        cut_set = {tuple(line) for line in nxt.holes}
        for line in p.score:
            assert tuple(line) in cut_set, (
                f"score line of board {k} missing from cutlines of board {nxt.board}")
            checked += 1
    assert checked > 0


def test_base_mode_zero_aligns_contours(hills):
    # hills zmin ~94.1 m; interval 2 m at 1:1000
    lowest = slice_dtm(hills, default_params(labels_enabled=False))
    zero = slice_dtm(hills, default_params(labels_enabled=False, base_mode="zero"))
    # lowest mode: contours phase-shifted by zmin; zero mode: absolute multiples of 2
    assert abs(lowest.z_levels[1] - (hills.zmin + 2.0)) < 2e-3  # z_levels rounded to 3 decimals
    for z in zero.z_levels[1:]:
        assert abs(z / 2.0 - round(z / 2.0)) < 2e-3, f"contour {z} not aligned to 0 m origin"
    assert zero.z_levels[0] == round(hills.zmin, 3)  # base plate stays at the terrain minimum
    assert zero.z_levels[1] > hills.zmin


def test_cutlines_touch_board_outline(hills):
    # regression: trimming contours at the board edge left a ~0.5 mm gap to the red outline
    from shapely.geometry import LineString, Point
    result = slice_dtm(hills, default_params(labels_enabled=False))
    for p in result.pieces:
        outline = LineString(p.outer)
        for line in p.holes:
            if line[0] == line[-1]:
                continue  # closed ring: never touches the edge
            for end in (line[0], line[-1]):
                d = Point(end).distance(outline)
                assert d < 0.01, f"cutline end {end} is {d:.3f} mm away from the outline"


def test_stack_is_rings_not_slabs(hills):
    from shapely.geometry import Polygon
    result = slice_dtm(hills, default_params(labels_enabled=False))

    def area(entries, level):
        return sum(Polygon(e["outer"], e["holes"]).area for e in entries if e["level"] == level)

    # a low layer's physical ring is smaller than its full contour region
    # (the area N layers up is left out - the model is hollow underneath)
    full = slice_dtm(hills, default_params(labels_enabled=False, n_boards=result.n_levels + 1))
    assert area(result.stack, 1) < area(full.stack, 1) * 0.99


def test_more_boards_widen_glue_strip(hills):
    few = slice_dtm(hills, default_params(n_boards=2, labels_enabled=False))
    many = slice_dtm(hills, default_params(n_boards=8, labels_enabled=False))
    assert many.glue_min >= few.glue_min
    # material saving: fewer boards than the old one-sheet-per-layer approach
    assert len(many.pieces) < many.n_levels


def test_nesting_no_overlap_and_inside_sheet(hills):
    # 1:5000 -> 300 x 300 mm model, fits the 600 x 400 sheet
    params = default_params(scale=5000)
    result = slice_dtm(hills, params)
    nested = nest(result.pieces, params.sheet_width_mm, params.sheet_height_mm,
                  params.sheet_margin_mm, params.part_spacing_mm)
    assert nested.n_sheets >= 1
    assert not nested.unplaced
    boxes = {}
    for pl in nested.placements:
        w, h = pl.piece.size
        if pl.rotated:
            w, h = h, w
        # inside usable area
        assert pl.dx >= params.sheet_margin_mm - 1e-6
        assert pl.dy >= params.sheet_margin_mm - 1e-6
        assert pl.dx + w <= params.sheet_width_mm - params.sheet_margin_mm + 1e-6
        assert pl.dy + h <= params.sheet_height_mm - params.sheet_margin_mm + 1e-6
        # no bbox overlap on the same sheet
        for (ox, oy, ow, oh) in boxes.get(pl.sheet, []):
            assert (pl.dx + w <= ox or ox + ow <= pl.dx
                    or pl.dy + h <= oy or oy + oh <= pl.dy), "pieces overlap"
        boxes.setdefault(pl.sheet, []).append((pl.dx, pl.dy, w, h))


def test_dxf_roundtrip(hills):
    params = default_params(scale=5000)
    result = slice_dtm(hills, params)
    nested = nest(result.pieces, params.sheet_width_mm, params.sheet_height_mm,
                  params.sheet_margin_mm, params.part_spacing_mm)
    blob = export_zip(nested, params, result, hills.summary())
    zf = zipfile.ZipFile(io.BytesIO(blob))
    names = zf.namelist()
    assert "cutting_report.txt" in names
    dxf_names = [n for n in names if n.endswith(".dxf")]
    assert len(dxf_names) == nested.n_sheets

    total_outer = 0
    for name in dxf_names:
        doc = ezdxf.read(io.StringIO(zf.read(name).decode("utf-8")))
        layer_names = {ly.dxf.name for ly in doc.layers}
        for lname, aci, rgb in DLF_LAYERS:
            assert lname in layer_names
            assert doc.layers.get(lname).color == aci
        msp = doc.modelspace()
        outers = [e for e in msp if e.dxf.layer == "DLF-05_cut_outer"]
        for e in outers:
            assert e.closed, "cut outline not closed"
        total_outer += len(outers)
        assert doc.header["$INSUNITS"] == 4
    # every placed piece appears exactly once as an outer cut
    assert total_outer == len(nested.placements)


def test_stroke_fonts():
    from slicer import stroke_font
    for font in ("simplex", "roman", "script"):
        for ch in "0123456789":
            strokes = stroke_font.text_strokes(ch, 6.0, font=font)
            assert strokes, f"{font} glyph {ch} empty"
            for st in strokes:  # glyphs stay inside a sane box at height 6
                for x, y in st:
                    assert -1.0 <= x <= 8.0 and -2.5 <= y <= 7.5, (font, ch, x, y)
        assert stroke_font.text_width("42", 6.0, font) > 0
    # simplification reduces control points, straight-only glyphs unaffected
    full = sum(len(s) for s in stroke_font.text_strokes("0689", 6.0, simplify=0.0))
    least = sum(len(s) for s in stroke_font.text_strokes("0689", 6.0, simplify=1.0))
    assert least < full
    one_full = stroke_font.text_strokes("1", 6.0, simplify=0.0)
    one_least = stroke_font.text_strokes("1", 6.0, simplify=1.0)
    assert sum(len(s) for s in one_least) <= sum(len(s) for s in one_full)


def test_label_font_param(hills):
    for font in ("simplex", "script"):
        result = slice_dtm(hills, default_params(label_font=font, label_simplify=0.5))
        assert any(p.labels for p in result.pieces)


def test_obj_mesh_input():
    from slicer.meshload import load_obj
    # synthetic ramp mesh: 2 triangles, Z-up, 100 x 50 m, z 0..20
    obj = b"""
v 0 0 0
v 100 0 10
v 100 50 20
v 0 50 10
f 1 2 3
f 1 3 4
"""
    dtm, warnings = load_obj(obj, "ramp.obj")
    assert 95 <= dtm.width_world <= 105
    assert dtm.zmin < 1.0 and 18.0 <= dtm.zmax <= 20.5
    result = slice_dtm(dtm, SliceParams(scale=1000, thickness_mm=2))
    assert result.n_levels >= 9  # ~20 m relief / 2 m interval
    assert result.pieces


def test_rasterize_matches_per_triangle_reference():
    """The vectorised rasteriser (build 22) must reproduce the original
    per-triangle loop bit for bit - same barycentric arithmetic, same window
    rounding, same NaN-means-empty semantics. A random rugged mesh exercises
    mixed window shapes, shared edges (duplicate cells across faces) and
    degenerate triangles."""
    import math

    import numpy as np

    from slicer.meshload import rasterize

    rng = np.random.default_rng(7)
    n = 24
    gx, gy = np.meshgrid(np.linspace(0, 90, n), np.linspace(0, 60, n))
    verts = np.column_stack([
        (gx + rng.uniform(-1.4, 1.4, gx.shape)).ravel(),
        (gy + rng.uniform(-1.4, 1.4, gy.shape)).ravel(),
        rng.uniform(0, 25, gx.size),
    ])
    faces = []
    for r in range(n - 1):
        for c in range(n - 1):
            a = r * n + c
            faces.append((a, a + 1, a + n))
            faces.append((a + 1, a + n + 1, a + n))
    faces.append((0, 1, 1))  # degenerate: must be skipped, not crash
    faces = np.asarray(faces, dtype=np.int32)
    px, py, pz = verts[:, 0], verts[:, 1], verts[:, 2]
    cell = 1.7

    def reference(px, py, pz, faces, cell):
        x0, y0 = float(px.min()), float(py.min())
        nx = max(2, int(math.ceil((px.max() - x0) / cell)) + 1)
        ny = max(2, int(math.ceil((py.max() - y0) / cell)) + 1)
        grid = np.full((ny, nx), np.nan, dtype=np.float32)
        fx, fy, fz = px[faces], py[faces], pz[faces]
        for i in range(len(faces)):
            xs, ys, zs = fx[i], fy[i], fz[i]
            det = (ys[1] - ys[2]) * (xs[0] - xs[2]) + (xs[2] - xs[1]) * (ys[0] - ys[2])
            if abs(det) < 1e-9:
                continue
            i0 = int((xs.min() - x0) / cell)
            i1 = int((xs.max() - x0) / cell) + 2
            j0 = int((ys.min() - y0) / cell)
            j1 = int((ys.max() - y0) / cell) + 2
            ax = x0 + np.arange(i0, min(i1, nx)) * cell
            ay = y0 + np.arange(j0, min(j1, ny)) * cell
            if not len(ax) or not len(ay):
                continue
            GX, GY = np.meshgrid(ax, ay)
            w0 = ((ys[1] - ys[2]) * (GX - xs[2]) + (xs[2] - xs[1]) * (GY - ys[2])) / det
            w1 = ((ys[2] - ys[0]) * (GX - xs[2]) + (xs[0] - xs[2]) * (GY - ys[2])) / det
            w2 = 1.0 - w0 - w1
            mask = (w0 >= -1e-6) & (w1 >= -1e-6) & (w2 >= -1e-6)
            if not mask.any():
                continue
            zval = w0 * zs[0] + w1 * zs[1] + w2 * zs[2]
            win = grid[j0:min(j1, ny), i0:min(i1, nx)]
            win[...] = np.where(mask, np.fmax(win, zval.astype(np.float32)), win)
        return grid[::-1]

    got, _, _ = rasterize(px, py, pz, faces, cell)
    want = reference(px, py, pz, faces, cell)
    assert np.array_equal(got, want, equal_nan=True)


def test_filled_region_drops_non_finite_rings():
    """A ring carrying a NaN/Inf vertex must never reach GEOS.

    contourpy can emit a degenerate ring with a non-finite vertex on rough
    terrain; GEOS then raises
    `CGAlgorithmsDD::orientationIndex encountered NaN/Inf numbers` and the
    whole slice dies with a message that means nothing to a user. Observed in
    the WebAssembly build (numpy 2.4.3) on terrain the desktop build handled.
    A stub generator stands in for contourpy so the case is deterministic."""
    import numpy as np

    from slicer.contours import _filled_region

    square = np.array([[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0], [0.0, 0.0]])
    poisoned = np.array([[20.0, 20.0], [30.0, np.nan], [30.0, 30.0],
                         [20.0, 30.0], [20.0, 20.0]])

    class StubGen:
        """Mimics contourpy's OuterOffset filled() output."""
        def __init__(self, rings):
            self._rings = rings

        def filled(self, lower, upper):
            pts = [np.concatenate(r) for r in self._rings]
            offs = []
            for r in self._rings:
                o, acc = [0], 0
                for part in r:
                    acc += len(part)
                    o.append(acc)
                offs.append(np.array(o))
            return pts, offs

    # the poisoned ring is dropped, the good one survives
    dropped = []
    out = _filled_region(StubGen([[square], [poisoned]]), 0.0, 1.0, dropped)
    assert len(dropped) == 1
    assert not out.is_empty
    assert abs(out.area - 100.0) < 1e-6      # only the clean 10x10 square

    # and the whole thing is finite - nothing NaN leaked into the result
    for geom in out.geoms:
        assert np.isfinite(np.asarray(geom.exterior.coords)).all()

    # a poisoned OUTER ring takes its (valid) holes with it rather than
    # producing a polygon with the wrong shape
    dropped2 = []
    hole = np.array([[22.0, 22.0], [24.0, 22.0], [24.0, 24.0], [22.0, 24.0], [22.0, 22.0]])
    out2 = _filled_region(StubGen([[poisoned, hole]]), 0.0, 1.0, dropped2)
    assert out2.is_empty
    assert dropped2


def test_lzw_geotiff_without_imagecodecs():
    """QGIS and GDAL write LZW-compressed GeoTIFFs by default, float DEMs
    usually with the floating-point predictor (tag 317 = 3). Pyodide has no
    imagecodecs, so slicer.tiffcodecs must decode both. The two fixtures were
    written by tifffile WITH the real imagecodecs (ground truth, round-trip
    verified at generation time); here they must decode identically through
    whatever codec path is active - the real package if installed, the
    pure-Python fallback otherwise. A first real user hit exactly this on
    launch day (2026-08-05)."""
    import base64
    import io

    import numpy as np
    import tifffile

    from slicer import tiffcodecs

    tiffcodecs.install()

    ARR = np.frombuffer(base64.b64decode(
        'RmWdQpPGd0Ib3KVCn7yVQuHVMkLvj7FCLR2cQj+bnkJrnzlCwxN6QtwoakIvraxC8mKQQrNGokLPrnhCn3JNQl91h0JywyxCXMOiQpwqj0IMz5tCAADAf98RsULnT6lCntadQoDtRkIeWH1CwcIoQp/bPkIOTpRC6XmaQkDAsEJCKmFCiRdqQkLpfULv5EVC+/s5Qhgkf0LCYU1CPfuSQi5ud0KQRKNCyQaWQix5XkLaOaNC+HmgQuV+bUJmqllC4z+UQlTzO0JN+0dC83ghQkSxnkIvfJJCPISWQqoSnkJ9yHtCxt+IQpv1O0Lx5zZCHteSQiA4fkILhohC'
    ), dtype=np.float32).reshape(7, 9)
    LZW_NOPRED = 'SUkqAAgAAAAQAAABBAABAAAACQAAAAEBBAABAAAABwAAAAIBAwABAAAAIAAAAAMBAwABAAAABQAAAAYBAwABAAAAAQAAAA4BAgASAAAAzgAAABEBBAABAAAAMAEAABUBAwABAAAAAQAAABYBBAABAAAABwAAABcBBAABAAAABwEAABoBBQABAAAA8AAAABsBBQABAAAA+AAAACgBAwABAAAAAQAAADEBAgAMAAAAAAEAAFMBAwABAAAAAwAAAA6DDAADAAAADAEAAAAAAAB7InNoYXBlIjogWzcsIDldfQAAAAAAAAAAAAAAAAAAAAAAAQAAAAEAAAABAAAAAQAAAHRpZmZmaWxlLnB5AAAAAAAAAABAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAAAAAAAAIARjKnSEk2MdyEG24pSEn14lSE4WqMiE70esSELQ6nCEP02niEa0+OSEwwmeiE3BQaiEL1arCE8jEkCEsyMoiEz1ceIaciaQi+dUOQjkwxYQi4w5wnBUjyEDGemyEAAAwD+3wjGHOT1SQk81oIgHaRiEHiwfSEwWEKIa2x8QgcTkoQnSeU0QiAwFgQiEKjCQkSF5ZdLQ73IRSE+33JAwJD+QmEYZ+PX2kiELjdCEgRFGQmSBksQhYeS8Qm0Oc6+DyoCE5T8bSEZlUWSE4x/cyo8x2Qia+yOQnmeBCQiIsZALz5lh4hNCqglID6yD2QmM30QQk29d28XONrK18sIBwfiEC0N14CA'
    LZW_FLOATPRED = 'SUkqAAgAAAARAAABBAABAAAACQAAAAEBBAABAAAABwAAAAIBAwABAAAAIAAAAAMBAwABAAAABQAAAAYBAwABAAAAAQAAAA4BAgASAAAA2gAAABEBBAABAAAAMAEAABUBAwABAAAAAQAAABYBBAABAAAABwAAABcBBAABAAAA7wAAABoBBQABAAAA/AAAABsBBQABAAAABAEAACgBAwABAAAAAQAAADEBAgAMAAAADAEAAD0BAwABAAAAAwAAAFMBAwABAAAAAwAAAA6DDAADAAAAGAEAAAAAAAB7InNoYXBlIjogWzcsIDldfQAAAAAAAAAAAAAAAAAAAAAAAQAAAAEAAAABAAAAAQAAAHRpZmZmaWxlLnB5AAAAAAAAAABAAAAAAAAAAEAAAAAAAAAAAIAQgBA4JAy22hc8E6f3WAk2LDCFnAGV0jj8BFOTUQhCEDh8EhZAoLBBw8CE5Ak1mqOlK5wqhVq5DQxAGTgAGSmw2CHGgwAnIh6w4KYHaDBK8Xw9FSNyMZ1KMQiPkOFzWBCAcHo3wQt3EnpFIwA5gsVgMFliCQmyD6GTmKyOana0n23W8b22VwER1yra/I3uRmcRXKLHmyCKWBSPU0c2swjmwGEHVUe3iYhyY1dfYKXma7B2pwY2j69HOBRYlVoCD6OWWfHagT6cXypii681BCoCG6DVm+y47AU/EctguFnk8DCTlsbmmSWqVhaAnXAQ'

    for name, b64 in (("nopred", LZW_NOPRED), ("floatpred", LZW_FLOATPRED)):
        with tifffile.TiffFile(io.BytesIO(base64.b64decode(b64))) as t:
            got = t.pages[0].asarray()
        assert np.array_equal(got, ARR, equal_nan=True), f"LZW {name} mismatch"

    # and the codecs stand alone: a raw LZW round trip against a known vector
    raw = tiffcodecs.lzw_decode(
        base64.b64decode(b'gBIMpsNhvEECgkGhEFEMBA=='))  # imagecodecs.lzw_encode ground truth
    assert raw == b"Hello Hello Hello!"


def _mini_shp(rings) -> bytes:
    """Build a minimal one-record polygon .shp (outer rings CW)."""
    import struct
    parts, points = [], []
    for ring in rings:
        parts.append(len(points))
        points.extend(ring)
    rec = struct.pack("<i", 5)
    xs = [p[0] for p in points]; ys = [p[1] for p in points]
    rec += struct.pack("<4d", min(xs), min(ys), max(xs), max(ys))
    rec += struct.pack("<ii", len(parts), len(points))
    rec += struct.pack(f"<{len(parts)}i", *parts)
    for x, y in points:
        rec += struct.pack("<2d", x, y)
    header = struct.pack(">i", 9994) + b"\0" * 20 + struct.pack(">i", (100 + 8 + len(rec)) // 2)
    header += struct.pack("<ii", 1000, 5) + struct.pack("<8d", min(xs), min(ys), max(xs), max(ys), 0, 0, 0, 0)
    return header + struct.pack(">ii", 1, len(rec) // 2) + rec


def test_hatch_shapefile_and_generation():
    from slicer.hatch import read_shp_polygons, generate_hatch
    square_cw = [(0, 0), (0, 100), (100, 100), (100, 0), (0, 0)]  # CW = outer
    polys = read_shp_polygons(_mini_shp([square_cw]))
    assert abs(polys.area - 10000) < 1e-6
    lines = generate_hatch(polys, "lines", 10.0, 0.0)
    assert 8 <= len(lines) <= 12
    cross = generate_hatch(polys, "cross", 10.0, 45.0)
    assert len(cross) > len(lines) * 1.5
    dots = generate_hatch(polys, "dots", 10.0, 0.0)
    assert len(dots) >= 50
    for stroke in lines + dots:
        for x, y in stroke:
            assert -1 <= x <= 101 and -1 <= y <= 101


def hatch_lines_total(result):
    return sum(len(e["lines"]) for p in result.pieces for e in p.hatch)


def test_hatch_only_on_visible_rings(hills):
    from shapely.geometry import box
    # hatch square over the middle of the terrain (local world coords, m)
    hatch = box(300, 300, 1200, 1200)
    params = default_params(labels_enabled=False)
    with_h = slice_dtm(hills, params,
                       hatches=[(hatch, {"pattern": "lines", "spacing_mm": 5})])
    without = slice_dtm(hills, params)
    assert hatch_lines_total(with_h) > 0
    assert hatch_lines_total(without) == 0
    # hatched on several boards (visible rings of many levels intersect the square)
    assert sum(1 for p in with_h.pieces if p.hatch) >= 3


def test_all_patterns_produce_strokes():
    from shapely.geometry import Polygon
    from slicer.hatch import generate_hatch, HATCH_PATTERNS
    area = Polygon([(0, 0), (60, 0), (60, 40), (0, 40)])
    assert len(HATCH_PATTERNS) == 24
    for key in HATCH_PATTERNS:
        strokes = generate_hatch(area, key, 3.0, 30.0)
        assert strokes, f"pattern {key} produced nothing"
        for stroke in strokes:  # rotation must keep strokes near the area
            for x, y in stroke:
                assert -5 <= x <= 65 and -5 <= y <= 45, (key, x, y)


def test_multiple_hatch_layers_in_dxf(hills):
    from shapely.geometry import box
    params = default_params(scale=5000, labels_enabled=False)
    hatches = [
        (box(300, 300, 900, 900), {"pattern": "waves", "spacing_mm": 2, "color": "cyan"}),
        (box(700, 700, 1300, 1300), {"pattern": "grass", "spacing_mm": 3, "color": "blue"}),
    ]
    result = slice_dtm(hills, params, hatches=hatches)
    colors = {e["color"] for p in result.pieces for e in p.hatch}
    assert colors == {"cyan", "blue"}
    nested = nest(result.pieces, params.sheet_width_mm, params.sheet_height_mm,
                  params.sheet_margin_mm, params.part_spacing_mm)
    blob = export_zip(nested, params, result, hills.summary())
    zf = zipfile.ZipFile(io.BytesIO(blob))
    n = {}
    for name in zf.namelist():
        if name.endswith(".dxf"):
            doc = ezdxf.read(io.StringIO(zf.read(name).decode()))
            for e in doc.modelspace():
                n[e.dxf.layer] = n.get(e.dxf.layer, 0) + 1
    assert n.get("DLF-03_score_strong", 0) > 0   # cyan waves
    assert n.get("DLF-01_score_light", 0) > 0    # blue grass (labels disabled)


def _mini_shp_typed(shp_type: int, records: list[bytes]) -> bytes:
    import struct
    body = b""
    for i, rec in enumerate(records):
        body += struct.pack(">ii", i + 1, len(rec) // 2) + rec
    header = struct.pack(">i", 9994) + b"\0" * 20 + struct.pack(">i", (100 + len(body)) // 2)
    header += struct.pack("<ii", 1000, shp_type) + struct.pack("<8d", 0, 0, 100, 100, 0, 0, 0, 0)
    return header + body


def test_shp_line_and_point_readers():
    import struct
    from slicer.hatch import read_shp
    # polyline: one 2-part record
    pts = [(0, 0), (50, 10), (100, 0)], [(0, 20), (100, 30)]
    all_pts = pts[0] + pts[1]
    rec = struct.pack("<i", 3) + struct.pack("<4d", 0, 0, 100, 30)
    rec += struct.pack("<ii", 2, len(all_pts)) + struct.pack("<2i", 0, 3)
    for x, y in all_pts:
        rec += struct.pack("<2d", x, y)
    kind, geom = read_shp(_mini_shp_typed(3, [rec]))
    assert kind == "line" and len(geom.geoms) == 2
    assert abs(geom.geoms[1].length - 100.5) < 0.1
    # points: two single-point records
    recs = [struct.pack("<i", 1) + struct.pack("<2d", x, y) for x, y in [(10, 10), (90, 40)]]
    kind, geom = read_shp(_mini_shp_typed(1, recs))
    assert kind == "point" and geom == [(10.0, 10.0), (90.0, 40.0)]


def test_linetypes_make_real_segments():
    from shapely.geometry import LineString
    from slicer.hatch import apply_linetype, LINETYPES
    line = LineString([(0, 0), (100, 0)])
    solid = apply_linetype(line, "solid")
    assert len(solid) == 1 and len(solid[0]) == 2
    for lt in ("dashed", "dotted", "dashdot", "dashdotdot"):
        segs = apply_linetype(line, lt, 1.0)
        assert len(segs) > 10, lt
        total = sum(math.dist(s[0], s[-1]) for s in segs)
        assert total < 100.0  # gaps exist
    # scale doubles dash length -> fewer segments
    assert len(apply_linetype(line, "dashed", 2.0)) < len(apply_linetype(line, "dashed", 1.0))


def test_line_and_point_layers_in_slice(hills):
    from shapely.geometry import LineString, MultiLineString
    road = MultiLineString([LineString([(100, 100), (1400, 1400)])])
    trees = [(400, 400), (750, 750), (1100, 1100)]
    params = default_params(labels_enabled=False)
    result = slice_dtm(hills, params, hatches=[
        (road, {"kind": "line", "linetype": "dashed", "linetype_scale": 1, "color": "blue"}),
        (trees, {"kind": "point", "radius_mm": 3, "linetype": "dotted", "color": "cyan"}),
    ])
    colors = {e["color"] for p in result.pieces for e in p.hatch}
    assert colors == {"blue", "cyan"}
    n_blue = sum(len(e["lines"]) for p in result.pieces for e in p.hatch if e["color"] == "blue")
    n_cyan = sum(len(e["lines"]) for p in result.pieces for e in p.hatch if e["color"] == "cyan")
    assert n_blue > 10   # dashed road split across visible rings
    assert n_cyan > 10   # dotted circles
    # every stroke stays within the model footprint
    for p in result.pieces:
        for e in p.hatch:
            for line in e["lines"]:
                for x, y in line:
                    assert -1 <= x <= result.model_width + 1
                    assert -1 <= y <= result.model_height + 1


def test_report_mentions_interval(hills):
    params = default_params()
    result = slice_dtm(hills, params)
    nested = nest(result.pieces, params.sheet_width_mm, params.sheet_height_mm,
                  params.sheet_margin_mm, params.part_spacing_mm)
    blob = export_zip(nested, params, result, hills.summary())
    report = zipfile.ZipFile(io.BytesIO(blob)).read("cutting_report.txt").decode()
    assert "real-world contour interval: 2" in report
    assert "DLF-05_cut_outer" in report


def test_simplify_failure_does_not_kill_the_slice():
    """GEOS refusing to simplify must cost the simplification, not the model.

    The real failure: a DEM-of-difference where ~90% of cells sit at zero, so
    the contour level nearest zero traced every speckle of noise - 27,357
    vertices on ONE level of fourteen. `shapely.simplify(preserve_topology=True)`
    raised `CGAlgorithmsDD::orientationIndex encountered NaN/Inf numbers` from
    inside its double-double predicates even though the geometry was VALID and
    every coordinate finite, and that one level aborted the whole slice. WASM
    only; the desktop GEOS simplifies the identical geometry without complaint.

    Simplification only shortens the laser path, so the guard degrades instead
    of failing. Both GEOS entry points are stubbed to raise so the test is
    deterministic and does not depend on which build of GEOS is installed.
    """
    import shapely
    from shapely.geometry import Polygon

    from slicer import contours as C

    geom = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
    calls = []

    def boom(g, tol, preserve_topology=True):
        calls.append(preserve_topology)
        raise shapely.errors.GEOSException(
            "IllegalArgumentException: CGAlgorithmsDD::orientationIndex "
            "encountered NaN/Inf numbers")

    real = shapely.simplify
    shapely.simplify = boom
    try:
        stubborn = []
        out = C._simplify_safe(geom, 0.2, stubborn)
    finally:
        shapely.simplify = real

    # it tried topology-preserving first, then plain Douglas-Peucker
    assert calls == [True, False]
    # and having been refused twice it kept the geometry rather than dying
    assert out.equals(geom)
    assert len(stubborn) == 1


def test_simplify_safe_still_simplifies_normally():
    """The guard must not disable simplification on healthy geometry."""
    from shapely.geometry import Polygon

    from slicer import contours as C

    # a square with many near-collinear points along one edge
    pts = [(x / 10.0, 0.0) for x in range(101)] + [(10.0, 10.0), (0.0, 10.0)]
    geom = Polygon(pts)
    stubborn = []
    out = C._simplify_safe(geom, 0.5, stubborn)

    assert not stubborn                      # nothing stubborn about it
    assert len(out.exterior.coords) < len(pts)   # it really did simplify
    assert out.is_valid


def test_geotiff_read_falls_back_when_threads_are_unavailable():
    """A platform without threads must still read the raster.

    Pyodide has no working pthreads, so `Thread.start()` raises
    `RuntimeError: can't start new thread`. tifffile decodes segments in a
    ThreadPoolExecutor whenever `TiffPage.maxworkers` >= 2. It clamps that to
    `TIFF.MAXWORKERS` (1 in the browser) for every COMPRESSED path, but the
    uncompressed multi-tile branch ends in a hardcoded `return 2` - so an
    uncompressed tiled GeoTIFF, the plain GDAL/QGIS export, aborted the upload
    with "could not read terrain file: can't start new thread".
    """
    from slicer.dtm import _read_page

    class Page:
        def __init__(self):
            self.calls = []

        def asarray(self, maxworkers=None):
            self.calls.append(maxworkers)
            if maxworkers is None:
                raise RuntimeError("can't start new thread")
            return "decoded"

    p = Page()
    assert _read_page(p) == "decoded"
    # tried the fast path first, then retried single-threaded
    assert p.calls == [None, 1]


def test_geotiff_read_does_not_swallow_other_errors():
    """Only the thread failure is retried - anything else must propagate."""
    import pytest

    from slicer.dtm import _read_page

    class Page:
        def asarray(self, maxworkers=None):
            raise RuntimeError("file is truncated")

    with pytest.raises(RuntimeError, match="truncated"):
        _read_page(Page())


def test_selftest_exits_without_unwinding_the_interpreter():
    """A passed self-test must not be undone by a teardown crash.

    CI recorded `Segmentation fault: 11` (exit 139) from the frozen macOS
    arm64 build AFTER `SELFTEST OK` had printed: the app started and served
    correctly, then died unloading GEOS/numpy inside the PyInstaller bundle.
    The release job still ran and published v1.0.3 with only TWO of the three
    platform assets - the Apple Silicon download was simply missing, and
    nothing failed loudly enough to notice.

    The self-test's contract is "the packaged app starts and serves". Once it
    has answered that, it leaves via os._exit so the exit status is the verdict
    it computed, not whatever the runtime does on the way out. This test guards
    that: if the os._exit is ever turned back into a plain `return 0`, the
    flake comes back and it costs a release asset again.
    """
    import urllib.request

    import launcher
    from app.core import APP_BUILD

    class Resp:
        def read(self):
            return b'<div id="dropzone"></div>'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    calls = {}

    def fake_exit(code):
        calls["code"] = code
        raise SystemExit(code)  # stand in for the real _exit so pytest survives

    orig = (launcher.serve, launcher.wait_until_up,
            urllib.request.urlopen, launcher.os._exit)
    launcher.serve = lambda port: None
    launcher.wait_until_up = lambda url: {"build": APP_BUILD}
    urllib.request.urlopen = lambda *a, **kw: Resp()
    launcher.os._exit = fake_exit
    try:
        raised = False
        try:
            launcher.selftest(8799)
        except SystemExit:
            raised = True
    finally:
        (launcher.serve, launcher.wait_until_up,
         urllib.request.urlopen, launcher.os._exit) = orig

    assert raised, "selftest returned normally instead of leaving via os._exit"
    assert calls.get("code") == 0


def test_slice_reports_monotonic_progress_that_reaches_the_total(hills):
    """The countdown and the ring are only honest if this contract holds.

    The frontend derives "about 30 seconds left" from elapsed time against the
    fraction reported done, so the count must never go backwards (the estimate
    would jump around) and must actually arrive at the total (the ring must not
    stop at 90% on a finished slice). Nothing here checks timing - only that the
    signal is well-formed.
    """
    seen = []
    result = slice_dtm(hills, default_params(),
                       progress=lambda done, total: seen.append((done, total)))

    assert seen, "no progress was reported at all"
    totals = {t for _, t in seen}
    assert len(totals) == 1, f"the total changed mid-run: {totals}"
    total = totals.pop()
    dones = [d for d, _ in seen]
    assert dones == sorted(dones), "progress went backwards"
    assert dones[-1] == total, f"finished at {dones[-1]} of {total}"
    assert result.n_levels > 0


def test_slice_without_progress_callback_still_works(hills):
    """progress is optional - the desktop transport passes nothing."""
    r = slice_dtm(hills, default_params())
    assert r.n_levels > 0
