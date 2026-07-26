"""Slicing parameters, mirroring the sliders of DL-Contour_offset_method_011.gh."""
from __future__ import annotations

import math
from dataclasses import dataclass, field, fields


@dataclass
class SliceParams:
    scale: float = 500.0              # model scale denominator, e.g. 500 for 1:500
    thickness_mm: float = 2.0         # material thickness = contour interval on the model
    vertical_exaggeration: float = 1.0
    sheet_width_mm: float = 1000.0
    sheet_height_mm: float = 700.0
    sheet_margin_mm: float = 10.0
    part_spacing_mm: float = 5.0
    min_length_mm: float = 10.0       # drop curves shorter than this (GH: "delete if length less then 10 mm")
    label_height_mm: float = 3.0
    labels_enabled: bool = True
    label_density: float = 0.5        # 0 = no labels, 0.5 = only where a readable label fits,
                                      # 1 = force a (possibly tiny) label onto every ring
    label_font: str = "simplex"       # single-stroke face: simplex | roman | script (Hershey)
    label_simplify: float = 0.3       # 0..1 control-point reduction on curved glyph parts
    label_hidden: bool = True         # engrave numbers in the glue zone so the next ring
                                      # covers them; falls back to the visible ring where
                                      # the zone is too narrow (topmost rings stay blank)
    # per-shapefile hatch settings: list of dicts with keys
    # id, pattern, spacing_mm, rotation_deg, outline, color (see hatch.DEFAULT_HATCH)
    hatch_layers: list = field(default_factory=list)
    simplify_mm: float = 0.2
    base_plate: bool = True           # include the full-footprint bottom layer
    base_mode: str = "lowest"         # "lowest": contours start at the DTM minimum;
                                      # "zero": contours lie at absolute multiples of the interval (0 m origin)
    n_boards: int = 4                 # offset method: board k cuts contours k, k+N, k+2N, ...
    sheet_outline: bool = True        # draw sheet boundary on a non-cutting helper layer
    max_levels: int = 400

    def validate(self) -> list[str]:
        errors = []
        if self.scale <= 0: errors.append("scale must be > 0")
        if self.thickness_mm <= 0: errors.append("material thickness must be > 0")
        if self.vertical_exaggeration <= 0: errors.append("vertical exaggeration must be > 0")
        if self.sheet_width_mm <= 2 * self.sheet_margin_mm or self.sheet_height_mm <= 2 * self.sheet_margin_mm:
            errors.append("sheet size must be larger than twice the margin")
        if self.n_boards < 2: errors.append("number of boards must be >= 2 (a lower value leaves no glue surface)")
        if self.base_mode not in ("lowest", "zero"): errors.append("base_mode must be 'lowest' or 'zero'")
        if self.label_font not in ("simplex", "roman", "script"):
            errors.append("label_font must be simplex, roman or script")
        if not 0.0 <= self.label_simplify <= 1.0: errors.append("label_simplify must be within 0..1")
        if not 0.0 <= self.label_density <= 1.0: errors.append("label_density must be within 0..1")
        from .hatch import HATCH_PATTERNS, HATCH_COLORS, LINETYPES
        for i, layer in enumerate(self.hatch_layers):
            if not isinstance(layer, dict):
                errors.append(f"feature layer {i} must be an object")
                continue
            if layer.get("pattern", "lines") not in HATCH_PATTERNS:
                errors.append(f"feature layer {i}: unknown pattern {layer.get('pattern')!r}")
            if layer.get("color", "green") not in HATCH_COLORS:
                errors.append(f"feature layer {i}: color must be one of {', '.join(HATCH_COLORS)}")
            if layer.get("linetype", "solid") not in LINETYPES:
                errors.append(f"feature layer {i}: unknown linetype {layer.get('linetype')!r}")
            if layer.get("point_hatch", "none") not in ("none", *HATCH_PATTERNS):
                errors.append(f"feature layer {i}: unknown point hatch {layer.get('point_hatch')!r}")
            if float(layer.get("point_hatch_spacing_mm", 1.0)) <= 0:
                errors.append(f"feature layer {i}: point hatch spacing must be > 0")
            if float(layer.get("spacing_mm", 2.0)) <= 0:
                errors.append(f"feature layer {i}: spacing must be > 0")
            if float(layer.get("radius_mm", 2.0)) <= 0:
                errors.append(f"feature layer {i}: radius must be > 0")
            if float(layer.get("linetype_scale", 1.0)) <= 0:
                errors.append(f"feature layer {i}: linetype scale must be > 0")
        return errors

    @property
    def world_interval(self) -> float:
        """Real-world height (in DTM units, normally metres) represented by one material layer."""
        return self.thickness_mm * self.scale / 1000.0 / self.vertical_exaggeration

    @property
    def world_to_model(self) -> float:
        """Factor from DTM units (m) to model mm in plan."""
        return 1000.0 / self.scale

    def level_count(self, zmin: float, zmax: float) -> int:
        n = int(math.floor((zmax - zmin) / self.world_interval)) + 1
        return max(1, min(n, self.max_levels))

    @classmethod
    def from_dict(cls, d: dict) -> "SliceParams":
        allowed = {f.name: f.type for f in fields(cls)}
        kwargs = {}
        for k, v in d.items():
            if k in allowed:
                cur = getattr(cls(), k)
                kwargs[k] = type(cur)(v)
        return cls(**kwargs)
