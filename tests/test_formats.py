"""Build 32 import formats: .asc and .stl terrain; DXF, GeoJSON, GML and GeoPackage
vectors - and that every one of them lands where a shapefile lands."""
import io
import sqlite3
import struct
import sys
from pathlib import Path

import ezdxf
import numpy as np
import pytest
import shapely
from shapely.geometry import LineString, Point, Polygon

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import core  # noqa: E402
from slicer import meshload  # noqa: E402
from slicer.dtm import load_asc, load_geotiff  # noqa: E402
from slicer.vectors import read_vector  # noqa: E402
from tests.make_test_dtm import make_hills  # noqa: E402

X0, Y0 = 500100.0, 6599000.0            # inside the hills terrain, far from lon/lat ranges
SQUARE = Polygon([(X0, Y0), (X0 + 100, Y0), (X0 + 100, Y0 + 100), (X0, Y0 + 100)],
                 [[(X0 + 40, Y0 + 40), (X0 + 60, Y0 + 40), (X0 + 60, Y0 + 60), (X0 + 40, Y0 + 60)]])
PATH = LineString([(X0, Y0 + 200), (X0 + 100, Y0 + 200)])
TREE = Point(X0 + 30, Y0 + 30)


@pytest.fixture(scope="module")
def hills(tmp_path_factory):
    return make_hills(tmp_path_factory.mktemp("d") / "hills.tif")


# ------------------------------------------------------------------ terrain

def test_geotiff_origin_is_the_first_cell_centre(hills):
    dtm, _ = load_geotiff(str(hills))
    # tiepoint (0, 0) -> (500000, 6600000) names the OUTER corner (PixelIsArea)
    assert (dtm.origin_x, dtm.origin_y) == (500002.5, 6600000.0 - 2.5 - 299 * 5.0)


def test_asc_matches_the_geotiff(hills):
    dtm, _ = load_geotiff(str(hills))
    z = np.where(np.isfinite(dtm.elevation), dtm.elevation, -9999.0)
    body = "\n".join(" ".join(repr(float(v)) for v in row) for row in z)
    for head in (f"ncols 300\nnrows 300\nxllcorner 500000\nyllcorner {6600000 - 1500}\n",
                 f"NCOLS 300\nNROWS 300\nXLLCENTER 500002.5\nYLLCENTER {6600000 - 1500 + 2.5}\n"):
        asc, _ = load_asc(f"{head}cellsize 5\nNODATA_value -9999\n{body}\n".encode(), "h.asc")
        assert np.array_equal(asc.elevation, dtm.elevation, equal_nan=True)
        assert (asc.cell_size, asc.origin_x, asc.origin_y) == (dtm.cell_size, dtm.origin_x, dtm.origin_y)
    with pytest.raises(ValueError, match="holds"):
        load_asc(b"ncols 3\nnrows 3\nxllcorner 0\nyllcorner 0\ncellsize 1\n1 2 3\n", "bad.asc")


def test_stl_binary_and_ascii_give_the_obj_terrain():
    y, x = np.mgrid[0:20, 0:30].astype(float)
    verts = np.c_[x.ravel() * 2.0, y.ravel() * 2.0, 0.5 * y.ravel() + 0.1 * x.ravel()]
    idx = np.arange(600).reshape(20, 30)
    quads = np.c_[idx[:-1, :-1].ravel(), idx[:-1, 1:].ravel(), idx[1:, 1:].ravel(), idx[1:, :-1].ravel()]
    obj = "".join(f"v {a} {b} {c}\n" for a, b, c in verts) + "".join(
        f"f {a + 1} {b + 1} {c + 1} {d + 1}\n" for a, b, c, d in quads)
    ref, _ = meshload.load_obj(obj.encode(), "m.obj")
    tris = np.r_[verts[quads[:, [0, 1, 2]]], verts[quads[:, [0, 2, 3]]]]
    binary = b"\0" * 80 + struct.pack("<I", len(tris)) + b"".join(
        np.zeros(3, "<f4").tobytes() + t.astype("<f4").tobytes() + b"\0\0" for t in tris)
    ascii_ = ("solid m\n" + "".join("facet normal 0 0 1\nouter loop\n" + "".join(
        f"vertex {a} {b} {c}\n" for a, b, c in t.tolist()) + "endloop\nendfacet\n" for t in tris)
        + "endsolid m\n").encode()
    for data in (binary, ascii_):
        v, _ = meshload.parse_stl(data)
        assert len(v) == len(verts)                           # every corner welded back
        stl, warnings = meshload.load_stl(data, "m.stl")
        assert stl.elevation.shape == ref.elevation.shape
        assert np.nanmax(np.abs(stl.elevation - ref.elevation)) < 1e-5   # float32 in STL
        assert "millimetres" in warnings[-1]


