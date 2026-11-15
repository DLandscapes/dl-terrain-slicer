"""Vector layers from GIS and CAD files: shapefile, DXF, GeoJSON, GML, GeoPackage.

`read_vector` returns layers in the shapes slice_dtm already consumes from
shapefiles - ("polygon", MultiPolygon), ("line", MultiLineString),
("point", [(x, y), ...]) - one layer per DXF layer / GeoPackage table and kind,
so a Rhino drawing with an outline layer and a tree layer arrives as two cards.

Coordinates stay in the file's own CRS and units; core moves them onto the
terrain by its origin, exactly as for shapefiles. That is why DXF is the CAD
route: a curve drawn in Rhino over the mesh exported as OBJ shares the mesh's
X/Y. Z is dropped - the terrain decides heights. No GDAL, no reprojection:
everything here also runs in the browser build.
"""
from __future__ import annotations

import io
import json
import xml.etree.ElementTree as ET
from functools import reduce

import shapely
from shapely.geometry import MultiLineString, MultiPolygon, Polygon, LineString, Point, shape

from .hatch import read_shp

EXTENSIONS = ("shp", "dxf", "geojson", "json", "gml", "gpkg")
_DXF_UNITS = {1: "inches", 2: "feet", 4: "millimetres", 5: "centimetres", 6: "metres"}


def read_vector(data: bytes, filename: str) -> tuple[list[tuple[str, object, str]], list[str]]:
    """Returns ([(kind, geometry, name), ...], warnings)."""
    ext = filename.lower().rsplit(".", 1)[-1]
    stem = filename.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if ext == "shp":
        kind, geom = read_shp(data)
        return [(kind, geom, stem)], []
    readers = {"dxf": _dxf, "geojson": _geojson, "json": _geojson, "gml": _gml, "gpkg": _gpkg}
    if ext not in readers:
        raise ValueError(f".{ext} is not a vector format the slicer reads "
                         f"(shapefile, DXF, GeoJSON, GML or GeoPackage)")
    groups, warnings = readers[ext](data)
    layers = [layer for name, geoms in groups for layer in _by_kind(geoms, name or stem)]
    if not layers:
        raise ValueError("the file holds no polygons, lines or points")
    return layers, warnings


def _by_kind(geoms, name):
    """Split geometries into one polygon, one line and one point layer, 2D."""
    polys, lines, points = [], [], []
    for g in geoms:
        for part in shapely.get_parts(shapely.force_2d(g)):   # explodes Multi* and collections
            if isinstance(part, Polygon):
                polys.append(shapely.make_valid(part))
            elif isinstance(part, LineString):                # LinearRing is a LineString too
                lines.append(LineString(part.coords))
            elif isinstance(part, Point):
                points.append((part.x, part.y))
    out = []
    if polys:
        area = shapely.make_valid(shapely.union_all(polys))
        area = MultiPolygon([p for p in shapely.get_parts(area) if isinstance(p, Polygon)])
        if not area.is_empty:
            out.append(("polygon", area, name))
    if lines:
        out.append(("line", MultiLineString(lines), name))
    if points:
        out.append(("point", points, name))
    return out


def _dxf(data):
    """DXF from CAD (Rhino, AutoCAD, QGIS): closed curves, circles and hatches are
    areas (a curve inside another cuts a hole), open curves lines, POINTs points;
    one group per DXF layer."""
    from ezdxf import recover
    from ezdxf.addons import geo
    doc, _auditor = recover.read(io.BytesIO(data))
    groups: dict[str, list] = {}
    curves: dict[str, list] = {}
    skipped: dict[str, int] = {}
    for e in doc.modelspace():
        try:
            g = shape(geo.proxy(e, distance=0.02).__geo_interface__)   # arcs to 2 cm chords
        except (TypeError, ValueError):                       # TEXT, INSERT, MESH, ...
            skipped[e.dxftype()] = skipped.get(e.dxftype(), 0) + 1
            continue
        group = groups.setdefault(e.dxf.layer, [])             # keeps the drawing's layer order
        if isinstance(g, Polygon) and e.dxftype() != "HATCH":
            curves.setdefault(e.dxf.layer, []).append(shapely.make_valid(g))
        else:
            group.append(g)
    # a closed curve inside another is a hole, as CAD hatching reads islands
    # (even-odd); a HATCH keeps its own holes and is added on top, so a hatch
    # exported with its boundary curve does not cancel out
    for layer, polys in curves.items():
        groups[layer].append(reduce(shapely.symmetric_difference, polys))
    warnings = []
    if skipped:
        warnings.append("left out (not a curve or point): " +
                        ", ".join(f"{n} x {t}" for t, n in sorted(skipped.items())))
    units = _DXF_UNITS.get(doc.header.get("$INSUNITS", 0))
    if units and units != "metres":
        warnings.append(f"the drawing is in {units}; its coordinates are used as they are, "
                        f"so the terrain must be in {units} too")
    return list(groups.items()), warnings


def _geojson(data):
    obj = json.loads(data)
    items = obj.get("features", [obj]) if isinstance(obj, dict) else []
    geoms = [shape(f.get("geometry", f) if f.get("type") == "Feature" else f)
             for f in items if (f.get("geometry") if f.get("type") == "Feature" else f)]
    return [(None, geoms)], []


def _gml(data):
    """GML 2 and 3 (local tag names, so every namespace version works)."""
    root = ET.fromstring(data)
    local = lambda el: el.tag.rsplit("}", 1)[-1]
    geoms = []

    def coords(el, dim=2):
        for sub in el.iter():
            name = local(sub)
            if name == "posList":
                d = int(sub.get("srsDimension") or el.get("srsDimension") or dim)
                v = [float(t) for t in sub.text.split()]
                return [tuple(v[i:i + 2]) for i in range(0, len(v), d)]
            if name == "coordinates":                         # GML 2: "x,y x,y"
                return [tuple(float(t) for t in p.split(",")[:2]) for p in sub.text.split()]
        return [tuple(float(t) for t in p.text.split()[:2])
                for p in el.iter() if local(p) == "pos"]

    def rings(el, names, dim):
        return [coords(r, dim) for r in el.iter() if local(r) in names]

    def walk(el, dim):
        name = local(el)
        dim = int(el.get("srsDimension") or dim)
        if name == "Polygon":
            outer = rings(el, ("exterior", "outerBoundaryIs"), dim)
            if outer:
                geoms.append(Polygon(outer[0], rings(el, ("interior", "innerBoundaryIs"), dim)))
            return
        if name in ("LineString", "LineStringSegment"):
            geoms.append(LineString(coords(el, dim)))
            return
        if name == "Point":
            geoms.append(Point(coords(el, dim)[0]))
            return
        for child in el:
            walk(child, dim)

    walk(root, 2)
    return [(None, geoms)], []


def _gpkg(data):
    """GeoPackage (SQLite): one group per feature table. The geometry blob is a
    'GP' header with an optional envelope, then standard WKB."""
    import sqlite3
    con = sqlite3.connect(":memory:")
    con.deserialize(data)
    groups = []
    for table, column in con.execute("SELECT table_name, column_name FROM gpkg_geometry_columns"):
        geoms = []
        for (blob,) in con.execute(f'SELECT "{column}" FROM "{table}"'):
            if not blob or blob[:2] != b"GP" or blob[3] & 0x10:   # missing or empty geometry
                continue
            envelope = (0, 32, 48, 48, 64)[(blob[3] >> 1) & 0x07]
            geoms.append(shapely.from_wkb(bytes(blob[8 + envelope:])))
        groups.append((table, geoms))
    con.close()
    return groups, []