# ------------------------------------------------------------------ vectors

def _dxf(units=6):
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = units
    msp = doc.modelspace()
    msp.add_lwpolyline(list(SQUARE.exterior.coords), close=True, dxfattribs={"layer": "beds"})
    msp.add_lwpolyline(list(SQUARE.interiors[0].coords), close=True, dxfattribs={"layer": "beds"})
    # a Rhino hatch exported WITH its boundary curve (Rhino keeps the curve)
    lawn = [(X0 + 200, Y0), (X0 + 250, Y0), (X0 + 250, Y0 + 50), (X0 + 200, Y0 + 50)]
    msp.add_lwpolyline(lawn, close=True, dxfattribs={"layer": "lawn"})
    msp.add_hatch(dxfattribs={"layer": "lawn"}).paths.add_polyline_path(lawn, is_closed=True)
    msp.add_lwpolyline(list(PATH.coords), dxfattribs={"layer": "paths"})
    msp.add_polyline3d([(X0, Y0 + 300, 5), (X0 + 50, Y0 + 300, 7)], dxfattribs={"layer": "paths"})
    for k in range(3):
        msp.add_point((TREE.x + k, TREE.y, 12.0), dxfattribs={"layer": "trees"})
    msp.add_text("note", dxfattribs={"layer": "notes"})
    out = io.StringIO()
    doc.write(out)
    return out.getvalue().encode()


def test_dxf_one_layer_per_cad_layer_and_kind():
    layers, warnings = read_vector(_dxf(), "site.dxf")
    got = {(name, kind) for kind, _, name in layers}
    assert got == {("beds", "polygon"), ("lawn", "polygon"), ("paths", "line"), ("trees", "point")}
    beds = next(g for k, g, n in layers if n == "beds")
    assert beds.area == pytest.approx(SQUARE.area)         # the inner curve cut a hole
    lawn = next(g for k, g, n in layers if n == "lawn")
    assert lawn.area == pytest.approx(50 * 50)              # hatch + its curve did not cancel
    paths = next(g for k, g, n in layers if n == "paths")
    assert len(paths.geoms) == 2 and not paths.has_z       # the 3D polyline lost its Z
    assert len(next(g for k, g, n in layers if n == "trees")) == 3
    assert any("TEXT" in w for w in warnings)
    _, mm = read_vector(_dxf(units=4), "site.dxf")
    assert any("millimetres" in w for w in mm)


def _geojson():
    feats = [{"type": "Feature", "properties": {}, "geometry": shapely.geometry.mapping(g)}
             for g in (SQUARE, PATH, TREE)]
    return __import__("json").dumps({"type": "FeatureCollection", "features": feats}).encode()


def _gml():
    ring = lambda r: " ".join(f"{x} {y} 1.0" for x, y in r.coords)
    return f"""<gml:FeatureCollection xmlns:gml="http://www.opengis.net/gml/3.2">
 <gml:featureMember><gml:Polygon srsDimension="3">
  <gml:exterior><gml:LinearRing><gml:posList>{ring(SQUARE.exterior)}</gml:posList></gml:LinearRing></gml:exterior>
  <gml:interior><gml:LinearRing><gml:posList>{ring(SQUARE.interiors[0])}</gml:posList></gml:LinearRing></gml:interior>
 </gml:Polygon></gml:featureMember>
 <gml:featureMember><gml:LineString><gml:posList>{" ".join(f"{x} {y}" for x, y in PATH.coords)}</gml:posList></gml:LineString></gml:featureMember>
 <gml:featureMember><gml:Point><gml:pos>{TREE.x} {TREE.y}</gml:pos></gml:Point></gml:featureMember>
</gml:FeatureCollection>""".encode()


def _gpkg():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE gpkg_geometry_columns (table_name TEXT, column_name TEXT)")
    for table, geoms in (("features", [SQUARE, PATH, TREE]),):
        con.execute(f'CREATE TABLE "{table}" (fid INTEGER PRIMARY KEY, geom BLOB)')
        con.execute("INSERT INTO gpkg_geometry_columns VALUES (?, 'geom')", (table,))
        for g in geoms:
            minx, miny, maxx, maxy = g.bounds          # flags 0b011: little endian + xy envelope
            head = b"GP\x00\x03" + struct.pack("<i", 25833) + struct.pack("<4d", minx, maxx, miny, maxy)
            con.execute(f'INSERT INTO "{table}" (geom) VALUES (?)', (head + shapely.to_wkb(g),))
    con.commit()
    return con.serialize()


@pytest.mark.parametrize("name,make", [("f.geojson", _geojson), ("f.gml", _gml), ("f.gpkg", _gpkg)])
def test_gis_formats_read_the_same_features(name, make):
    layers, warnings = read_vector(make(), name)
    by_kind = {kind: g for kind, g, _ in layers}
    assert set(by_kind) == {"polygon", "line", "point"} and not warnings
    assert by_kind["polygon"].area == pytest.approx(SQUARE.area)      # the hole survived
    assert by_kind["line"].length == pytest.approx(PATH.length)
    assert by_kind["point"] == [(TREE.x, TREE.y)]


def test_lonlat_and_unknown_files_are_explained(hills):
    store = core.Store()
    uid = core.load_terrain(store, hills.read_bytes(), "hills.tif")["upload_id"]
    res = core.add_hatch(store, uid, b'{"type": "Point", "coordinates": [18.9, 69.6]}', "p.geojson")
    assert "longitude/latitude" in res["warnings"][0]
    # a CAD drawing near 0, 0 over a mesh in the same local frame is no lon/lat
    obj = b"v -50 -40 10\nv 50 -40 12\nv 50 40 15\nv -50 40 11\nf 1 2 3 4\n"
    uid = core.load_terrain(store, obj, "local.obj")["upload_id"]
    res = core.add_hatch(store, uid, b'{"type": "Point", "coordinates": [3.0, 4.0]}', "p.geojson")
    assert res["warnings"] == []
    with pytest.raises(ValueError, match="not a vector format"):
        read_vector(b"", "drawing.svg")


def test_every_format_lands_where_the_terrain_is(hills):
    store = core.Store()
    uid = core.load_terrain(store, hills.read_bytes(), "hills.tif")["upload_id"]
    dtm = store.get_dtm(uid)
    for name, data in (("f.geojson", _geojson()), ("f.gml", _gml()), ("f.gpkg", _gpkg()),
                       ("f.dxf", _dxf())):
        res = core.add_hatch(store, uid, data, name)
        assert {l["kind"] for l in res["layers"]} == {"polygon", "line", "point"}
        assert not res["warnings"] or "TEXT" in res["warnings"][0]
    # the tree at world (X0+30, Y0+30) sits at that offset from the first cell centre
    pts = [h["polys"] for h in store.hatches[uid] if h["kind"] == "point"]
    assert pts[0] == [(X0 + 30 - dtm.origin_x, Y0 + 30 - dtm.origin_y)]
    assert pts[0][0] == pytest.approx((X0 + 30 - 500002.5, Y0 + 30 - (6600000 - 2.5 - 299 * 5)))
    sliced = core.do_slice(store, uid, {"scale": 3000})     # 500 mm board: fits the sheet
    assert sliced["stats"]["n_sheets"] > 0
    assert sum(len(p["hatch"]) for p in sliced["placements"]) > 0
