"""Drafting (Drawing) tools for CATIA V5 and CATIA 3DEXPERIENCE.

Built from field-validated automation probes run on CATIA V5R20 by
contributor ESE3X: Drawing document creation, generative view
creation (Sheets / Views.Add / GenerativeBehavior / DefineFrontView /
Update) and view positioning are confirmed working through COM.

Dimensioning: CATIA never exposes the projected curves of generative
views to Automation (field-proven on V5R20), so generative views are
dimensioned from the part's 3D sketch constraints through
DrawingSheet.GenerateDimensions (the agent expresses design intent as
constraints, CATIA places the dimensions). The manual Add path only
works on 2D geometry drawn in the view. Projection, section and detail
views, text and tables are provided. Sheet setup, view/dimension
placement, dimension texts, table cells, PLM attribute links and template
copy/paste follow recipes field-proven on 3DEXPERIENCE R428 SP6
(field report 2026-11-02; kept outside the repository).

3DEXPERIENCE: a drawing is created with PLMNewService ("Drawing") and the
editor's ActiveObject is a DrawingRoot (Sheets, ActiveSheet) instead of a
DrawingDocument. Texts, tables and dimensions keep the V5 API. Generative
views are created by the DrawingDefineGenView factory
(Sheet.Views.DrawingDefineGenView) that replaced V5's
DrawingViewGenerativeBehavior. Its Define* functions (DSYAutomation.chm and
type information of R428 SP6) take the view anchor (iXPos, iYpos) first and
iViewStyle ("" = no Generative View Style), iComputeUpdate and iViewProp
(CATDrawingGenService.DrawingGenViewProp) last, and return the view. The
call is bound by parameter name from the installation's type information
and refused with the discovered signature when a parameter is unknown, so a
release with another signature fails before anything is created. Front
views project product instances (iListofPrdInst). Where a release still
exposes view.GenerativeBehavior, the V5 recipe is used as is.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from typing import Any, ClassVar

from catia_mcp.automation import (
    call_out_object,
    call_out_values,
    get_member,
    iter_items,
    name_exists,
    resolves,
    safe_get,
)
from catia_mcp.connection import CATIAConnection
from catia_mcp.errors import (
    AmbiguousTarget,
    CATIAToolError,
    InvalidArgument,
    ObjectNotFound,
    UnsupportedCapability,
    WrongDocumentType,
    after_mutation,
)
from catia_mcp.introspect import method_params
from catia_mcp.plm import PLMSession
from catia_mcp.references import search_final_topology
from catia_mcp.tools.measurement import read_measurable_array

# DefineFrontView takes the two sheet-plane axis vectors expressed in the
# 3D part's coordinate system.
# CatProjViewType (R20 automation enum, verified against the V5 reference
# binding): the projection of a view relative to its parent.
_PROJECTION_TYPES = {"right": 0, "left": 1, "top": 2, "bottom": 3, "rear": 4}

_PLANE_VECTORS = {
    "xy": (1, 0, 0, 0, 1, 0),
    "yz": (0, 1, 0, 0, 0, 1),
    "zx": (0, 0, 1, 1, 0, 0),
}

# Section profile types: V5 GenerativeBehavior names -> the legal values the
# 3DEXPERIENCE factory documents (DSYAutomation.chm R428).
_FACTORY_PROFILE_TYPES = {
    "Offset": "CatOffsetSectionProfile",
    "Aligned": "CatAlignedSectionProfile",
}


# CatDimType kinds (0-based V5 enum_CatDimType; Angle = 4 and Radius = 5,
# Diameter = 9 verified, the other members from the V5 enum order).
_DIM_KINDS = {
    0: "distance", 1: "distance", 2: "length", 3: "length", 4: "angle",
    5: "radius", 6: "radius", 7: "radius", 8: "radius",
    9: "diameter", 10: "diameter", 11: "diameter", 12: "diameter", 13: "diameter",
}


def _dim_kind(dim_type: int | None) -> str:
    return _DIM_KINDS.get(dim_type, "other") if dim_type is not None else "unknown"


# Hole-internal filter (catia_drawing_generate_dimensions skip_hole_internals).
# Drill-point angles of standard twist drills / CATIA hole bottoms.
_DRILL_POINT_ANGLES = (118.0, 120.0)
# ISO 261 metric pitches (coarse first, then fine) per nominal diameter.
_ISO_PITCHES: dict[float, tuple[float, ...]] = {
    1.6: (0.35, 0.2), 2.0: (0.4, 0.25), 2.5: (0.45, 0.35), 3.0: (0.5, 0.35),
    4.0: (0.7, 0.5), 5.0: (0.8, 0.5), 6.0: (1.0, 0.75), 8.0: (1.25, 1.0, 0.75),
    10.0: (1.5, 1.25, 1.0, 0.75), 12.0: (1.75, 1.5, 1.25, 1.0), 14.0: (2.0, 1.5),
    16.0: (2.0, 1.5), 18.0: (2.5, 2.0, 1.5), 20.0: (2.5, 2.0, 1.5),
    22.0: (2.5, 2.0, 1.5), 24.0: (3.0, 2.0, 1.5), 27.0: (3.0, 2.0), 30.0: (3.5, 2.0),
}

# Sheet setup (catia_drawing_set_sheet). Values -> (enum value, field-proven).
# Field-proven on R428 SP6 (2026-11-02 field report): PaperSize A0=2, A1=3,
# A2=4, A3=5; Orientation = 1 (landscape, the A1 landscape result);
# ProjectionMethod = 1 (third angle). The others follow the V5 enums
# CatPaperSize (Letter=0, Legal=1, A0..A4=2..6, A=7, B=8, ...),
# CatPaperOrientation (Portrait=0, Landscape=1) and CatSheetProjectionMethod
# (FirstAngle=0, ThirdAngle=1): unverified on R428, read back after setting.
_PAPER_SIZES: dict[str, tuple[int, bool]] = {
    "A0": (2, True), "A1": (3, True), "A2": (4, True), "A3": (5, True),
    "A4": (6, False), "LETTER": (0, False), "TABLOID": (8, False),
}
_ORIENTATIONS: dict[str, tuple[int, bool]] = {"landscape": (1, True), "portrait": (0, False)}
_PROJECTION_METHODS: dict[str, tuple[int, bool]] = {"third_angle": (1, True), "first_angle": (0, False)}


_SHEET_PROPERTIES: dict[str, Any] = {
    "paper_size": {
        "type": "string",
        "enum": ["A0", "A1", "A2", "A3", "A4", "letter", "tabloid"],
        "description": (
            "Paper format. A0-A3 field-proven on R428 SP6; A4, letter and tabloid "
            "(ANSI B, 11x17) use the V5 CatPaperSize values, unverified on 3DEXPERIENCE."
        ),
    },
    "orientation": {
        "type": "string",
        "enum": ["landscape", "portrait"],
        "description": "Sheet orientation (landscape field-proven; portrait unverified)",
    },
    "projection": {
        "type": "string",
        "enum": ["third_angle", "first_angle"],
        "description": (
            "Projection method: third_angle (US/ASME, field-proven) or first_angle "
            "(ISO, CATIA's default)"
        ),
    },
    "scale": {"type": "number", "description": "Sheet scale, e.g. 1 for 1:1, 0.5 for 1:2"},
}


# Paper sizes in mm as (short side, long side) per CatPaperSize value, for
# sheets whose GetPaperWidth / GetPaperHeight cannot be read. ISO 216 and
# ANSI sizes; the enum values are the ones in _PAPER_SIZES above.
_PAPER_MM: dict[int, tuple[float, float]] = {
    0: (215.9, 279.4), 1: (215.9, 355.6), 2: (841.0, 1189.0), 3: (594.0, 841.0),
    4: (420.0, 594.0), 5: (297.0, 420.0), 6: (210.0, 297.0), 8: (279.4, 431.8),
}

# In-plane axes of each projection plane as DefineFrontView receives them:
# (horizontal on the sheet, vertical on the sheet). From _PLANE_VECTORS:
# zx = (0,0,1 | 1,0,0) puts the part's Z axis HORIZONTAL (field report
# 2026-11-03: the ZX front view came out rotated); yz keeps Z vertical.
_PLANE_AXES = {"xy": ("x", "y"), "yz": ("y", "z"), "zx": ("z", "x")}

# Where annotation x / y are measured (catia_drawing_add_text, add_table,
# edit_text).
_COORDS_PROPERTY = {
    "type": "string",
    "enum": ["view", "sheet"],
    "description": (
        "Frame of x / y. 'view' (default): view units, measured from the view's axis "
        "origin (xAxisData, yAxisData) in model mm, i.e. sheet distance divided by the "
        "view scale. NOT from the view's position (view.x / view.y), which can be "
        "hundreds of mm away. 'sheet': paper mm from the sheet's lower-left corner; "
        "converted with x_view = (sheet_x - xAxisData) / Scale (field-proven on R428 "
        "SP6). On the Background view both frames coincide."
    ),
}


def _axis_number(view: Any, member: str) -> float | None:
    """A numeric view member that may surface as a property or a method."""
    try:
        value = getattr(view, member)
        if callable(value):
            value = value()
        return float(value)
    except Exception:
        return None


def _sheet_to_view(view: Any, sheet_x: float | None, sheet_y: float | None) -> tuple[
    float | None, float | None, list[str]
]:
    """Convert sheet mm to the view units DrawingTexts.Add / DrawingTables.Add
    and DrawingText.x / .y use.

    Field-proven on R428 SP6 (field report 2026-11-04): these coordinates are
    view units relative to the view's axis origin (xAxisData, yAxisData),
    divided by the view scale: x = (sheet_x - xAxisData) / Scale. The view's
    rotation (Angle) is NOT part of the proven formula; a rotated view only
    gets a warning. Private helper: a shared drawing model module may provide
    the same conversion later."""
    notes: list[str] = []
    scale = _axis_number(view, "Scale")
    if scale is None or abs(scale) < 1e-12:
        raise UnsupportedCapability(
            f"View '{safe_get(view, 'Name', '?')}' has no readable Scale, so sheet "
            "coordinates cannot be converted. Pass coords='view'."
        )
    out: list[float | None] = []
    for value, member in ((sheet_x, "xAxisData"), (sheet_y, "yAxisData")):
        if value is None:
            out.append(None)
            continue
        origin = _axis_number(view, member)
        if origin is None:
            raise UnsupportedCapability(
                f"View '{safe_get(view, 'Name', '?')}' exposes no {member} (axis origin), so "
                "sheet coordinates cannot be converted on this release. Pass coords='view'."
            )
        out.append((float(value) - origin) / scale)
    angle = _axis_number(view, "Angle")
    if angle and abs(angle) > 1e-9:
        notes.append(
            f"The view is rotated ({math.degrees(angle):g} deg); the sheet-to-view conversion "
            "ignores rotation (unverified for rotated views): check with catia_screenshot."
        )
    return out[0], out[1], notes


def _coords_mode(args: dict[str, Any]) -> str:
    mode = str(args.get("coords") or "view").strip().lower()
    if mode not in ("view", "sheet"):
        raise InvalidArgument(f"Unknown coords '{args.get('coords')}'. Use 'view' or 'sheet'.")
    return mode


def _up_axis_angle(plane: str, up_axis: Any, angle: Any) -> tuple[float | None, list[str]]:
    """In-plane rotation (degrees) for catia_drawing_add_view, from up_axis.

    Field-proven on R428 SP6 (field report 2026-11-03): a front view on ZX
    shows the part's Z axis horizontal and angle=90 turned it upright. The
    general rule (an axis that is horizontal in the plane becomes vertical
    with a 90 deg rotation) follows from the plane vectors; whether it then
    points up or down is unverified for other planes."""
    notes: list[str] = []
    horizontal, vertical = _PLANE_AXES[plane]
    if up_axis is None or up_axis == "":
        if angle is None and plane == "zx":
            notes.append(
                "Front view on ZX shows the part's Z axis HORIZONTAL (field report "
                "2026-11-03). If Z should point up, recreate it with up_axis='z' (same as "
                "angle=90) or rotate it."
            )
        return (float(angle) if angle is not None else None), notes
    axis = str(up_axis).strip().lower()
    if axis not in ("x", "y", "z"):
        raise InvalidArgument(f"Unknown up_axis '{up_axis}'. Use 'x', 'y' or 'z'.")
    if axis == vertical:
        wanted = 0.0
    elif axis == horizontal:
        wanted = 90.0
        notes.append(
            f"up_axis='{axis}' on plane {plane.upper()}: rotated the view by 90 deg"
            + (" (field-proven for ZX / Z up on R428 SP6)." if (plane, axis) == ("zx", "z") else
               " (unverified for this plane: if the axis points down, pass angle=-90).")
        )
    else:
        raise InvalidArgument(
            f"up_axis '{axis}' is the viewing direction of plane {plane.upper()} (it is "
            f"normal to the sheet). In-plane axes: {horizontal} (horizontal), {vertical} "
            "(vertical)."
        )
    if angle is not None and abs(float(angle) - wanted) > 1e-9:
        raise InvalidArgument(
            f"angle={angle} contradicts up_axis='{axis}' (which needs angle={wanted:g}). "
            "Pass one of them."
        )
    return wanted, notes


class _Lazy:
    """A factory argument resolved only if the signature asks for it."""

    def __init__(self, resolve: Any) -> None:
        self.resolve = resolve


class DraftingTools:
    """Tools for CATIA drawings (generative drafting), V5 and 3DEXPERIENCE."""

    def __init__(self, connection: CATIAConnection) -> None:
        self.conn = connection
        self.plm = PLMSession(connection)

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "catia_new_drawing",
                "description": (
                    "Create a new empty Drawing document (CATDrawing) for "
                    "generative drafting. The drawing becomes the active "
                    "document; the source Part stays open and is referenced "
                    "by catia_drawing_add_view. Optional paper_size, "
                    "orientation and projection set up the first sheet as "
                    "catia_drawing_set_sheet does; left out, CATIA's defaults "
                    "stay (R428 SP6: A0, first angle). US drawings usually "
                    "want projection='third_angle'."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": _SHEET_PROPERTIES,
                },
            },
            {
                "name": "catia_drawing_list_view_geometry",
                "description": (
                    "List the manually drawn 2D geometry of a drawing view "
                    "as indexed elements for catia_drawing_add_dimension. "
                    "Field-proven on V5R20: generated projection curves are "
                    "NOT exposed here (GeometricElements shows only the axis "
                    "and Factory2D items). To dimension generative views use "
                    "catia_drawing_generate_dimensions. Defaults to the last "
                    "view of the active sheet."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "view_name": {
                            "type": "string",
                            "description": "View to inspect (default: last view of the active sheet)",
                        },
                    },
                },
            },
            {
                "name": "catia_drawing_add_dimension",
                "description": (
                    "Add a dimension between one or two manually drawn 2D "
                    "elements (indices from catia_drawing_list_view_geometry). "
                    "Works on Factory2D geometry only (R20: generated curves "
                    "are not addressable); for generative views use "
                    "catia_drawing_generate_dimensions."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["distance", "length", "angle", "radius", "diameter"],
                            "description": (
                                "Dimension type. 'length'/'radius'/'diameter' "
                                "take one element; 'distance'/'angle' take two."
                            ),
                        },
                        "element_index_1": {
                            "type": "integer",
                            "description": "First element index from catia_drawing_list_view_geometry",
                        },
                        "element_index_2": {
                            "type": "integer",
                            "description": "Second element index (distance/angle)",
                        },
                        "view_name": {
                            "type": "string",
                            "description": "Target view (default: last view of the active sheet)",
                        },
                    },
                    "required": ["type", "element_index_1"],
                },
            },
            {
                "name": "catia_drawing_generate_dimensions",
                "description": (
                    "Generate associative dimensions on a generative view "
                    "from the part's 3D sketch constraints (one dimension per "
                    "distance/length/angle/radius/diameter constraint). This "
                    "is the only route that dimensions generated geometry in "
                    "CATIA V5 (field-proven on R20): add constraints with "
                    "catia_sketch_constraint, then call this. Reports the "
                    "dimensions with values; generated ones follow the 3D "
                    "model (status 3d_driven). Runs sheet-wide; each "
                    "dimension lands in the view parallel to its constraint. "
                    "3DEXPERIENCE R428 (live): Pad/Pocket lengths are "
                    "generated too, and a radius constraint stays a radius "
                    "(R) dimension (automation cannot switch it to diameter). "
                    "Every view of every sheet is compared before and after, "
                    "and every new dimension is reported with its view. With "
                    "view_name, new dimensions that landed in other views are "
                    "deleted. skip_hole_internals (default true) deletes "
                    "generated ISO tap-drill diameters and, only in a view "
                    "that shows a hole (tap-drill pair, or drill-point angle "
                    "next to a diameter), 118/120 deg drill-point angles and "
                    "duplicates (same type and value). Angles are in degrees. Dimensions are not moved off "
                    "the geometry: use catia_drawing_move_dimension. LIMITS (field report "
                    "2026-11-03, R428 SP6): edges of generated views are NOT addressable by "
                    "automation (a selection search returns generated items that resolve only to "
                    "the view), so dimensions picked on model edges must be placed by the user in "
                    "the CATIA UI; afterwards catia_drawing_set_dimension_frame / "
                    "catia_drawing_set_dimension_text format them. Dimensions generated from 3D "
                    "constraints land in the view parallel to the constraint, not necessarily the "
                    "view asked for."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "view_name": {
                            "type": "string",
                            "description": (
                                "Keep only this view's new dimensions (others are deleted). "
                                "Default: activate the last view and keep every new dimension."
                            ),
                        },
                        "skip_hole_internals": {
                            "type": "boolean",
                            "description": (
                                "Delete generated tap-drill diameters and, in views showing "
                                "a hole, drill-point angles and duplicate values (default true)"
                            ),
                        },
                    },
                },
            },
            {
                "name": "catia_drawing_list_dimensions",
                "description": (
                    "List the dimensions of a drawing view with their index, "
                    "value (mm, angles in degrees), kind and status (basic_2d "
                    "or 3d_driven). Regenerates the view "
                    "first so 3d_driven values reflect the current part "
                    "(set refresh=false to skip)."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "view_name": {
                            "type": "string",
                            "description": "View to inspect (default: last view of the active sheet)",
                        },
                        "refresh": {
                            "type": "boolean",
                            "description": "Regenerate the view before reading (default true)",
                        },
                    },
                },
            },
            {
                "name": "catia_drawing_projection_view",
                "description": (
                    "Add a projection view (right/left/top/bottom/rear) "
                    "derived from an existing generative view, placed next "
                    "to it at the same scale. DefineProjectionView with the "
                    "documented CatProjViewType enum. Placement follows the "
                    "sheet's projection method, which the result names: on a "
                    "first-angle sheet (R428 default) 'bottom' lands ABOVE the "
                    "parent and 'right' to its left. Switch with "
                    "catia_drawing_set_sheet projection='third_angle'. Projection "
                    "views follow the parent's orientation: fix a sideways front "
                    "view (ZX plane, Z horizontal) with catia_drawing_add_view "
                    "up_axis='z' before deriving views."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "direction": {
                            "type": "string",
                            "enum": ["right", "left", "top", "bottom", "rear"],
                            "description": "Projection relative to the parent view",
                        },
                        "parent_view": {
                            "type": "string",
                            "description": "Parent view name (default: last view of the active sheet)",
                        },
                        "name": {"type": "string", "description": "New view name (default '<direction> view')"},
                        "gap": {
                            "type": "number",
                            "description": "Distance from the parent view in mm (default 100)",
                        },
                        "x": {"type": "number", "description": "Explicit X position in mm (overrides gap)"},
                        "y": {"type": "number", "description": "Explicit Y position in mm (overrides gap)"},
                    },
                    "required": ["direction"],
                },
            },
            {
                "name": "catia_drawing_section_view",
                "description": (
                    "Add a section view or section cut from a cutting "
                    "profile drawn as a polyline in the parent view's 2D "
                    "coordinates (mm). DefineSectionView per the V5 "
                    "reference: profile points, section type, profile type, "
                    "side to draw, parent view."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "profile": {
                            "type": "array",
                            "items": {"type": "number"},
                            "description": "Flat list [x1, y1, x2, y2, ...] of the cutting polyline in parent view coordinates (mm), at least two points",
                        },
                        "parent_view": {"type": "string", "description": "Parent view name (default: last view)"},
                        "section_type": {
                            "type": "string",
                            "enum": ["SectionView", "SectionCut"],
                            "description": "SectionView shows geometry behind the plane, SectionCut only the cut (default SectionView)",
                        },
                        "profile_type": {
                            "type": "string",
                            "enum": ["Offset", "Aligned"],
                            "description": "Cutting profile type (default Offset)",
                        },
                        "side": {
                            "type": "integer",
                            "enum": [0, 1],
                            "description": "Side to draw: 0 clockwise, 1 counterclockwise (default 1)",
                        },
                        "name": {"type": "string", "description": "New view name"},
                        "x": {"type": "number", "description": "X position in mm (default: parent x + 150)"},
                        "y": {"type": "number", "description": "Y position in mm (default: parent y)"},
                    },
                    "required": ["profile"],
                },
            },
            {
                "name": "catia_drawing_detail_view",
                "description": (
                    "Add a circular detail view of a region of the parent "
                    "view (center and radius in the parent view's 2D "
                    "coordinates, mm). DefineCircularDetailView."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "center_x": {"type": "number", "description": "Circle center X in parent view coordinates (mm)"},
                        "center_y": {"type": "number", "description": "Circle center Y in parent view coordinates (mm)"},
                        "radius": {"type": "number", "description": "Circle radius (mm)"},
                        "parent_view": {"type": "string", "description": "Parent view name (default: last view)"},
                        "scale": {"type": "number", "description": "Detail scale factor (default 2)"},
                        "name": {"type": "string", "description": "New view name"},
                        "x": {"type": "number", "description": "X position in mm (default: parent x + 150)"},
                        "y": {"type": "number", "description": "Y position in mm (default: parent y + 100)"},
                    },
                    "required": ["center_x", "center_y", "radius"],
                },
            },
            {
                "name": "catia_drawing_add_text",
                "description": (
                    "Add a text annotation to a drawing view (DrawingTexts.Add). "
                    "By default (coords='view') x / y are VIEW units: model mm from "
                    "the view's axis origin (xAxisData, yAxisData), i.e. sheet "
                    "distance / view scale, NOT from the view's position. Pass "
                    "coords='sheet' to give paper mm and let the tool convert "
                    "(field-proven formula on R428 SP6). Use the sheet's Background "
                    "view for title block text (there both frames coincide)."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "Text content"},
                        "x": {"type": "number", "description": "X in mm (frame per 'coords')"},
                        "y": {"type": "number", "description": "Y in mm (frame per 'coords')"},
                        "coords": _COORDS_PROPERTY,
                        "view_name": {
                            "type": "string",
                            "description": "Target view (default: last view; use 'Background View' for frames and title blocks)",
                        },
                    },
                    "required": ["text", "x", "y"],
                },
            },
            {
                "name": "catia_drawing_add_table",
                "description": (
                    "Add a table to a drawing view (DrawingTables.Add) and "
                    "optionally fill its cells. Building block for title "
                    "blocks, revision tables and BOMs: pass 'cells' as rows "
                    "of strings, optional per-column widths, and the "
                    "Background View as target for sheet-level tables. x / y are "
                    "view units (from the view's axis origin, divided by its scale) "
                    "unless coords='sheet' (paper mm, converted by the tool)."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "x": {"type": "number", "description": "Table X in mm (frame per 'coords')"},
                        "y": {"type": "number", "description": "Table Y in mm (frame per 'coords')"},
                        "coords": _COORDS_PROPERTY,
                        "rows": {"type": "integer", "description": "Number of rows"},
                        "columns": {"type": "integer", "description": "Number of columns"},
                        "row_height": {"type": "number", "description": "Row height in mm (default 8)"},
                        "column_width": {"type": "number", "description": "Column width in mm (default 40)"},
                        "cells": {
                            "type": "array",
                            "items": {"type": "array", "items": {"type": "string"}},
                            "description": "Optional cell contents, row-major, e.g. [[\"Part\", \"Qty\"], [\"Bracket\", \"2\"]]",
                        },
                        "column_widths": {
                            "type": "array",
                            "items": {"type": "number"},
                            "description": "Optional per-column widths in mm (0 = automatic)",
                        },
                        "view_name": {
                            "type": "string",
                            "description": "Target view (default: last view; use 'Background View' for title blocks)",
                        },
                    },
                    "required": ["x", "y", "rows", "columns"],
                },
            },
            {
                "name": "catia_drawing_list_views",
                "description": (
                    "List the sheets of the active drawing and the views of each sheet "
                    "(name, role main/background/user, position, scale, generative or "
                    "not), to pick view_name / parent_view values. 'main' and "
                    "'background' are accepted as view_name aliases."
                ),
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "catia_drawing_add_view",
                "description": (
                    "Add a generative front view of an open Part to the "
                    "active Drawing's active sheet, projected on the chosen "
                    "plane or on a planar face, then update it. Create the "
                    "drawing first with catia_new_drawing; derive other "
                    "views with catia_drawing_projection_view / "
                    "section_view / detail_view. Field-validated on V5R20. "
                    "3DEXPERIENCE: projects a product (3D Part or Physical "
                    "Product, also a 3D Shape edited inside one), plane only; "
                    "a standalone 3D Shape fails with UNSUPPORTED_CAPABILITY "
                    "(live-checked on R428 SP6)."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "part_name": {
                            "type": "string",
                            "description": (
                                "Open Part document to represent (e.g. "
                                "'Part1.CATPart'; on 3DEXPERIENCE the title "
                                "of the open 3D content). Defaults to the "
                                "only open Part when unambiguous."
                            ),
                        },
                        "body_name": {
                            "type": "string",
                            "description": (
                                "Optional body to draw alone (e.g. 'PartBody' "
                                "or 'Body.2'); other bodies are excluded. "
                                "Field-validated on V5R20."
                            ),
                        },
                        "plane": {
                            "type": "string",
                            "enum": ["xy", "yz", "zx"],
                            "description": "Projection plane (default xy); ignored when 'face' is given",
                        },
                        "face": {
                            "type": "string",
                            "description": (
                                "Face-driven projection: 'Face.N' of the "
                                "body's final shape, as listed by "
                                "catia_list_faces run with the SAME body_name; "
                                "the view is projected on that planar face's "
                                "own plane, orientation-independent. "
                                "Field-proven on V5R20."
                            ),
                        },
                        "angle": {
                            "type": "number",
                            "description": (
                                "In-plane rotation of the view in degrees (default 0). "
                                "Plane zx shows the part's Z axis horizontal; angle=90 "
                                "turns it upright (field-proven on R428 SP6)."
                            ),
                        },
                        "up_axis": {
                            "type": "string",
                            "enum": ["x", "y", "z"],
                            "description": (
                                "Part axis that should point up on the sheet; mapped to "
                                "'angle' (0 if it already is vertical in the plane, 90 "
                                "if horizontal). E.g. plane='zx', up_axis='z'. Not with "
                                "'face'."
                            ),
                        },
                        "x": {
                            "type": "number",
                            "description": "View X position on the sheet in mm (default 300)",
                        },
                        "y": {
                            "type": "number",
                            "description": "View Y position on the sheet in mm (default 150)",
                        },
                        "name": {
                            "type": "string",
                            "description": "View name (default 'Front View')",
                        },
                    },
                },
            },
            *self._layout_tool_definitions(),
        ]

    def execute(self, tool_name: str, arguments: dict[str, Any]) -> str:
        match tool_name:
            case "catia_new_drawing":
                return self._new_drawing(arguments)
            case "catia_drawing_add_view":
                return self._add_view(arguments)
            case "catia_drawing_projection_view":
                return self._projection_view(arguments)
            case "catia_drawing_section_view":
                return self._section_view(arguments)
            case "catia_drawing_detail_view":
                return self._detail_view(arguments)
            case "catia_drawing_add_text":
                return self._add_text(arguments)
            case "catia_drawing_add_table":
                return self._add_table(arguments)
            case "catia_drawing_generate_dimensions":
                return self._generate_dimensions(arguments)
            case "catia_drawing_list_dimensions":
                return self._list_dimensions(arguments)
            case "catia_drawing_list_view_geometry":
                return self._list_view_geometry(arguments)
            case "catia_drawing_add_dimension":
                return self._add_dimension(arguments)
            case "catia_drawing_list_views":
                return self._list_views()
            case "catia_drawing_set_sheet":
                return self._set_sheet(arguments)
            case "catia_drawing_move_view":
                return self._move_view(arguments)
            case "catia_drawing_move_dimension":
                return self._move_dimension(arguments)
            case "catia_drawing_delete_dimension":
                return self._delete_dimension(arguments)
            case "catia_drawing_set_dimension_text":
                return self._set_dimension_text(arguments)
            case "catia_drawing_set_table_cell":
                return self._set_table_cell(arguments)
            case "catia_drawing_edit_text":
                return self._edit_text(arguments)
            case "catia_drawing_link_attribute":
                return self._link_attribute(arguments)
            case "catia_drawing_apply_template":
                return self._apply_template(arguments)
            case "catia_drawing_print":
                return self._print(arguments)
            case _:
                raise ValueError(f"Unknown drafting tool: {tool_name}")

    @staticmethod
    def _active_sheet(drawing: Any) -> Any:
        """V5 DrawingDocument.Sheets.ActiveSheet; 3DEXPERIENCE DrawingRoot
        exposes the same chain and also DrawingRoot.ActiveSheet."""
        try:
            return drawing.Sheets.ActiveSheet
        except Exception:
            return drawing.ActiveSheet

    def _new_drawing(self, args: dict[str, Any] | None = None) -> str:
        self.conn.ensure_connected()
        # Validate the sheet options before anything is created.
        settings = self._sheet_settings(args or {})
        if self.conn.is_3dx:
            self.plm.create("Drawing")
            with after_mutation():
                drawing = self.conn.active_object()
                sheet = self._active_sheet(drawing)
                setup = self._sheet_setup_text(sheet, settings)
            return (
                f"Drawing created in the session: '{self.plm.title_of(drawing)}' (active "
                f"sheet '{sheet.Name}'){setup}. Use catia_drawing_add_view to project a 3D part "
                "open in another editor; save with catia_save_document."
            )
        drawing = self.conn.documents.Add("Drawing")
        with after_mutation():
            sheet = drawing.Sheets.ActiveSheet
            setup = self._sheet_setup_text(sheet, settings)
        return (
            f"Drawing created: '{drawing.Name}' (active sheet '{sheet.Name}', "
            f"{drawing.Sheets.Count} sheet(s)){setup}. Use catia_drawing_add_view "
            "to project a part onto it."
        )

    def _sheet_setup_text(self, sheet: Any, settings: list[tuple[str, Any, str, bool]]) -> str:
        if not settings:
            return ""
        report = self._apply_sheet_settings(sheet, settings)
        text = "; sheet set to " + ", ".join(
            f"{label} ({prop} = {report['read_back'].get(prop, '?')})"
            for prop, _, label, _ in settings
        )
        if report["warnings"]:
            text += ". Warning: " + " ".join(report["warnings"])
        return text

    # ── 3DEXPERIENCE editors ──

    def _editor_objects(self) -> list[tuple[Any, Any]]:
        rows = []
        for editor in self.plm.editors():
            obj = safe_get(editor, "ActiveObject")
            if obj is not None:
                rows.append((editor, obj))
        return rows

    def _find_source_3dx(self, part_name: str | None) -> tuple[Any, Any]:
        """3D object (Part or root occurrence) shown in another editor, with
        that editor."""
        candidates = [
            (editor, obj) for editor, obj in self._editor_objects()
            if self.conn.classify(obj) in ("part", "product")
        ]
        titles = [self.plm.title_of(obj) for _, obj in candidates]
        if part_name:
            matches = [c for c, t in zip(candidates, titles) if t == part_name] or [
                c for c, t in zip(candidates, titles) if part_name in t
            ] or [
                # A 3D Part edited at shape level shows its 3D Shape in the
                # editor (live R428); accept the enclosing product's title.
                c for c in candidates if self._product_title_3dx(*c) == part_name
            ]
            if len(matches) == 1:
                return matches[0]
            if not matches:
                raise ObjectNotFound(
                    f"No open 3D editor titled '{part_name}'. Open 3D content: "
                    f"{', '.join(titles) or 'none'}."
                )
            raise AmbiguousTarget(f"'{part_name}' matches several editors: {', '.join(titles)}.")
        if len(candidates) == 1:
            return candidates[0]
        raise AmbiguousTarget(
            "Pass 'part_name' (the title) to pick the 3D content to project. Open 3D "
            f"content: {', '.join(titles) or 'none'}."
        )

    def _product_title_3dx(self, editor: Any, obj: Any) -> str | None:
        """Title of the product enclosing a shape-level editor, if any."""
        if self.conn.classify(obj) != "part":
            return None
        try:
            return self.plm.title_of(self.conn.root_occurrence(editor))
        except Exception:
            return None

    def _projected_root_3dx(self, editor: Any, obj: Any, title: str) -> Any:
        """The product root occurrence a front view projects (iListofPrdInst).

        Generative views project 'an assembly definition containing instances
        of 3D shape representation' (DSYAutomation.chm R428). Live on R428
        SP6, DefineFrontView projected a VPMRootOccurrence and silently
        created nothing for a standalone 3D Shape (its Part or its
        VPMRepReference). A 3D Shape edited inside a product resolves to that
        product's root occurrence; a standalone one is refused untouched.
        """
        if resolves(obj, "ReferenceRootOccurrenceOf"):
            return obj
        try:
            return self.conn.root_occurrence(editor)
        except WrongDocumentType:
            raise UnsupportedCapability(
                f"'{title}' is a standalone 3D Shape. 3DEXPERIENCE generative views project "
                "product instances, and CATIA creates no view for a 3D Shape outside a "
                "product. Insert the shape into a Physical Product (or use a 3D Part), open "
                "it, and pass its title as part_name."
            ) from None

    def _find_part_document(self, part_name: str | None) -> Any:
        docs = self.conn.documents
        open_parts = []
        for i in range(1, docs.Count + 1):
            doc = docs.Item(i)
            try:
                doc_name = str(doc.Name)
            except Exception:
                continue
            if doc_name.lower().endswith(".catpart"):
                open_parts.append(doc)
                if part_name and doc_name == part_name:
                    return doc

        if part_name:
            names = ", ".join(str(d.Name) for d in open_parts) or "none"
            raise RuntimeError(
                f"No open Part named '{part_name}'. Open parts: {names}."
            )
        if len(open_parts) == 1:
            return open_parts[0]
        names = ", ".join(str(d.Name) for d in open_parts) or "none"
        raise RuntimeError(
            "Pass 'part_name' to pick the part to project. "
            f"Open parts: {names}."
        )

    def write_target(self, name: str, args: dict[str, Any]) -> Any | None:
        """PLM object a drafting call writes, for the server's write guard.

        Drafting tools may write a drawing that is not the active window (the
        only open drawing, or apply_template's target while the template is
        in front), so the guard must check that drawing, not the window in
        front. None lets the guard use the active editor."""
        if not self.conn.is_3dx or not name.startswith("catia_drawing_"):
            return None
        if name == "catia_drawing_apply_template":
            entries = self._drawing_entries()
            if args.get("target"):
                drawing = self._match_entry(entries, str(args["target"]), "target")["drawing"]
            else:
                template = self._match_entry(entries, str(args["template"]), "template")
                others = [e for e in entries if e is not template]
                try:
                    active = self._find_active_drawing()
                except CATIAToolError:
                    active = None
                chosen = next((e for e in others if e["drawing"] is active), None)
                if chosen is None and len(others) == 1:
                    chosen = others[0]
                if chosen is None:
                    return None  # the tool itself reports the ambiguity
                drawing = chosen["drawing"]
        else:
            drawing = self._find_active_drawing()
        return self.plm.plm_object_of(drawing)

    def _find_active_drawing(self) -> Any:
        if self.conn.is_3dx:
            try:
                active = self.conn.active_object()
                if self.conn.classify(active) == "drawing":
                    return active
            except Exception:
                pass
            drawings = [obj for _, obj in self._editor_objects() if self.conn.classify(obj) == "drawing"]
            if len(drawings) == 1:
                return drawings[0]
            raise ObjectNotFound(
                "No active Drawing editor. Run catia_new_drawing first, or activate "
                "the drawing's window with catia_activate_document."
            )
        app = self.conn.app
        try:
            active = app.ActiveDocument
            if str(active.Name).lower().endswith(".catdrawing"):
                return active
        except Exception:
            pass
        docs = self.conn.documents
        drawings = []
        for i in range(1, docs.Count + 1):
            doc = docs.Item(i)
            try:
                if str(doc.Name).lower().endswith(".catdrawing"):
                    drawings.append(doc)
            except Exception:
                continue
        if len(drawings) == 1:
            return drawings[0]
        raise RuntimeError(
            "No active Drawing document. Run catia_new_drawing first."
        )

    def _add_view(self, args: dict[str, Any]) -> str:
        self.conn.ensure_connected()
        if self.conn.is_3dx:
            return self._add_view_3dx(args)
        part_doc = self._find_part_document(args.get("part_name"))
        drawing = self._find_active_drawing()

        plane = (args.get("plane") or "xy").lower()
        if plane not in _PLANE_VECTORS:
            raise ValueError(f"Unknown plane '{plane}'. Use 'xy', 'yz', or 'zx'.")
        vx1, vy1, vz1, vx2, vy2, vz2 = _PLANE_VECTORS[plane]
        if args.get("face") and args.get("up_axis"):
            raise InvalidArgument("up_axis applies to 'plane' views; with 'face' pass 'angle'.")
        if args.get("face"):
            angle, orientation_notes = args.get("angle"), []
        else:
            angle, orientation_notes = _up_axis_angle(plane, args.get("up_axis"), args.get("angle"))

        # Resolve every input before touching the sheet, so a bad argument
        # never leaves an empty unconfigured view behind.
        body_name = args.get("body_name")
        target_body = None
        if body_name:
            bodies = part_doc.Part.Bodies
            try:
                target_body = bodies.Item(body_name)
            except Exception:
                names = ", ".join(str(bodies.Item(i).Name) for i in range(1, bodies.Count + 1))
                raise RuntimeError(
                    f"No body named '{body_name}' in '{part_doc.Name}'. "
                    f"Top-level bodies: {names or 'none'}."
                )
        face_name = args.get("face")
        face_plane = None
        if face_name:
            # Face enumeration and GetPlane run with the PART active, as in
            # the proven run; the drawing is re-activated afterwards.
            try:
                part_doc.Activate()
            except Exception:
                pass
            try:
                face_plane = self._face_plane(part_doc, body_name, face_name)
            finally:
                try:
                    drawing.Activate()
                except Exception:
                    pass

        sheet = self._active_sheet(drawing)
        view = sheet.Views.Add(args.get("name") or "Front View")

        gb = view.GenerativeBehavior
        if target_body is not None:
            # gb.Document accepts a Body and then draws that body only
            # (field-validated with a control view on V5R20).
            gb.Document = target_body
            source = f"{part_doc.Name} / {body_name}"
        else:
            gb.Document = part_doc
            source = str(part_doc.Name)
        if face_plane is not None:
            # SetProjectionPlane takes the two in-plane direction vectors
            # (normal = V1 x V2), field-corrected and proven on V5R20.
            gb.SetProjectionPlane(
                face_plane[3], face_plane[4], face_plane[5],
                face_plane[6], face_plane[7], face_plane[8],
            )
            projection = f"plane of {face_name}"
        else:
            gb.DefineFrontView(vx1, vy1, vz1, vx2, vy2, vz2)
            projection = f"{plane.upper()} projection"

        view.x = args.get("x", 300)
        view.y = args.get("y", 150)
        if angle:
            view.Angle = math.radians(angle)
        gb.Update()

        self.conn.refresh_display()
        return " ".join([
            (f"View '{view.Name}' of '{source}' added to sheet "
            f"'{sheet.Name}' ({projection}{self._angle_text(view, angle)}) and updated."),
            *orientation_notes,
        ])

    @staticmethod
    def _angle_text(view: Any, angle: float | None) -> str:
        """', rotated N deg' read back from DrawingView.Angle (radians)."""
        if not angle:
            return ""
        actual = _axis_number(view, "Angle")
        if actual is None:
            return f", rotation {angle:g} deg requested (Angle unreadable)"
        return f", rotated {math.degrees(actual):g} deg"

    # CatDimType is 0-based in R20 (enum_CatDimType, 21 values, verified
    # live: catDimRadius = 5): Distance=0, DistanceOffset=1, Length=2,
    # LengthCurvilinear=3, Angle=4, Radius=5, RadiusTangent=6,
    # RadiusCylinder=7, RadiusEdge=8, Diameter=9, ...
    _DIM_TYPES = {
        "distance": 0,
        "length": 2,
        "angle": 4,
        "radius": 5,
        "diameter": 9,
    }

    # Sheet views 1 and 2 are always the Main and Background views. Their
    # names differ by platform: V5 "Main View" / "Background View", while the
    # R428 SP6 smoke run listed them as "View.1" / "View.2"; these aliases
    # reach them by role on both.
    _VIEW_ALIASES: ClassVar[dict[str, int]] = {
        "main": 1,
        "main view": 1,
        "background": 2,
        "background view": 2,
    }

    @staticmethod
    def _view_role(index: int) -> str:
        return {1: "main", 2: "background"}.get(index, "user")

    def _find_view(self, view_name: str | None) -> Any:
        drawing = self._find_active_drawing()
        views = self._active_sheet(drawing).Views
        if view_name:
            for i in range(1, views.Count + 1):
                v = views.Item(i)
                if str(v.Name) == view_name:
                    return v
            alias = self._VIEW_ALIASES.get(" ".join(view_name.lower().split()))
            if alias is not None and views.Count >= alias:
                return views.Item(alias)
            names = ", ".join(str(views.Item(i).Name) for i in range(1, views.Count + 1))
            raise ObjectNotFound(
                f"No view named '{view_name}'. Views: {names}. 'main' and 'background' "
                "also reach the sheet's Main / Background view."
            )
        if views.Count < 3:
            raise ObjectNotFound(
                "The active sheet has no user view yet (only Main/Background). "
                "Add one with catia_drawing_add_view."
            )
        # Items 1 and 2 are the sheet's Main and Background views; the last
        # item is the most recently added user view.
        return views.Item(views.Count)

    def _list_view_geometry(self, args: dict[str, Any]) -> str:
        self.conn.ensure_connected()
        view = self._find_view(args.get("view_name"))
        geoms = view.GeometricElements
        elements = []
        for i in range(1, geoms.Count + 1):
            try:
                name = str(geoms.Item(i).Name)
            except Exception:
                name = "<unnamed>"
            elements.append({"index": i, "name": name})
        if not elements:
            return (
                f"View '{view.Name}' exposes no 2D geometry. For a "
                "generative view, make sure it was updated "
                "(catia_drawing_add_view updates automatically)."
            )
        import json as _json

        return _json.dumps(
            {"view": str(view.Name), "elements": elements}, indent=2
        )

    def _add_dimension(self, args: dict[str, Any]) -> str:
        self.conn.ensure_connected()
        dim_type = args["type"]
        code = self._DIM_TYPES.get(dim_type)
        if code is None:
            raise ValueError(
                f"Unknown dimension type '{dim_type}'. Use one of: "
                + ", ".join(self._DIM_TYPES)
            )
        view = self._find_view(args.get("view_name"))
        geoms = view.GeometricElements

        idx1 = args["element_index_1"]
        idx2 = args.get("element_index_2")
        two_element_types = ("distance", "angle")
        if dim_type in two_element_types and idx2 is None:
            raise ValueError(f"'{dim_type}' needs element_index_2 as well.")

        elems = [geoms.Item(idx1)]
        if idx2 is not None:
            elems.append(geoms.Item(idx2))

        # DrawingDimensions.Add(iTypeDim, iGeomElem, iPtCoordElem, iLineRep).
        # Input safearrays marshal fine through late-bound COM (only in/out
        # arrays do not). Field-proven form on V5R20: view activated first,
        # zeroed selection points, iLineRep = 3 (catDimAuto, CATIA places
        # the dimension line).
        try:
            view.Activate()
        except Exception:
            pass
        pt_coords = [0.0] * (2 * len(elems))
        dim = view.Dimensions.Add(code, elems, pt_coords, 3)

        self.conn.refresh_display()
        return (
            f"{dim_type.capitalize()} dimension '{dim.Name}' added on view "
            f"'{view.Name}' (associative value computed by CATIA)."
        )

    def _face_plane(self, part_doc: Any, body_name: str | None, face_name: str) -> list[float]:
        """Return the 9 GetPlane components (origin, dir1, dir2) of Face.N of
        the body's final shape, through the SystemService.Evaluate detour."""
        m = re.match(r"^Face\.(\d+)$", face_name or "")
        if not m:
            raise ValueError(f"'face' must look like 'Face.3' (from catia_list_faces), got '{face_name}'.")
        idx = int(m.group(1))
        part = part_doc.Part
        body = part.Bodies.Item(body_name) if body_name else part.MainBody
        if body.Shapes.Count == 0:
            raise RuntimeError("The body has no solid shape to take a face from.")
        sel = part_doc.Selection
        try:
            if idx < 1 or idx > search_final_topology(sel, body, "Face", is_3dx=self.conn.is_3dx):
                raise RuntimeError(
                    f"{face_name} is out of range: the final shape exposes "
                    f"{sel.Count} face(s)."
                )
            ref = sel.Item(idx).Reference
        finally:
            try:
                sel.Clear()
            except Exception:
                pass
        spa = part_doc.GetWorkbench("SPAWorkbench")
        measurable = spa.GetMeasurable(ref)
        try:
            plane = read_measurable_array(self.conn.app, measurable, "GetPlane", 9)
        except Exception as e:
            raise RuntimeError(
                f"{face_name} has no plane (GetPlane failed: {e}). Only "
                "planar faces can drive a projection; pick another face."
            )
        if not any(abs(v) > 0 for v in plane):
            raise RuntimeError(
                f"{face_name} returned no usable plane (not planar?). Pick a "
                "planar face."
            )
        return plane

    @staticmethod
    def _dimension_row(d: Any, index: int | None = None) -> dict[str, Any]:
        """One dimension as reported to the agent. Angles are reported in
        degrees: GetValue().Value answers in radians for an angle dimension
        (field report R428 SP6, 2026-11-02: a 120 deg drill point read 2.0944)."""
        row: dict[str, Any] = {}
        if index is not None:
            row["index"] = index
        row["name"] = str(safe_get(d, "Name", "<unnamed>"))
        dim_type = None
        try:
            dim_type = int(d.DimType)
        except Exception:
            pass
        try:
            raw = float(d.GetValue().Value)
        except Exception:
            raw = None
        kind = _dim_kind(dim_type)
        if raw is None:
            row["value"] = None
        elif kind == "angle":
            # A value above a full turn cannot be radians: some release
            # already answered degrees, keep it.
            degrees = raw if abs(raw) > 2 * math.pi + 1e-6 else math.degrees(raw)
            row["value"] = round(degrees, 4)
            row["unit"] = "deg"
        else:
            row["value"] = round(raw, 4)
            row["unit"] = "mm"
        if dim_type is not None:
            row["dim_type"] = dim_type
            row["kind"] = kind
        try:
            # 6 = catBasic (manual 2D parents), 7 = cat3DDrivableDim
            # (generated from a 3D constraint, follows the part).
            status = int(d.DimStatus)
            row["status"] = {6: "basic_2d", 7: "3d_driven"}.get(status, status)
        except Exception:
            pass
        return row

    def _dimension_rows(self, view: Any) -> list[dict[str, Any]]:
        dims = view.Dimensions
        return [self._dimension_row(dims.Item(j), j) for j in range(1, dims.Count + 1)]

    def _sheets(self, drawing: Any) -> list[Any]:
        """Every sheet of the drawing; the active one when the collection
        cannot be walked."""
        try:
            sheets = list(iter_items(drawing.Sheets))
            if sheets:
                return sheets
        except Exception:
            pass
        return [self._active_sheet(drawing)]

    def _dimension_counts(self, drawing: Any) -> dict[tuple[int, int], int]:
        """Dimension count of every view of every sheet, keyed (sheet, view)
        by 1-based index."""
        counts: dict[tuple[int, int], int] = {}
        for s, sheet in enumerate(self._sheets(drawing), start=1):
            for v, view in enumerate(iter_items(sheet.Views), start=1):
                try:
                    counts[(s, v)] = int(view.Dimensions.Count)
                except Exception:
                    counts[(s, v)] = 0
        return counts

    def _drawing_selection(self, drawing: Any) -> Any:
        """The Selection that can hold the drawing's objects: the V5
        DrawingDocument's, or on 3DEXPERIENCE the Selection of the editor
        showing the DrawingRoot."""
        if not self.conn.is_3dx:
            return drawing.Selection
        for editor, obj in self._editor_objects():
            try:
                same = obj is drawing or obj == drawing
            except Exception:
                same = False
            if same:
                return editor.Selection
        return self.conn.active_editor.Selection

    def _delete_objects(self, drawing: Any, objects: list[Any]) -> None:
        """Selection.Add each object, then Selection.Delete (field-proven on
        R428 SP6 for dimensions, 2026-11-02 field report)."""
        if not objects:
            return
        sel = self._drawing_selection(drawing)
        try:
            sel.Clear()
            for obj in objects:
                sel.Add(obj)
            sel.Delete()
        finally:
            try:
                sel.Clear()
            except Exception:
                pass

    @staticmethod
    def _tap_drill_of(row: dict[str, Any], view_rows: list[dict[str, Any]]) -> str | None:
        """'M<D>x<P>' when `row` is the ISO tap drill of another diameter of
        the view: d = D - 1.082532 * P (ISO basic minor diameter, what CATIA
        sets as tap drill) for an ISO 261 pitch P of M<D>."""
        value = row.get("value")
        if row.get("kind") != "diameter" or value is None:
            return None
        for other in view_rows:
            nominal = other.get("value")
            if other is row or other.get("kind") != "diameter" or nominal is None:
                continue
            for pitch in _ISO_PITCHES.get(round(nominal, 3), ()):
                if abs(nominal - 1.082532 * pitch - value) <= 0.01:
                    return f"M{nominal:g}x{pitch:g}"
        return None

    @classmethod
    def _view_shows_hole(cls, view_rows: list[dict[str, Any]]) -> bool:
        """True when a view's dimensions carry a hole signature: an ISO
        tap-drill pair, or a drill-point angle next to a diameter. The
        angle and duplicate rules only apply in such a view, so equal
        dimensions of a plain prism (a 100 x 100 block's two 100s) and a
        lone 120 deg angle of a hexagon are kept."""
        has_diameter = any(r.get("kind") == "diameter" for r in view_rows)
        for r in view_rows:
            value = r.get("value")
            if value is None:
                continue
            if cls._tap_drill_of(r, view_rows):
                return True
            if (
                has_diameter and r.get("kind") == "angle"
                and any(abs(value - a) <= 0.5 for a in _DRILL_POINT_ANGLES)
            ):
                return True
        return False

    @classmethod
    def _hole_internal_reason(
        cls, row: dict[str, Any], view_rows: list[dict[str, Any]], seen: dict[tuple[int, float], str],
        *, hole_view: bool = True,
    ) -> str | None:
        """Why a generated dimension is a hole internal to drop, or None.

        Conservative detection from what DrawingDimension exposes (DimType,
        GetValue().Value). Unverified beyond the field report's case (M6
        tapped hole on R428 SP6: 120 deg drill point, tap drill 4.917,
        three 60 mm depths):
        - tap-drill diameter: see _tap_drill_of;
        - drill-point angle: an angle dimension of 118 or 120 deg (+-0.5),
          only in a view showing a hole (hole_view, see _view_shows_hole);
        - duplicate: same DimType and value as an earlier dimension of the
          same view (the first one is kept), only in a view showing a hole.
        """
        value, dim_type, kind = row.get("value"), row.get("dim_type"), row.get("kind")
        if value is None or dim_type is None:
            return None
        tap = cls._tap_drill_of(row, view_rows)
        if tap:
            return f"tap-drill diameter of {tap}"
        if not hole_view:
            return None
        if kind == "angle" and any(abs(value - a) <= 0.5 for a in _DRILL_POINT_ANGLES):
            return f"drill-point angle ({value:g} deg)"
        key = (dim_type, round(value, 3))
        if key in seen:
            return f"duplicate of {seen[key]}"
        seen[key] = row["name"]
        return None

    def _generate_dimensions(self, args: dict[str, Any]) -> str:
        self.conn.ensure_connected()
        drawing = self._find_active_drawing()
        requested_view = args.get("view_name")
        view = self._find_view(requested_view)
        skip_internals = args.get("skip_hole_internals", True)

        # Field-proven on V5R20: DrawingSheet.GenerateDimensions() is a
        # silent no-op unless the DRAWING is the active document and the
        # view is active. One dimension per supported 3D constraint
        # (distance/length/angle/radius/diameter), idempotent on re-run.
        app = self.conn.app
        try:
            drawing.Activate()
        except Exception:
            pass
        try:
            view.Activate()
        except Exception:
            pass
        sheet = self._active_sheet(drawing)
        if self.conn.is_3dx:
            if self.conn.classify(self.conn.active_object()) != "drawing":
                raise RuntimeError(
                    "The drawing is not the active editor; GenerateDimensions would "
                    "silently do nothing. Activate the drawing window with "
                    "catia_activate_document and retry."
                )
        else:
            active_doc = str(app.ActiveDocument.Name)
            if active_doc != str(drawing.Name):
                raise RuntimeError(
                    f"Could not activate drawing '{drawing.Name}' (active "
                    f"document is '{active_doc}'); GenerateDimensions would "
                    "silently do nothing. Bring the drawing window to front and "
                    "retry."
                )
        try:
            active_view = str(sheet.Views.ActiveView.Name)
        except Exception:
            active_view = None
        if active_view is not None and active_view != str(view.Name):
            raise RuntimeError(
                f"Could not activate view '{view.Name}' (active view is "
                f"'{active_view}'); retry or pass view_name explicitly."
            )
        # GenerateDimensions runs sheet-wide (field report R428 SP6,
        # 2026-11-02: asked for Top View, 7 more landed in Section A-A), so
        # every view of every sheet is snapshotted before and after. CATIA
        # appends new dimensions to a view's collection: items past the
        # previous count are the generated ones.
        before = self._dimension_counts(drawing)
        sheet.GenerateDimensions()

        with after_mutation():
            target_key = None
            new: list[dict[str, Any]] = []  # {"view", "dim", "row", "rows"}
            for s, sh in enumerate(self._sheets(drawing), start=1):
                sheet_name = str(safe_get(sh, "Name", f"Sheet.{s}"))
                for v, vw in enumerate(iter_items(sh.Views), start=1):
                    # COM hands out a new wrapper per read: match by names.
                    if vw is view or (
                        sheet_name == str(safe_get(sheet, "Name", ""))
                        and str(safe_get(vw, "Name", "")) == str(view.Name)
                    ):
                        target_key = (s, v)
                    try:
                        dims = vw.Dimensions
                        count = int(dims.Count)
                    except Exception:
                        continue
                    first = before.get((s, v), 0) + 1
                    if count < first:
                        continue
                    rows = [self._dimension_row(dims.Item(j), j) for j in range(1, count + 1)]
                    for j in range(first, count + 1):
                        new.append({
                            "key": (s, v),
                            "view": str(safe_get(vw, "Name", f"View.{v}")),
                            "sheet": sheet_name,
                            "dim": dims.Item(j),
                            "row": rows[j - 1],
                            "rows": rows,
                            "first_new": first,
                        })

            removals: list[tuple[dict[str, Any], str]] = []
            if requested_view and target_key is not None:
                for entry in new:
                    if entry["key"] != target_key:
                        removals.append((entry, f"outside requested view '{view.Name}'"))
            if skip_internals:
                seen_by_view: dict[tuple[int, int], dict[tuple[int, float], str]] = {}
                removed_ids = {id(e) for e, _ in removals}
                for entry in new:
                    if id(entry) in removed_ids:
                        continue
                    key = entry["key"]
                    if key not in seen_by_view:
                        seen: dict[tuple[int, float], str] = {}
                        # Dimensions that existed before the call count as
                        # earlier ones for duplicate detection.
                        for old in entry["rows"][: entry["first_new"] - 1]:
                            if old.get("value") is not None and old.get("dim_type") is not None:
                                seen.setdefault((old["dim_type"], round(old["value"], 3)), old["name"])
                        seen_by_view[key] = seen
                    kept_rows = [
                        e["row"] for e in new if e["key"] == key and id(e) not in removed_ids
                    ] + entry["rows"][: entry["first_new"] - 1]
                    reason = self._hole_internal_reason(
                        entry["row"], kept_rows, seen_by_view[key],
                        hole_view=self._view_shows_hole(entry["rows"]),
                    )
                    if reason:
                        removals.append((entry, f"hole internal: {reason}"))
                        removed_ids.add(id(entry))

            delete_error = None
            if removals:
                try:
                    self._delete_objects(drawing, [e["dim"] for e, _ in removals])
                except Exception as e:
                    delete_error = str(e)
            self.conn.refresh_display()
            after = self._dimension_counts(drawing)

        # A failed delete leaves everything on the sheet: report it all as kept.
        removed_ids = set() if delete_error else {id(e) for e, _ in removals}

        def report(entry: dict[str, Any]) -> dict[str, Any]:
            row = {k: v for k, v in entry["row"].items() if k != "index"}
            return {"sheet": entry["sheet"], "view": entry["view"], **row}

        result: dict[str, Any] = {
            "view": str(view.Name),
            "generated": len(new),
            "kept": [report(e) for e in new if id(e) not in removed_ids],
            "deleted": [{**report(e), "reason": why} for e, why in removals] if not delete_error else [],
            "dimensions": self._dimension_rows(view),
        }
        notes = []
        if not requested_view and any(e["key"] != target_key for e in new):
            notes.append(
                "GenerateDimensions runs sheet-wide: dimensions landed in other views "
                "(listed with their view). Pass view_name to keep only that view's."
            )
        if delete_error:
            notes.append(
                f"Deleting {len(removals)} unwanted dimension(s) failed ({delete_error}); "
                "they are still on the sheet. Remove them with catia_drawing_delete_dimension."
            )
        else:
            expected = sum(before.values()) + len(new) - len(removals)
            if removals and sum(after.values()) != expected:
                notes.append(
                    f"Expected {expected} dimension(s) on the sheet(s) after cleanup, found "
                    f"{sum(after.values())}. Check with catia_drawing_list_dimensions."
                )
        if requested_view and target_key is None and new:
            notes.append(
                f"View '{view.Name}' was not found again after generation, so dimensions "
                "in other views were NOT removed (listed with their view)."
            )
        if skip_internals:
            notes.append(
                "skip_hole_internals dropped ISO tap-drill diameters and, in views showing "
                "a hole, drill-point angles (118/120 deg) and duplicates (same type and "
                "value); pass skip_hole_internals=false to keep them."
            )
        if not new:
            notes.append(
                "No dimension was generated. Causes: the part's sketches carry no "
                "supported constraints (use catia_sketch_constraint: distance, "
                "length/radius, angle), or they were already generated (idempotent)."
            )
        else:
            notes.append(
                "Generated dimensions sit where CATIA puts them (often on model edges): "
                "move them with catia_drawing_move_dimension."
            )
        # Field report 2026-11-03 (R428 SP6), issues 1 and 8.
        notes.append(
            "Only 3D constraints become dimensions here; each lands in the view parallel to "
            "its constraint. Edges of generated views are not addressable by automation "
            "(R428 SP6): dimensions picked on model edges must be placed in the CATIA UI, "
            "then formatted with catia_drawing_set_dimension_frame / "
            "catia_drawing_set_dimension_text."
        )
        result["notes"] = notes
        return json.dumps(result, indent=2, ensure_ascii=False)

    def _list_dimensions(self, args: dict[str, Any]) -> str:
        self.conn.ensure_connected()
        view = self._find_view(args.get("view_name"))
        # 3d_driven dimensions only follow the part after the generative
        # view is regenerated (field-proven: part.Update alone leaves the
        # old value). Refresh before reading.
        if args.get("refresh", True):
            try:
                view.GenerativeBehavior.Update()
            except Exception:
                pass
        return json.dumps(
            {"view": str(view.Name), "dimensions": self._dimension_rows(view)},
            indent=2,
        )

    def _find_view_any(self, view_name: str | None) -> Any:
        """Resolve a view for annotations: by name (Main/Background views
        included, also by the aliases 'main' / 'background'), else the last
        user view, else the Background View so title blocks can be filled on
        an otherwise empty sheet."""
        if view_name:
            return self._find_view(view_name)
        views = self._active_sheet(self._find_active_drawing()).Views
        if views.Count >= 3:
            return views.Item(views.Count)
        return views.Item(2) if views.Count >= 2 else views.Item(1)

    def _parent_generative_view(self, parent_name: str | None) -> Any:
        parent = self._find_view(parent_name)
        try:
            generative = bool(parent.IsGenerative())
        except Exception:
            generative = True  # cannot tell; let CATIA decide
        if not generative:
            raise ValueError(
                f"'{parent.Name}' is not a generative view; derive from a "
                "view created by catia_drawing_add_view."
            )
        return parent

    def _create_derived_view(self, sheet: Any, parent: Any, name: str, define: Any) -> Any:
        """Add a view, link it to the parent's 3D source, run the Define*
        call; remove the view again if anything fails so no empty orphan
        is left on the sheet (and becomes the next default parent)."""
        view = sheet.Views.Add(name)
        try:
            linked = False
            try:
                view.GenerativeBehavior.Document = parent.GenerativeBehavior.Document
                linked = True
            except Exception:
                try:
                    parent.GenerativeLinks.CopyLinksTo(view.GenerativeLinks)
                    linked = True
                except Exception:
                    pass
            if not linked:
                raise RuntimeError(
                    "Could not carry the parent's 3D link to the new view; "
                    "it would generate no geometry."
                )
            define(view)
            return view
        except Exception:
            try:
                sheet.Views.Remove(view.Name)
            except Exception:
                pass
            raise

    @staticmethod
    def _parent_placement(parent: Any) -> tuple[float, float, float]:
        try:
            return float(parent.x), float(parent.y), float(parent.Scale)
        except Exception:
            return 0.0, 0.0, 1.0

    @staticmethod
    def _place(view: Any, x: float, y: float, scale: float | None = None) -> float | None:
        applied = None
        if scale:
            try:
                view.Scale = scale
                applied = float(view.Scale)
            except Exception:
                applied = None
        view.x = x
        view.y = y
        return applied

    def _projection_view(self, args: dict[str, Any]) -> str:
        self.conn.ensure_connected()
        direction = str(args["direction"]).lower()
        if direction not in _PROJECTION_TYPES:
            raise ValueError(f"Unknown direction '{direction}'. Use right, left, top, bottom or rear.")
        gap = float(args.get("gap", 100.0))
        drawing = self._find_active_drawing()
        sheet = self._active_sheet(drawing)
        parent = self._parent_generative_view(args.get("parent_view"))

        # Placement follows the sheet's projection method: third angle puts
        # the right view to the right of the front view, first angle (the
        # ISO default) puts it to the left. CatSheetProjectionMethod:
        # 0 = first angle, 1 = third angle.
        try:
            first_angle = int(sheet.ProjectionMethod) == 0
            method_known = True
        except Exception:
            first_angle = False
            method_known = False
        sign = -1.0 if first_angle else 1.0
        offsets = {
            "right": (sign * gap, 0.0), "left": (-sign * gap, 0.0),
            "top": (0.0, sign * gap), "bottom": (0.0, -sign * gap),
            "rear": (2 * sign * gap, 0.0),
        }
        dx, dy = offsets[direction]
        px, py, pscale = self._parent_placement(parent)

        if self._uses_factory(parent):
            view = self._factory_view(
                sheet, "DefineProjectionView",
                {"iParentView": parent, "iType": _PROJECTION_TYPES[direction]},
                args.get("name") or f"{direction} view",
                args.get("x", px + dx), args.get("y", py + dy), pscale,
            )
        else:
            def define(view: Any) -> None:
                view.GenerativeBehavior.DefineProjectionView(
                    parent.GenerativeBehavior, _PROJECTION_TYPES[direction]
                )

            view = self._create_derived_view(sheet, parent, args.get("name") or f"{direction} view", define)
            self._place(view, args.get("x", px + dx), args.get("y", py + dy), pscale)
            view.GenerativeBehavior.Update()
        self.conn.refresh_display()
        if not method_known:
            method = "projection method unreadable, third angle assumed"
        else:
            method = "first angle" if first_angle else "third angle"
        message = (
            f"Projection view '{view.Name}' ({direction}) of '{parent.Name}' "
            f"added to sheet '{sheet.Name}' ({method} layout) and updated."
        )
        if first_angle:
            # Field report R428 SP6, 2026-11-02: the default first-angle sheet
            # put the 'bottom' view above its parent, which looked wrong.
            where = {
                "right": "left of", "left": "right of", "top": "below",
                "bottom": "above", "rear": "left of",
            }[direction]
            message += (
                f" The sheet uses FIRST-ANGLE projection (ISO), so the {direction} view "
                f"is placed {where} the parent. For third-angle (US/ASME) layout run "
                "catia_drawing_set_sheet projection='third_angle' before adding views."
            )
        return message

    def _section_view(self, args: dict[str, Any]) -> str:
        self.conn.ensure_connected()
        profile = [float(v) for v in args["profile"]]
        if len(profile) < 4 or len(profile) % 2:
            raise ValueError("profile must be a flat list [x1, y1, x2, y2, ...] with at least two points")
        section_type = args.get("section_type") or "SectionView"
        profile_type = args.get("profile_type") or "Offset"
        if section_type not in ("SectionView", "SectionCut"):
            raise ValueError("section_type must be 'SectionView' or 'SectionCut'")
        if profile_type not in ("Offset", "Aligned"):
            raise ValueError("profile_type must be 'Offset' or 'Aligned'")
        side = int(args.get("side", 1))
        if side not in (0, 1):
            raise ValueError("side must be 0 (clockwise) or 1 (counterclockwise)")
        drawing = self._find_active_drawing()
        sheet = self._active_sheet(drawing)
        parent = self._parent_generative_view(args.get("parent_view"))
        px, py, pscale = self._parent_placement(parent)

        if self._uses_factory(parent):
            view = self._factory_view(
                sheet, "DefineSectionView",
                {
                    "iProfile": profile,
                    "iSectionType": section_type,
                    "iProfileType": _FACTORY_PROFILE_TYPES[profile_type],
                    "iSideToDraw": side,
                    "iParentView": parent,
                },
                args.get("name") or "section view",
                args.get("x", px + 150.0), args.get("y", py), pscale,
            )
        else:
            def define(view: Any) -> None:
                # DefineSectionView(iProfile, iSectionType, iProfileType,
                # iSideToDraw, iParentGB); the profile is an input safearray.
                view.GenerativeBehavior.DefineSectionView(
                    profile, section_type, profile_type, side, parent.GenerativeBehavior
                )

            view = self._create_derived_view(sheet, parent, args.get("name") or "section view", define)
            self._place(view, args.get("x", px + 150.0), args.get("y", py), pscale)
            view.GenerativeBehavior.Update()
        with after_mutation():
            name_note = self._strip_trailing_dash(view, self.conn.app)
        self.conn.refresh_display()
        return (
            f"{section_type} '{view.Name}' from '{parent.Name}' added to sheet "
            f"'{sheet.Name}' ({profile_type}, {len(profile) // 2} profile points) and updated."
            f"{name_note}"
        )

    @staticmethod
    def _strip_trailing_dash(view: Any, app: Any = None) -> str:
        """Field reports 2026-11-02 and 2026-11-03 (R428 SP6): CATIA names a
        section view 'Section A-A-', and the earlier fallback did not remove
        the dash live. Non-fatal, in order, reading the name back after each:

        1. DrawingView.Name = name without the dash;
        2. GetViewName(prefix, ident, suffix) (BSTR out-parameters through
           SystemService.Evaluate), then SetViewName with the suffix's dash
           dropped, or with an empty suffix;
        3. view.Update() and a last read, in case CATIA recomposes the name.

        The ident is never touched: it is the letter shared with the section
        callout in the parent view. None of these steps is verified on R428;
        the parts GetViewName returns are reported so a live run shows where
        the dash lives."""
        name = str(safe_get(view, "Name", ""))
        if not name.endswith("-") or not name.rstrip("-").strip():
            return ""
        wanted = name.rstrip("-").rstrip()

        def current() -> str:
            return str(safe_get(view, "Name", ""))

        tried = []
        try:
            view.Name = wanted
            tried.append("Name")
        except Exception:
            tried.append("Name (refused)")
        parts: tuple[str, str, str] | None = None
        if current() != wanted and app is not None:
            try:
                prefix, ident, suffix = (str(v or "") for v in call_out_values(app, view, "GetViewName", 3))
                parts = (prefix, ident, suffix)
            except Exception:
                parts = None
            if parts is not None:
                prefix, ident, suffix = parts
                stripped = suffix.rstrip().rstrip("-").rstrip()
                for candidate in dict.fromkeys((stripped, "")):
                    if candidate == suffix:
                        continue
                    try:
                        view.SetViewName(prefix, ident, candidate)
                        tried.append(f"SetViewName(suffix={candidate!r})")
                    except Exception:
                        tried.append(f"SetViewName(suffix={candidate!r}) (refused)")
                    if current() == wanted:
                        break
        if current() == wanted:
            try:
                view.Update()
            except Exception:
                pass
        actual = current()
        if actual == wanted:
            return f" (trailing '-' removed from CATIA's name '{name}')"
        detail = (
            f" GetViewName reads prefix={parts[0]!r}, ident={parts[1]!r}, suffix={parts[2]!r}."
            if parts is not None else " GetViewName could not be read."
        )
        return (
            f" (CATIA named it '{actual}' and kept the trailing '-' after {', '.join(tried)};"
            f"{detail} Rename it in the UI.)"
        )

    def _detail_view(self, args: dict[str, Any]) -> str:
        self.conn.ensure_connected()
        cx, cy, radius = float(args["center_x"]), float(args["center_y"]), float(args["radius"])
        if radius <= 0:
            raise ValueError("radius must be positive")
        factor = float(args.get("scale", 2.0))
        drawing = self._find_active_drawing()
        sheet = self._active_sheet(drawing)
        parent = self._parent_generative_view(args.get("parent_view"))
        px, py, pscale = self._parent_placement(parent)

        if self._uses_factory(parent):
            view = self._factory_view(
                sheet, "DefineCircularDetailView",
                {"iXCenter": cx, "iYCenter": cy, "iRadius": radius, "iParentView": parent},
                args.get("name") or "detail view",
                args.get("x", px + 150.0), args.get("y", py + 100.0), factor * pscale,
            )
            try:
                applied = float(view.Scale)
            except Exception:
                applied = None
        else:
            def define(view: Any) -> None:
                view.GenerativeBehavior.DefineCircularDetailView(cx, cy, radius, parent.GenerativeBehavior)

            view = self._create_derived_view(sheet, parent, args.get("name") or "detail view", define)
            applied = self._place(view, args.get("x", px + 150.0), args.get("y", py + 100.0), factor * pscale)
            view.GenerativeBehavior.Update()
        self.conn.refresh_display()
        scale_note = f"scale {applied:g}" if applied else "scale unchanged (CATIA kept the parent scale)"
        return (
            f"Detail view '{view.Name}' of '{parent.Name}' (r={radius} mm at "
            f"({cx}, {cy}), {scale_note}) added."
        )

    # ── 3DEXPERIENCE generative view factory ──

    def _uses_factory(self, view: Any) -> bool:
        """3DEXPERIENCE releases without DrawingViewGenerativeBehavior create
        generative views through Views.DrawingDefineGenView."""
        return self.conn.is_3dx and not name_exists(view, "GenerativeBehavior")

    def _bind_factory_call(self, factory: Any, method: str, values: dict[str, Any]) -> Any:
        """Call factory.method with arguments bound by parameter name (case
        insensitive), in signature order, from the installation's type
        information. Refuses with the discovered signature, before calling,
        unless every required parameter has a value."""
        params = method_params(factory, method)
        if params is None:
            raise UnsupportedCapability(
                f"DrawingDefineGenView.{method} is not exposed (or has no type information) "
                "on this installation. Inspect it with catia_inspect_api path="
                "'document.ActiveObject.ActiveSheet.Views.DrawingDefineGenView'."
            )
        signature = f"{method}(" + ", ".join(
            f"{p['name']}: {p['type']}{' out' if p['out'] else ''}" for p in params
        ) + ")"
        inputs = [p for p in params if not p["out"]]
        outputs = [p for p in params if p["out"]]
        if len(outputs) > 1 or (outputs and params[-1] is not outputs[0]):
            raise UnsupportedCapability(f"Unsupported factory signature {signature}.")
        known = {name.lower(): value for name, value in values.items()}
        args: list[Any] = []
        for param in inputs:
            key = param["name"].lower()
            if key not in known:
                if param["optional"]:
                    break
                raise UnsupportedCapability(
                    f"Cannot bind parameter '{param['name']}' ({param['type']}) of {signature}: "
                    "the signature differs from what this tool knows how to fill. Create "
                    "the view in the UI, or script it with catia_run_script using this "
                    "signature."
                )
            value = known[key]
            args.append(value.resolve() if isinstance(value, _Lazy) else value)
        if outputs:
            return call_out_object(self.conn.app, factory, method, *args)
        return getattr(factory, method)(*args)

    def _view_properties(self) -> Any:
        """iViewProp: the generative view properties of the drawing settings.
        DSYAutomation.chm names the service CATDrawingGenService; its examples
        use CATDrawingService."""
        errors = []
        for service in ("CATDrawingGenService", "CATDrawingService"):
            try:
                return self.conn.session_service(service).DrawingGenViewProp
            except Exception as e:
                errors.append(f"{service}: {e}")
        raise UnsupportedCapability(
            "No generative view properties (DrawingGenViewProp) on this installation: "
            + "; ".join(errors)
        )

    def _factory_common(self, x: float, y: float) -> dict[str, Any]:
        """Anchor and trailing arguments shared by every Define* call."""
        return {
            "iXPos": float(x),
            "iYpos": float(y),
            "iViewStyle": "",
            "iComputeUpdate": True,
            "iViewProp": _Lazy(self._view_properties),
        }

    def _factory_view(
        self,
        sheet: Any,
        method: str,
        values: dict[str, Any],
        name: str,
        x: float,
        y: float,
        scale: float | None = None,
    ) -> Any:
        views = sheet.Views
        before = int(views.Count)
        factory = views.DrawingDefineGenView
        view = self._bind_factory_call(factory, method, {**self._factory_common(x, y), **values})
        if view is None or not name_exists(view, "Name"):
            if int(views.Count) <= before:
                # Live R428: CATIA returns nothing, without an error, when it
                # cannot project the inputs. The sheet is verifiably unchanged.
                raise CATIAToolError(
                    f"DrawingDefineGenView.{method} returned no view and the sheet gained "
                    "none: CATIA silently rejected the inputs.",
                    code="FEATURE_NO_EFFECT",
                )
            view = views.Item(views.Count)
        try:
            view.Name = name
        except Exception:
            pass
        self._place(view, x, y, scale)
        for update in (
            lambda: view.Update(),
            lambda: self._find_active_drawing().Update(),
        ):
            try:
                update()
                break
            except Exception:
                continue
        return view

    def _add_view_3dx(self, args: dict[str, Any]) -> str:
        if args.get("face") or args.get("body_name"):
            raise UnsupportedCapability(
                "Face-driven and single-body views use V5-only APIs (SPAWorkbench "
                "GetPlane, GenerativeBehavior.Document = Body). On 3DEXPERIENCE pass "
                "'plane' instead."
            )
        plane = (args.get("plane") or "xy").lower()
        if plane not in _PLANE_VECTORS:
            raise ValueError(f"Unknown plane '{plane}'. Use 'xy', 'yz', or 'zx'.")
        vectors = [float(v) for v in _PLANE_VECTORS[plane]]
        angle, orientation_notes = _up_axis_angle(plane, args.get("up_axis"), args.get("angle"))
        editor, source = self._find_source_3dx(args.get("part_name"))
        source_title = self.plm.title_of(source)
        drawing = self._find_active_drawing()
        sheet = self._active_sheet(drawing)
        views = sheet.Views
        name = args.get("name") or "Front View"
        x, y = args.get("x", 300), args.get("y", 150)

        if views.Count >= 1 and name_exists(views.Item(1), "GenerativeBehavior"):
            route = "GenerativeBehavior"
            view = views.Add(name)
            try:
                gb = view.GenerativeBehavior
                try:
                    gb.Document = source
                except Exception:
                    gb.Document = self.plm.plm_object_of(source)
                gb.DefineFrontView(*vectors)
                view.x = x
                view.y = y
                gb.Update()
            except Exception:
                try:
                    views.Remove(view.Name)
                except Exception:
                    pass
                raise
        else:
            route = "DrawingDefineGenView"
            root = self._projected_root_3dx(editor, source, source_title)
            source_title = self.plm.title_of(root)  # what is projected: the product
            view = self._factory_view(
                sheet, "DefineFrontView",
                {"iListofPrdInst": [root], "iPlane": vectors},
                name, x, y,
            )
        if angle:
            # view.Angle = radians: angle=90 on a ZX view is field-proven on
            # R428 SP6 (field report 2026-11-03); read back below.
            try:
                view.Angle = math.radians(angle)
            except Exception as e:
                orientation_notes.append(f"Setting the view angle failed ({e}): rotate it in the UI.")
        self.conn.refresh_display()
        return " ".join([
            (f"View '{safe_get(view, 'Name', name)}' of '{source_title}' added to sheet "
            f"'{sheet.Name}' ({plane.upper()} projection, {route}{self._angle_text(view, angle)})."),
            *orientation_notes,
        ])

    def _list_views(self) -> str:
        self.conn.ensure_connected()
        drawing = self._find_active_drawing()
        try:
            active_name = str(self._active_sheet(drawing).Name)
        except Exception:
            active_name = None
        sheets = []
        for sheet in iter_items(drawing.Sheets):
            views = []
            for i, view in enumerate(iter_items(sheet.Views), start=1):
                row: dict[str, Any] = {
                    "index": i,
                    "name": str(safe_get(view, "Name", "<unnamed>")),
                    "role": self._view_role(i),
                }
                try:
                    row["x"] = round(float(view.x), 2)
                    row["y"] = round(float(view.y), 2)
                except Exception:
                    pass
                try:
                    row["scale"] = float(view.Scale)
                except Exception:
                    pass
                try:
                    row["generative"] = bool(view.IsGenerative())
                except Exception:
                    pass
                views.append(row)
            name = str(safe_get(sheet, "Name", "<sheet>"))
            sheets.append({"name": name, "active": name == active_name, "views": views})
        return json.dumps({"sheets": sheets}, indent=2, ensure_ascii=False)

    @staticmethod
    def _view_position(
        view: Any, mode: str, x: float | None, y: float | None
    ) -> tuple[float | None, float | None, str, list[str]]:
        """(view x, view y, description, notes) for annotation placement."""
        if mode == "view":
            return x, y, "view coordinates", []
        vx, vy, notes = _sheet_to_view(view, x, y)
        given = ", ".join(f"{v:g}" for v in (x, y) if v is not None)
        shown = ", ".join(f"{v:.3f}" for v in (vx, vy) if v is not None)
        return vx, vy, f"view coordinates ({shown}) from sheet ({given}) mm", notes

    def _add_text(self, args: dict[str, Any]) -> str:
        self.conn.ensure_connected()
        mode = _coords_mode(args)
        x = self._number(args, "x", required=True)
        y = self._number(args, "y", required=True)
        view = self._find_view_any(args.get("view_name"))
        vx, vy, where, notes = self._view_position(view, mode, x, y)
        text = view.Texts.Add(str(args["text"]), vx, vy)
        self.conn.refresh_display()
        message = f"Text '{text.Name}' added to view '{view.Name}' at ({vx:g}, {vy:g}) mm ({where})."
        return " ".join([message, *notes])

    def _add_table(self, args: dict[str, Any]) -> str:
        self.conn.ensure_connected()
        mode = _coords_mode(args)
        x = self._number(args, "x", required=True)
        y = self._number(args, "y", required=True)
        view = self._find_view_any(args.get("view_name"))
        rows, cols = int(args["rows"]), int(args["columns"])
        if rows < 1 or cols < 1:
            raise ValueError("rows and columns must be at least 1")
        vx, vy, where, coord_notes = self._view_position(view, mode, x, y)
        table = view.Tables.Add(
            vx, vy, rows, cols,
            float(args.get("row_height", 8.0)), float(args.get("column_width", 40.0)),
        )
        widths = args.get("column_widths") or []
        for c, w in enumerate(widths[:cols], start=1):
            table.SetColumnSize(c, float(w))
        filled = 0
        cells = args.get("cells") or []
        for r, row in enumerate(cells[:rows], start=1):
            for c, value in enumerate(list(row)[:cols], start=1):
                if value is None or value == "":
                    continue
                table.SetCellString(r, c, str(value))
                filled += 1
        self.conn.refresh_display()
        return " ".join([
            (f"Table '{table.Name}' ({rows}x{cols}) added to view '{view.Name}' "
            f"at ({vx:g}, {vy:g}) mm ({where}), {filled} cell(s) filled."),
            *coord_notes,
        ])

    # ── Layout, title block and template tools ──
    #
    # Field-proven on R428 SP6 (2026-11-02 field report, "Workarounds via
    # catia_run_python"): Sheet.PaperSize / Orientation / ProjectionMethod,
    # DrawingView.x / .y, DrawingDimension.MoveValue(x, y, 0, 0),
    # Selection.Add + Selection.Delete, GetValue().SetPSText / SetBaultText,
    # DrawingTable.SetCellString, DrawingText.SetFontSize(0, 0, size) / .x /
    # .y / .Text, GetCellObject(r, c).InsertAttributeLink(0, 0, ref,
    # "VPMReference", attr), the PLMProductService root reference, and the
    # template copy (template Selection.Add + Copy, target Selection.Add(
    # background view) + Paste) with TitleBlock.AnchorPoint = 8 / 6.
    # Everything else here is marked unverified where it is used.

    def _layout_tool_definitions(self) -> list[dict[str, Any]]:
        dimension_target = {
            "view_name": {
                "type": "string",
                "description": "View holding the dimension (default: last view of the active sheet)",
            },
            "dimension_index": {
                "type": "integer",
                "description": "1-based index from catia_drawing_list_dimensions",
            },
            "dimension_name": {"type": "string", "description": "Dimension name (instead of the index)"},
        }
        table_target = {
            "view_name": {
                "type": "string",
                "description": "View holding the table (default: the sheet's Background view)",
            },
            "table_index": {"type": "integer", "description": "1-based table index in the view (default 1)"},
            "table_name": {"type": "string", "description": "Table name (instead of the index)"},
        }
        text_target = {
            "view_name": {
                "type": "string",
                "description": "View holding the text (default: the sheet's Background view)",
            },
            "text_index": {"type": "integer", "description": "1-based text index in the view"},
            "text_name": {"type": "string", "description": "Text name (instead of the index)"},
        }
        return [
            {
                "name": "catia_drawing_set_sheet",
                "description": (
                    "Set up a drawing sheet: paper size, orientation, projection "
                    "method and scale, then read the values back. Field-proven on "
                    "R428 SP6: PaperSize A0-A3, Orientation landscape, "
                    "ProjectionMethod third angle. The frame and title block are "
                    "NOT resized: set the sheet before catia_drawing_apply_template."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "sheet_name": {"type": "string", "description": "Sheet (default: active sheet)"},
                        **_SHEET_PROPERTIES,
                    },
                },
            },
            {
                "name": "catia_drawing_move_view",
                "description": (
                    "Move a drawing view to (x, y) mm on the sheet (DrawingView.x / .y, "
                    "field-proven on R428 SP6). Use catia_drawing_list_views for names "
                    "and current positions."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "view_name": {"type": "string", "description": "View to move"},
                        "x": {"type": "number", "description": "New X in mm (sheet coordinates)"},
                        "y": {"type": "number", "description": "New Y in mm (sheet coordinates)"},
                    },
                    "required": ["view_name", "x", "y"],
                },
            },
            {
                "name": "catia_drawing_move_dimension",
                "description": (
                    "Move a dimension's value (and its dimension line) to (x, y) mm in "
                    "the view's coordinates: DrawingDimension.MoveValue(x, y, 0, 0), "
                    "field-proven on R428 SP6. Use it to pull generated dimensions off "
                    "the model edges."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        **dimension_target,
                        "x": {"type": "number", "description": "Target X in mm (view coordinates)"},
                        "y": {"type": "number", "description": "Target Y in mm (view coordinates)"},
                    },
                    "required": ["x", "y"],
                },
            },
            {
                "name": "catia_drawing_delete_dimension",
                "description": (
                    "Delete a dimension from a drawing view (Selection.Add + "
                    "Selection.Delete, field-proven on R428 SP6)."
                ),
                "inputSchema": {"type": "object", "properties": dimension_target},
            },
            {
                "name": "catia_drawing_set_dimension_text",
                "description": (
                    "Add text around a dimension's value: prefix/suffix "
                    "(GetValue().SetPSText(1, prefix, suffix)) and before/after/"
                    "upper/lower texts (SetBaultText(1, before, after, upper, "
                    "lower)). Field-proven on R428 SP6: prefix 'M' and after "
                    "'x1.0 - 6H THRU' on a 6 mm diameter gave 'M6x1.0 - 6H THRU'. "
                    "Each call rewrites a whole group: parts of a group left out "
                    "are set to empty."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        **dimension_target,
                        "prefix": {"type": "string", "description": "Prefix replacing the symbol (e.g. 'M')"},
                        "suffix": {"type": "string", "description": "Suffix after the value"},
                        "before": {"type": "string", "description": "Text before the value"},
                        "after": {"type": "string", "description": "Text after the value (e.g. 'x1.0 - 6H THRU')"},
                        "upper": {"type": "string", "description": "Text above the value"},
                        "lower": {"type": "string", "description": "Text below the value"},
                    },
                },
            },
            {
                "name": "catia_drawing_set_table_cell",
                "description": (
                    "Write text into drawing table cells (DrawingTable.SetCellString, "
                    "field-proven on R428 SP6), e.g. title block or revision table "
                    "fields. Give row/column/text for one cell or 'cells' for several. "
                    "Cells are addressed by row/column; the table is not moved."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        **table_target,
                        "row": {"type": "integer", "description": "1-based row"},
                        "column": {"type": "integer", "description": "1-based column"},
                        "text": {"type": "string", "description": "Cell text"},
                        "cells": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "row": {"type": "integer"},
                                    "column": {"type": "integer"},
                                    "text": {"type": "string"},
                                },
                                "required": ["row", "column", "text"],
                            },
                            "description": "Several cells at once",
                        },
                    },
                },
            },
            {
                "name": "catia_drawing_edit_text",
                "description": (
                    "Edit a drawing text: content (.Text), position (.x/.y) and font "
                    "size (SetFontSize(0, 0, size)), all field-proven on R428 SP6. "
                    "Positions are view units (from the view's axis origin, divided "
                    "by its scale) unless coords='sheet' (paper mm, converted)."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        **text_target,
                        "text": {"type": "string", "description": "New content"},
                        "x": {"type": "number", "description": "New X in mm (frame per 'coords')"},
                        "y": {"type": "number", "description": "New Y in mm (frame per 'coords')"},
                        "coords": _COORDS_PROPERTY,
                        "font_size": {"type": "number", "description": "Font size in mm"},
                    },
                },
            },
            {
                "name": "catia_drawing_link_attribute",
                "description": (
                    "3DEXPERIENCE: link a table cell (row/column) or a text to a PLM "
                    "attribute of the 3D Part shown in the drawing, so it updates with "
                    "the part (e.g. V_PartNumber = the EIN, V_Name = the title). Uses "
                    "the 3D Part's root reference (PLMProductService RootOccurrence."
                    "ReferenceRootOccurrenceOf) and InsertAttributeLink, field-proven "
                    "on R428 SP6. The attribute is read first: an empty one (an EIN "
                    "not assigned yet) is refused with ATTRIBUTE_EMPTY instead of "
                    "CATIA's generic failure."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "attribute": {
                            "type": "string",
                            "description": "PLM attribute, e.g. 'V_PartNumber' (EIN) or 'V_Name'",
                        },
                        "part_name": {
                            "type": "string",
                            "description": (
                                "Title of the open 3D Part (default: the only open 3D "
                                "content)"
                            ),
                        },
                        **table_target,
                        "row": {"type": "integer", "description": "Cell row (link a table cell)"},
                        "column": {"type": "integer", "description": "Cell column (link a table cell)"},
                        "text_index": {"type": "integer", "description": "Link this text instead of a cell"},
                        "text_name": {"type": "string", "description": "Link this text instead of a cell"},
                        "plm_type": {
                            "type": "string",
                            "description": "Type of the attribute owner (default 'VPMReference')",
                        },
                    },
                    "required": ["attribute"],
                },
            },
            {
                "name": "catia_drawing_apply_template",
                "description": (
                    "Copy a template drawing's background (tables, texts and "
                    "geometry of its Background view: frame, title block, notes) "
                    "into the target drawing's Background view by copy/paste across "
                    "editors, the route field-proven on R428 SP6 (3DEXPERIENCE has no "
                    "scriptable 'apply page layout'). Open the template first with "
                    "catia_open_from_database, and set the target sheet's size with "
                    "catia_drawing_set_sheet. Background counts are read before and "
                    "after from fresh collections and must equal before + copied per "
                    "kind: surplus items (a doubled paste) are deleted and reported, "
                    "missing ones reported. Tables whose names match "
                    "title_block_table / revision_table are anchored bottom-right "
                    "(AnchorPoint 8) / top-right (AnchorPoint 6); matching tables kept "
                    "in the template's MAIN view (some templates park them off-sheet) "
                    "are copied too and always moved inside the border "
                    "(border_offset, default 10 mm). A target that already holds the "
                    "template's named tables is refused (ALREADY_APPLIED) unless "
                    "allow_repeat. The zone frame is NOT resized to another paper size."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "template": {
                            "type": "string",
                            "description": "Title (or part of it) of the open template drawing",
                        },
                        "target": {
                            "type": "string",
                            "description": "Title of the drawing to fill (default: the active drawing)",
                        },
                        "include_geometry": {"type": "boolean", "description": "Copy 2D geometry (default true)"},
                        "include_texts": {"type": "boolean", "description": "Copy texts (default true)"},
                        "include_tables": {"type": "boolean", "description": "Copy tables (default true)"},
                        "title_block_table": {
                            "type": "string",
                            "description": "Regex matching the title block table's name (anchored bottom-right)",
                        },
                        "revision_table": {
                            "type": "string",
                            "description": "Regex matching the revision table's name (anchored top-right)",
                        },
                        "border_offset": {
                            "type": "number",
                            "description": (
                                "Distance in mm from the paper edge to the inner border. "
                                "With it, matched background tables are moved into the "
                                "corners; main-view tables are always moved (default 10)."
                            ),
                        },
                        "allow_repeat": {
                            "type": "boolean",
                            "description": (
                                "Paste even when the target already holds the template's "
                                "named tables (default false)"
                            ),
                        },
                    },
                    "required": ["template"],
                },
            },
            {
                "name": "catia_drawing_print",
                "description": (
                    "EXPERIMENTAL. Print drawing sheets to files, one file per sheet, "
                    "with DrawingSheet.PrintToFile through the ACTIVE PRINTER: the only "
                    "scripted PDF route on 3DEXPERIENCE R428 SP6 (field report "
                    "2026-11-04; catia_export refuses PDF there). The output format is "
                    "whatever that printer produces: with 'Microsoft Print to PDF' it is "
                    "a PDF, but it may be IMAGE-ONLY (no text layer, so review tools "
                    "cannot read the text) and LETTER-sized whatever the sheet size. "
                    "Each file is checked on disk (size, PDF header, fonts, page size) "
                    "and every such problem is reported as a warning. 'printer' picks "
                    "another installed printer for this call (Application.Printers / "
                    "ActivePrinter, unverified on R428) and restores the previous one."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": (
                                "Absolute output folder (files are named <drawing>_<sheet>.pdf) "
                                "or file path (several sheets add _<sheet> to the name)"
                            ),
                        },
                        "sheets": {
                            "anyOf": [
                                {"type": "string", "enum": ["all"]},
                                {"type": "array", "items": {"type": "string"}},
                            ],
                            "description": "'all' (default) or a list of sheet names",
                        },
                        "printer": {
                            "type": "string",
                            "description": (
                                "Installed printer to use for this call (exact or unique "
                                "partial name, e.g. 'Microsoft Print to PDF'); default the "
                                "active printer"
                            ),
                        },
                        "timeout": {
                            "type": "number",
                            "description": "Seconds to wait for each spooled file (default 60)",
                        },
                    },
                    "required": ["file_path"],
                },
                "annotations": {
                    "title": "Print drawing sheets to files",
                    "readOnlyHint": False,
                    "destructiveHint": False,
                    "idempotentHint": False,
                    "openWorldHint": False,
                },
            },
        ]

    # ── shared helpers ──

    @staticmethod
    def _number(args: dict[str, Any], key: str, *, required: bool = False) -> float | None:
        value = args.get(key)
        if value is None:
            if required:
                raise InvalidArgument(f"'{key}' is required.")
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            raise InvalidArgument(f"'{key}' must be a number, got {value!r}.") from None

    @staticmethod
    def _sheet_settings(args: dict[str, Any]) -> list[tuple[str, Any, str, bool]]:
        """Validated (property, value, label, field_proven) to set, in order:
        paper size before orientation."""
        settings: list[tuple[str, Any, str, bool]] = []
        paper = args.get("paper_size")
        if paper is not None:
            key = str(paper).strip().upper()
            if key not in _PAPER_SIZES:
                raise InvalidArgument(
                    f"Unknown paper_size '{paper}'. Use one of: A0, A1, A2, A3, A4, letter, tabloid."
                )
            value, proven = _PAPER_SIZES[key]
            settings.append(("PaperSize", value, key if key.startswith("A") else key.lower(), proven))
        for arg, prop, table in (
            ("orientation", "Orientation", _ORIENTATIONS),
            ("projection", "ProjectionMethod", _PROJECTION_METHODS),
        ):
            raw = args.get(arg)
            if raw is None:
                continue
            key = str(raw).strip().lower().replace(" ", "_").replace("-", "_")
            if key not in table:
                raise InvalidArgument(f"Unknown {arg} '{raw}'. Use one of: {', '.join(table)}.")
            value, proven = table[key]
            settings.append((prop, value, key, proven))
        scale = args.get("scale")
        if scale is not None:
            try:
                scale_value = float(scale)
            except (TypeError, ValueError):
                raise InvalidArgument(f"'scale' must be a number, got {scale!r}.") from None
            if scale_value <= 0:
                raise InvalidArgument("'scale' must be positive (1 = 1:1, 0.5 = 1:2).")
            # Sheet.Scale: V5 DrawingSheet property, unverified on R428.
            settings.append(("Scale", scale_value, f"scale {scale_value:g}", False))
        return settings

    @staticmethod
    def _apply_sheet_settings(sheet: Any, settings: list[tuple[str, Any, str, bool]]) -> dict[str, Any]:
        applied: list[str] = []
        read_back: dict[str, Any] = {}
        warnings: list[str] = []
        for prop, value, label, proven in settings:
            try:
                setattr(sheet, prop, value)
            except Exception as e:
                raise CATIAToolError(
                    f"Setting Sheet.{prop} = {value} ({label}) failed: {e}. "
                    + (f"Already applied: {', '.join(applied)}." if applied else "Nothing was changed."),
                    modified=None if applied else False,
                ) from e
            applied.append(label)
            try:
                actual = getattr(sheet, prop)
                actual = float(actual) if prop == "Scale" else int(actual)
            except Exception:
                actual = None
            read_back[prop] = actual
            if actual is None:
                warnings.append(f"Sheet.{prop} could not be read back.")
            elif (abs(actual - value) > 1e-9) if prop == "Scale" else actual != value:
                warnings.append(f"Sheet.{prop} reads {actual} after setting {value} ({label}).")
            if not proven:
                warnings.append(
                    f"{label} (Sheet.{prop} = {value}) is not field-proven on 3DEXPERIENCE: "
                    "check the sheet with catia_screenshot."
                )
        for method, key in (("GetPaperWidth", "paper_width_mm"), ("GetPaperHeight", "paper_height_mm")):
            try:
                read_back[key] = round(float(getattr(sheet, method)()), 2)
            except Exception:
                pass
        return {"applied": applied, "read_back": read_back, "warnings": warnings}

    def _find_sheet(self, drawing: Any, sheet_name: str | None) -> Any:
        if not sheet_name:
            return self._active_sheet(drawing)
        sheets = self._sheets(drawing)
        for sheet in sheets:
            if str(safe_get(sheet, "Name", "")) == sheet_name:
                return sheet
        names = ", ".join(str(safe_get(s, "Name", "?")) for s in sheets)
        raise ObjectNotFound(f"No sheet named '{sheet_name}'. Sheets: {names}.")

    @staticmethod
    def _find_member(
        collection: Any, index: Any, name: Any, what: str, where: str, *, default_first: bool = False
    ) -> Any:
        """Item of a drawing collection by 1-based index or by name."""
        count = int(collection.Count)
        if name:
            for i in range(1, count + 1):
                item = collection.Item(i)
                if str(safe_get(item, "Name", "")) == str(name):
                    return item
            names = ", ".join(str(safe_get(collection.Item(i), "Name", "?")) for i in range(1, count + 1))
            raise ObjectNotFound(f"No {what} named '{name}' in {where}. {what.capitalize()}s: {names or 'none'}.")
        if index is None:
            if default_first and count >= 1:
                return collection.Item(1)
            raise InvalidArgument(f"Pass the {what}'s index or name.")
        try:
            i = int(index)
        except (TypeError, ValueError):
            raise InvalidArgument(f"The {what} index must be an integer, got {index!r}.") from None
        if i < 1 or i > count:
            raise ObjectNotFound(f"{what.capitalize()} index {i} is out of range: {where} has {count} {what}(s).")
        return collection.Item(i)

    def _find_dimension(self, args: dict[str, Any]) -> tuple[Any, Any]:
        view = self._find_view(args.get("view_name"))
        if args.get("dimension_index") is None and not args.get("dimension_name"):
            raise InvalidArgument(
                "Pass dimension_index or dimension_name (see catia_drawing_list_dimensions)."
            )
        dim = self._find_member(
            view.Dimensions, args.get("dimension_index"), args.get("dimension_name"),
            "dimension", f"view '{view.Name}'",
        )
        return view, dim

    def _find_table(self, args: dict[str, Any]) -> tuple[Any, Any]:
        view = self._find_view(args.get("view_name") or "background")
        table = self._find_member(
            view.Tables, args.get("table_index"), args.get("table_name"),
            "table", f"view '{view.Name}'", default_first=True,
        )
        return view, table

    def _find_text(self, args: dict[str, Any]) -> tuple[Any, Any]:
        view = self._find_view(args.get("view_name") or "background")
        text = self._find_member(
            view.Texts, args.get("text_index"), args.get("text_name"), "text", f"view '{view.Name}'",
        )
        return view, text

    # ── tools ──

    def _set_sheet(self, args: dict[str, Any]) -> str:
        self.conn.ensure_connected()
        settings = self._sheet_settings(args)
        if not settings:
            raise InvalidArgument("Pass at least one of paper_size, orientation, projection, scale.")
        drawing = self._find_active_drawing()
        sheet = self._find_sheet(drawing, args.get("sheet_name"))
        report = self._apply_sheet_settings(sheet, settings)
        self.conn.refresh_display()
        return json.dumps({
            "sheet": str(safe_get(sheet, "Name", "<sheet>")),
            **report,
            "notes": [
                (
                    "The frame and title block are not resized with the paper; apply or "
                    "rework the template for the new size."
                ),
            ],
        }, indent=2, ensure_ascii=False)

    def _move_view(self, args: dict[str, Any]) -> str:
        self.conn.ensure_connected()
        x = self._number(args, "x", required=True)
        y = self._number(args, "y", required=True)
        if not args.get("view_name"):
            raise InvalidArgument("'view_name' is required (see catia_drawing_list_views).")
        view = self._find_view(args["view_name"])
        old = (safe_get(view, "x"), safe_get(view, "y"))
        view.x = x
        with after_mutation():
            view.y = y
        self.conn.refresh_display()
        now = (safe_get(view, "x"), safe_get(view, "y"))
        return f"View '{view.Name}' moved from ({old[0]}, {old[1]}) to ({now[0]}, {now[1]}) mm."

    def _move_dimension(self, args: dict[str, Any]) -> str:
        self.conn.ensure_connected()
        x = self._number(args, "x", required=True)
        y = self._number(args, "y", required=True)
        view, dim = self._find_dimension(args)
        dim.MoveValue(x, y, 0, 0)
        self.conn.refresh_display()
        return f"Dimension '{dim.Name}' of view '{view.Name}' moved to ({x:g}, {y:g}) mm (view coordinates)."

    def _delete_dimension(self, args: dict[str, Any]) -> str:
        self.conn.ensure_connected()
        view, dim = self._find_dimension(args)
        name = str(safe_get(dim, "Name", "<dimension>"))
        before = int(view.Dimensions.Count)
        self._delete_objects(self._find_active_drawing(), [dim])
        after = int(view.Dimensions.Count)
        self.conn.refresh_display()
        if after != before - 1:
            raise CATIAToolError(
                f"Deleting dimension '{name}' left the view's count at {after} (was {before}).",
                code="FEATURE_NO_EFFECT" if after == before else "COM_ERROR",
                modified=None,
            )
        return f"Dimension '{name}' deleted from view '{view.Name}' ({after} left)."

    def _set_dimension_text(self, args: dict[str, Any]) -> str:
        self.conn.ensure_connected()
        ps_keys = ("prefix", "suffix")
        bault_keys = ("before", "after", "upper", "lower")
        if not any(args.get(k) is not None for k in ps_keys + bault_keys):
            raise InvalidArgument("Pass at least one of prefix, suffix, before, after, upper, lower.")
        view, dim = self._find_dimension(args)
        value = dim.GetValue()
        done = []
        if any(args.get(k) is not None for k in ps_keys):
            value.SetPSText(1, str(args.get("prefix") or ""), str(args.get("suffix") or ""))
            done.append("prefix/suffix")
        if any(args.get(k) is not None for k in bault_keys):
            with after_mutation():
                value.SetBaultText(1, *(str(args.get(k) or "") for k in bault_keys))
            done.append("before/after/upper/lower")
        self.conn.refresh_display()
        parts = {k: args[k] for k in ps_keys + bault_keys if args.get(k) is not None}
        return (
            f"Dimension '{dim.Name}' of view '{view.Name}': set {' and '.join(done)} texts "
            f"{json.dumps(parts, ensure_ascii=False)}. Check the result with catia_screenshot."
        )

    def _set_table_cell(self, args: dict[str, Any]) -> str:
        self.conn.ensure_connected()
        cells = list(args.get("cells") or [])
        if args.get("row") is not None or args.get("column") is not None:
            cells.insert(0, {"row": args.get("row"), "column": args.get("column"), "text": args.get("text")})
        if not cells:
            raise InvalidArgument("Pass row, column and text, or 'cells'.")
        parsed = []
        for cell in cells:
            try:
                r, c = int(cell["row"]), int(cell["column"])
            except (KeyError, TypeError, ValueError):
                raise InvalidArgument(f"Each cell needs integer row and column: {cell!r}.") from None
            if cell.get("text") is None:
                raise InvalidArgument(f"Cell ({r}, {c}) has no text.")
            parsed.append((r, c, str(cell["text"])))
        view, table = self._find_table(args)
        rows, cols = safe_get(table, "NumberOfRows"), safe_get(table, "NumberOfColumns")
        for r, c, _ in parsed:
            if r < 1 or c < 1 or (isinstance(rows, int) and r > rows) or (isinstance(cols, int) and c > cols):
                raise InvalidArgument(
                    f"Cell ({r}, {c}) is outside table '{table.Name}' ({rows}x{cols})."
                )
        written = []
        for n, (r, c, text) in enumerate(parsed):
            if n:
                with after_mutation():
                    table.SetCellString(r, c, text)
            else:
                table.SetCellString(r, c, text)
            try:
                actual = str(table.GetCellString(r, c))
            except Exception:
                actual = None
            written.append({"row": r, "column": c, "text": text, "read_back": actual})
        self.conn.refresh_display()
        return json.dumps(
            {"view": str(view.Name), "table": str(safe_get(table, "Name", "<table>")), "cells": written},
            indent=2, ensure_ascii=False,
        )

    def _edit_text(self, args: dict[str, Any]) -> str:
        self.conn.ensure_connected()
        mode = _coords_mode(args)
        x, y = self._number(args, "x"), self._number(args, "y")
        size = self._number(args, "font_size")
        if size is not None and size <= 0:
            raise InvalidArgument("'font_size' must be positive.")
        if args.get("text") is None and x is None and y is None and size is None:
            raise InvalidArgument("Pass at least one of text, x, y, font_size.")
        view, text = self._find_text(args)
        coord_notes: list[str] = []
        where = ""
        if x is not None or y is not None:
            x, y, where, coord_notes = self._view_position(view, mode, x, y)
        steps = []
        if args.get("text") is not None:
            steps.append(("content", lambda: setattr(text, "Text", str(args["text"]))))
        if x is not None:
            steps.append(("x", lambda: setattr(text, "x", x)))
        if y is not None:
            steps.append(("y", lambda: setattr(text, "y", y)))
        if size is not None:
            steps.append(("font size", lambda: text.SetFontSize(0, 0, size)))
        for n, (_, step) in enumerate(steps):
            if n:
                with after_mutation():
                    step()
            else:
                step()
        self.conn.refresh_display()
        return " ".join([
            f"Text '{safe_get(text, 'Name', '<text>')}' of view '{view.Name}' updated "
            f"({', '.join(label for label, _ in steps)}): now '{safe_get(text, 'Text', '?')}' at "
            f"({safe_get(text, 'x', '?')}, {safe_get(text, 'y', '?')}) mm"
            + (f" ({where})." if where else " (view coordinates)."),
            *coord_notes,
        ])

    def _part_reference_3dx(self, part_name: str | None) -> tuple[Any, str]:
        """The 3D Part's VPMReference: editor.GetService("PLMProductService")
        .RootOccurrence.ReferenceRootOccurrenceOf of the part's editor
        (field-proven on R428 SP6, 2026-11-02 field report)."""
        editor, obj = self._find_source_3dx(part_name)
        title = self.plm.title_of(obj)
        if resolves(obj, "ReferenceRootOccurrenceOf"):
            root = obj
        else:
            try:
                root = self.conn.root_occurrence(editor)
            except WrongDocumentType:
                raise UnsupportedCapability(
                    f"'{title}' is a standalone 3D Shape: it has no 3D Part reference to "
                    "carry V_PartNumber / V_Name. Link attributes of a 3D Part (or Physical "
                    "Product) instead."
                ) from None
        reference = get_member(root, "ReferenceRootOccurrenceOf")
        return reference, self.plm.title_of(root)

    def _link_attribute(self, args: dict[str, Any]) -> str:
        self.conn.ensure_connected()
        if not self.conn.is_3dx:
            raise UnsupportedCapability(
                "Attribute links to VPMReference attributes are a 3DEXPERIENCE feature; "
                "on CATIA V5 fill the cell with catia_drawing_set_table_cell."
            )
        attribute = str(args.get("attribute") or "").strip()
        if not attribute:
            raise InvalidArgument("'attribute' is required (e.g. 'V_PartNumber').")
        plm_type = str(args.get("plm_type") or "VPMReference")
        to_cell = args.get("row") is not None or args.get("column") is not None
        to_text = args.get("text_index") is not None or bool(args.get("text_name"))
        if to_cell == to_text:
            raise InvalidArgument("Pass either row and column (table cell) or text_index / text_name (text).")
        if to_cell:
            try:
                row, col = int(args["row"]), int(args["column"])
            except (KeyError, TypeError, ValueError):
                raise InvalidArgument("A table cell needs integer 'row' and 'column'.") from None
            view, table = self._find_table(args)
            target = table.GetCellObject(row, col)
            where = f"cell ({row}, {col}) of table '{safe_get(table, 'Name', '<table>')}'"
        else:
            view, target = self._find_text(args)
            table = row = col = None
            where = f"text '{safe_get(target, 'Name', '<text>')}'"

        reference, part_title = self._part_reference_3dx(args.get("part_name"))
        # Field report R428 SP6: InsertAttributeLink failed with a generic
        # error while V_PartNumber was empty (no EIN yet) and worked once the
        # EIN was assigned in the UI. Read it first and refuse clearly.
        # The pre-read (GetAttributeValue) is NOT field-proven: when it
        # raises, the field-proven InsertAttributeLink is still tried and a
        # failure then names both causes.
        read_error = None
        try:
            raw = reference.GetAttributeValue(attribute)
        except Exception as e:
            raw, read_error = None, str(e)
        if read_error is None and (raw is None or str(raw).strip() == ""):
            hint = (
                "assign the EIN first (3DEXPERIENCE UI: no automation API assigns an "
                "Enterprise Item Number), then retry"
                if attribute == "V_PartNumber"
                else "set it first, then retry"
            )
            raise CATIAToolError(
                f"{attribute} is empty on '{part_title}': {hint}. Nothing was linked.",
                code="ATTRIBUTE_EMPTY",
            )
        try:
            target.InsertAttributeLink(0, 0, reference, plm_type, attribute)
        except Exception as e:
            if read_error is not None:
                state = (
                    f"'{attribute}' could not be read on '{part_title}' either ({read_error}): "
                    "it may not exist on this type or be empty (an EIN not assigned yet). "
                    "Check it with catia_get_plm_attributes."
                )
            else:
                state = f"The attribute reads '{raw}' on '{part_title}'."
            raise CATIAToolError(
                f"InsertAttributeLink({plm_type}, {attribute}) on {where} failed: {e}. {state}",
                modified=None,
            ) from e
        self.conn.refresh_display()
        if table is not None:
            try:
                shown = str(table.GetCellString(row, col))
            except Exception:
                shown = None
        else:
            shown = safe_get(target, "Text")
        value = f"value '{raw}'" if read_error is None else "value unreadable before linking"
        return (
            f"Linked {where} in view '{view.Name}' to {attribute} of '{part_title}' "
            f"({value}); it now shows {shown!r}."
        )

    # ── template ──

    def _drawing_entries(self) -> list[dict[str, Any]]:
        """Open drawings with their title and Selection."""
        entries = []
        if self.conn.is_3dx:
            for editor, obj in self._editor_objects():
                if self.conn.classify(obj) == "drawing":
                    entries.append({"title": self.plm.title_of(obj), "drawing": obj, "selection": editor.Selection})
            return entries
        for doc in iter_items(self.conn.documents):
            name = str(safe_get(doc, "Name", ""))
            if name.lower().endswith(".catdrawing"):
                entries.append({"title": name, "drawing": doc, "selection": doc.Selection})
        return entries

    @staticmethod
    def _match_entry(entries: list[dict[str, Any]], title: str, what: str) -> dict[str, Any]:
        matches = [e for e in entries if e["title"] == title] or [
            e for e in entries if title.lower() in e["title"].lower()
        ]
        titles = ", ".join(e["title"] for e in entries) or "none"
        if not matches:
            raise ObjectNotFound(
                f"No open drawing titled '{title}' for the {what}. Open drawings: {titles}. "
                "Open the template with catia_open_from_database first."
            )
        if len(matches) > 1:
            raise AmbiguousTarget(f"'{title}' matches several open drawings: {titles}.")
        return matches[0]

    # (key, collection) of the background items the template copy handles.
    _TEMPLATE_KINDS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("tables", "Tables"), ("texts", "Texts"), ("geometry", "GeometricElements"),
    )

    @classmethod
    def _background_counts(cls, view: Any) -> dict[str, int]:
        counts = {}
        for key, collection in cls._TEMPLATE_KINDS:
            try:
                counts[key] = int(getattr(view, collection).Count)
            except Exception:
                counts[key] = 0
        return counts

    @staticmethod
    def _same_com(a: Any, b: Any) -> bool:
        """COM identity: pywin32 compares dispatch wrappers by IUnknown, and
        every read hands out a new wrapper, so `is` alone is not enough."""
        if a is b:
            return True
        try:
            return bool(a == b)
        except Exception:
            return False

    @classmethod
    def _dedupe(cls, items: list[Any]) -> tuple[list[Any], int]:
        unique: list[Any] = []
        for item in items:
            if not any(cls._same_com(item, seen) for seen in unique):
                unique.append(item)
        return unique, len(items) - len(unique)

    @staticmethod
    def _paper_size_mm(sheet: Any) -> tuple[float, float, str] | None:
        """(width, height, source) of the sheet's paper in mm: GetPaperWidth /
        GetPaperHeight (V5 DrawingSheet methods, unverified on R428), else
        the CatPaperSize value (field-proven A0-A3) and Orientation."""
        try:
            width, height = float(sheet.GetPaperWidth()), float(sheet.GetPaperHeight())
            if width > 0 and height > 0:
                return width, height, "GetPaperWidth/GetPaperHeight"
        except Exception:
            pass
        try:
            short, long_ = _PAPER_MM[int(sheet.PaperSize)]
        except Exception:
            return None
        try:
            landscape = int(sheet.Orientation) == 1
        except Exception:
            landscape = True
        width, height = (long_, short) if landscape else (short, long_)
        return width, height, "PaperSize/Orientation"

    @staticmethod
    def _copy_paste(source_sel: Any, target_sel: Any, items: list[tuple[str, Any]], into: Any) -> dict[str, int]:
        """Field-proven on R428 SP6: template Selection.Add + Copy, then target
        Selection.Add(background view) + Paste. Returns, per kind, how many
        items were really selected (and so copied)."""
        copied: dict[str, int] = {}
        try:
            source_sel.Clear()
            for kind, element in items:
                try:
                    source_sel.Add(element)
                except Exception:
                    continue
                copied[kind] = copied.get(kind, 0) + 1
            if not copied:
                return copied
            source_sel.Copy()
        finally:
            try:
                source_sel.Clear()
            except Exception:
                pass
        try:
            target_sel.Clear()
            target_sel.Add(into)
            target_sel.Paste()
        finally:
            try:
                target_sel.Clear()
            except Exception:
                pass
        return copied

    def _reconcile_paste(
        self,
        sheet: Any,
        selection: Any,
        before: dict[str, int],
        before_items: dict[str, list[Any]],
        copied: dict[str, int],
    ) -> dict[str, Any]:
        """Compare the target background after a paste with before + copied,
        per kind, from freshly fetched collections (sheet.Views each time).

        Field report 2026-11-03 (R428 SP6): one apply_template call left the
        background doubled (18 tables / 8 texts / 48 elements against 9 / 4 /
        24 reported). CATIA appends pasted items at the end of a view's
        collections, so the surplus is the newest items: they are deleted
        (never an item that existed before the paste) and the counts read
        again. Whether a paste can double on R428 is not reproduced offline;
        the check holds whatever the cause."""
        after = self._background_counts(sheet.Views.Item(2))
        surplus_removed: dict[str, int] = {}
        missing: dict[str, int] = {}
        notes: list[str] = []
        doomed: list[Any] = []
        for key, collection in self._TEMPLATE_KINDS:
            expected = before.get(key, 0) + copied.get(key, 0)
            extra = after.get(key, 0) - expected
            if extra < 0 and copied.get(key):
                missing[key] = -extra
            if extra <= 0:
                continue
            coll = getattr(sheet.Views.Item(2), collection)
            candidates = [coll.Item(i) for i in range(expected + 1, after[key] + 1)]
            keep = [c for c in candidates if any(self._same_com(c, old) for old in before_items.get(key, []))]
            if keep:
                notes.append(
                    f"{len(keep)} surplus {key} item(s) existed before the paste and were kept; "
                    "check the background with catia_drawing_list_views / catia_screenshot."
                )
            removable = [c for c in candidates if not any(self._same_com(c, k) for k in keep)]
            if removable:
                surplus_removed[key] = len(removable)
                doomed.extend(removable)
        delete_error = None
        if doomed:
            try:
                selection.Clear()
                for obj in doomed:
                    selection.Add(obj)
                selection.Delete()
            except Exception as e:
                delete_error = str(e)
            finally:
                try:
                    selection.Clear()
                except Exception:
                    pass
            after = self._background_counts(sheet.Views.Item(2))
        if surplus_removed:
            notes.append(
                "CATIA pasted more than was copied ("
                + ", ".join(f"{k}: +{n}" for k, n in surplus_removed.items())
                + "): "
                + (f"deleting the duplicates FAILED ({delete_error}); remove them by hand."
                   if delete_error else "the duplicate (newest) items were deleted.")
            )
        if missing:
            notes.append(
                "Fewer items arrived than were copied ("
                + ", ".join(f"{k}: {n} missing" for k, n in missing.items())
                + "); check the background."
            )
        return {
            "after": after,
            "added": {k: after[k] - before.get(k, 0) for k in after},
            "surplus_removed": surplus_removed,
            "missing": missing,
            "delete_error": delete_error,
            "notes": notes,
        }

    def _apply_template(self, args: dict[str, Any]) -> str:
        self.conn.ensure_connected()
        if not args.get("template"):
            raise InvalidArgument("'template' (title of the open template drawing) is required.")
        patterns = {}
        for key in ("title_block_table", "revision_table"):
            if args.get(key):
                try:
                    patterns[key] = re.compile(str(args[key]), re.IGNORECASE)
                except re.error as e:
                    raise InvalidArgument(f"'{key}' is not a valid regular expression: {e}.") from None
        offset = self._number(args, "border_offset")
        if offset is not None and offset < 0:
            raise InvalidArgument("'border_offset' must not be negative.")

        entries = self._drawing_entries()
        template = self._match_entry(entries, str(args["template"]), "template")
        if args.get("target"):
            target = self._match_entry(entries, str(args["target"]), "target")
        else:
            others = [e for e in entries if e is not template]
            active = None
            try:
                drawing = self._find_active_drawing()
                active = next((e for e in others if e["drawing"] is drawing or e["drawing"] == drawing), None)
            except CATIAToolError:
                pass
            if active is None and len(others) == 1:
                active = others[0]
            if active is None:
                raise AmbiguousTarget(
                    "Pass 'target' (the drawing to fill). Open drawings: "
                    + ", ".join(e["title"] for e in entries)
                )
            target = active
        if target is template:
            raise InvalidArgument("The template and the target are the same drawing.")

        template_sheet = self._active_sheet(template["drawing"])
        source_views = template_sheet.Views
        source_bg = source_views.Item(2)
        target_sheet = self._active_sheet(target["drawing"])
        wanted = {
            "tables": args.get("include_tables", True),
            "texts": args.get("include_texts", True),
            "geometry": args.get("include_geometry", True),
        }
        items: list[tuple[str, Any]] = []
        selected: dict[str, int] = {}
        duplicates = 0
        for key, collection in self._TEMPLATE_KINDS:
            if not wanted[key]:
                continue
            try:
                found = list(iter_items(getattr(source_bg, collection)))
            except Exception:
                found = []
            found, dropped = self._dedupe(found)
            duplicates += dropped
            selected[key] = len(found)
            items.extend((key, item) for item in found)

        # Title block / revision table kept in the template's MAIN view (field
        # report 2026-11-03: the site template keeps them there, off-sheet at
        # negative y): copied in a second paste into the target background
        # and anchored inside the border.
        main_tables: list[tuple[str, Any]] = []
        if patterns and wanted["tables"]:
            try:
                main_found = list(iter_items(source_views.Item(1).Tables))
            except Exception:
                main_found = []
            for table in self._dedupe(main_found)[0]:
                name = str(safe_get(table, "Name", ""))
                role = next((r for r, p in patterns.items() if p.search(name)), None)
                if role is not None:
                    main_tables.append((role, table))
        if not items and not main_tables:
            raise ObjectNotFound(
                f"The template '{template['title']}' Background view holds nothing to copy "
                f"({', '.join(f'{k}: 0' for k in selected) or 'every kind excluded'})."
            )

        # A repeated call (a retry after a timeout, say) pastes a second copy.
        # Field report 2026-11-03 reported +9 / +4 / +24 for a background that
        # then held 18 / 8 / 48: after - before = 9 means the background
        # already held one copy BEFORE that call, i.e. the template had been
        # applied before. Refuse when the target already holds the template:
        # every distinctive table name, or every template text string with at
        # least the template's item counts.
        target_bg = target_sheet.Views.Item(2)
        before = self._background_counts(target_bg)
        try:
            existing_tables = {str(safe_get(t, "Name", "")) for t in iter_items(target_bg.Tables)}
        except Exception:
            existing_tables = set()
        # CATIA's default names ('Table.3') prove nothing: only distinctive
        # names count.
        template_tables = {
            name for name in (
                str(safe_get(t, "Name", ""))
                for t in [*(t for k, t in items if k == "tables"), *(t for _, t in main_tables)]
            )
            if name and not re.fullmatch(r"Table\.\d+", name)
        }
        template_texts = {
            s for s in (str(safe_get(t, "Text", "") or "").strip() for k, t in items if k == "texts") if s
        }
        evidence = ""
        if template_tables and template_tables <= existing_tables:
            evidence = f"table(s) {', '.join(sorted(template_tables))}"
        elif template_texts and all(before.get(k, 0) >= n for k, n in selected.items()):
            try:
                existing_texts = {
                    str(safe_get(t, "Text", "") or "").strip() for t in iter_items(target_bg.Texts)
                }
            except Exception:
                existing_texts = set()
            if template_texts <= existing_texts:
                evidence = f"all {len(template_texts)} template text(s) and at least as many items"
        if evidence and not args.get("allow_repeat"):
            raise CATIAToolError(
                f"The target's background already holds the template's {evidence}: the "
                "template looks applied already. Nothing was pasted. Pass allow_repeat=true "
                "to paste another copy.",
                code="ALREADY_APPLIED",
            )

        before_items = {
            key: list(iter_items(getattr(target_bg, collection)))
            for key, collection in self._TEMPLATE_KINDS
        }
        source_sel, target_sel = template["selection"], target["selection"]
        copied = self._copy_paste(source_sel, target_sel, items, target_sheet.Views.Item(2)) if items else {}

        with after_mutation():
            first = self._reconcile_paste(target_sheet, target_sel, before, before_items, copied)
            if items and not any(v > 0 for v in first["added"].values()):
                raise CATIAToolError(
                    "Paste added nothing to the target's Background view (CATIA accepted the "
                    "copy and paste without effect). Activate the target drawing and retry, "
                    "or paste in the UI.",
                    code="FEATURE_NO_EFFECT",
                    modified=False,
                )
            tables_after_first = first["after"]["tables"]
            anchored = self._anchor_template_tables(
                target_sheet, target_sheet.Views.Item(2), before["tables"], patterns, offset,
                last=tables_after_first,
            )
            second = None
            if main_tables:
                fresh_bg = target_sheet.Views.Item(2)
                mid_items = {
                    key: list(iter_items(getattr(fresh_bg, collection)))
                    for key, collection in self._TEMPLATE_KINDS
                }
                main_copied = self._copy_paste(
                    source_sel, target_sel, [("tables", t) for _, t in main_tables], fresh_bg,
                )
                second = self._reconcile_paste(
                    target_sheet, target_sel, first["after"], mid_items, main_copied,
                )
                # Main-view tables sit off-sheet in the template: always move
                # them inside the border (default 10 mm from the paper edge).
                anchored += self._anchor_template_tables(
                    target_sheet, target_sheet.Views.Item(2), tables_after_first, patterns,
                    offset if offset is not None else 10.0,
                    roles=[r for r, _ in main_tables], source="main view",
                )
        self.conn.refresh_display()
        final = (second or first)["after"]
        added = {k: final[k] - before[k] for k in final}
        notes = [
            (
                "The zone frame was NOT resized: if the template was drawn for another paper "
                "size, rework its frame (DrawingTable.RemoveColumn/RemoveRow/SetColumnSize/"
                "SetRowSize) or pick a template of the target size."
            ),
            *first["notes"],
            *((second or {}).get("notes") or []),
        ]
        if duplicates:
            notes.append(f"{duplicates} duplicate template element(s) were listed twice and copied once.")
        not_selected = sum(selected.values()) - sum(copied.values())
        if items and not_selected > 0:
            notes.append(f"{not_selected} template element(s) could not be selected and were not copied.")
        for key, pattern in patterns.items():
            if not any(a["role"] == key for a in anchored):
                notes.append(f"No pasted table matched {key} /{pattern.pattern}/.")
        result: dict[str, Any] = {
            "template": template["title"],
            "target": target["title"],
            "selected": selected,
            "copied": copied,
            "added_to_background": added,
            "background_counts": {"before": before, "after": final},
            "anchored_tables": anchored,
            "notes": notes,
        }
        if main_tables:
            result["main_view_tables"] = [
                {"table": str(safe_get(t, "Name", "")), "role": r} for r, t in main_tables
            ]
        surplus = {**first["surplus_removed"]}
        if second:
            for k, n in second["surplus_removed"].items():
                surplus[k] = surplus.get(k, 0) + n
        if surplus:
            result["duplicates_removed"] = surplus
        return json.dumps(result, indent=2, ensure_ascii=False)

    def _anchor_template_tables(
        self,
        sheet: Any,
        view: Any,
        first_new: int,
        patterns: dict[str, Any],
        offset: float | None,
        *,
        last: int | None = None,
        roles: list[str] | None = None,
        source: str = "background",
    ) -> list[dict[str, Any]]:
        """Anchor pasted tables (view.Tables items first_new+1 .. last): title
        block bottom-right (AnchorPoint 8), revision table top-right
        (AnchorPoint 6), field-proven on R428 SP6. A table's role comes from
        its name; when CATIA renamed it on paste, from `roles` (the copied
        tables' roles, in paste order). Moving them into the corners at
        `offset` mm from the paper edge (paper size from the sheet) is
        unverified; the position is read back."""
        if not patterns:
            return []
        paper = self._paper_size_mm(sheet) if offset is not None else None
        anchored = []
        tables = view.Tables
        end = int(tables.Count) if last is None else min(last, int(tables.Count))
        for n, i in enumerate(range(first_new + 1, end + 1)):
            table = tables.Item(i)
            name = str(safe_get(table, "Name", ""))
            role = next((r for r, p in patterns.items() if p.search(name)), None)
            if role is None and roles is not None and n < len(roles):
                role = roles[n]
            if role is None:
                continue
            anchor, corner_y = (8, "bottom") if role == "title_block_table" else (6, "top")
            table.AnchorPoint = anchor
            row: dict[str, Any] = {"table": name, "role": role, "anchor_point": anchor, "from": source}
            if paper is not None and offset is not None:
                width, height, _ = paper
                table.x = width - offset
                table.y = offset if corner_y == "bottom" else height - offset
                row["position"] = [safe_get(table, "x"), safe_get(table, "y")]
                row["paper_mm"] = [width, height]
            elif offset is not None:
                row["position"] = "not moved: paper size unreadable"
            anchored.append(row)
        return anchored

    # ── printing (experimental) ──

    # Seconds between checks for a spooled file; tests set it to 0.
    _PRINT_POLL: ClassVar[float] = 0.25

    _PDF_WARNING = (
        "PrintToFile writes whatever the active printer produces. With 'Microsoft Print "
        "to PDF' the result can be image-only (no text layer, so review tools read it "
        "from images alone) and Letter-sized whatever the sheet size (field report "
        "2026-11-04). For a vector PDF at the sheet size, export from the CATIA UI or "
        "use a PDF printer set to the sheet's paper size."
    )

    @staticmethod
    def _file_name_part(value: str) -> str:
        return re.sub(r'[<>:"/\\|?*\s]+', "_", value).strip("._") or "sheet"

    def _select_printer(self, wanted: str) -> tuple[Any, str]:
        """Make `wanted` the active printer: Application.Printers and
        Application.ActivePrinter, V5 Application members, unverified on
        3DEXPERIENCE R428. Returns (previous printer, chosen name); refuses
        before anything is printed when the API is missing."""
        app = self.conn.app
        try:
            printers = list(iter_items(app.Printers))
        except Exception as e:
            raise UnsupportedCapability(
                f"This CATIA exposes no printer list (Application.Printers: {e}). Leave "
                "'printer' out to use the active printer, or change it in the CATIA UI."
            ) from None
        names = [str(safe_get(p, "DeviceName", safe_get(p, "Name", "")) or "") for p in printers]
        matches = [p for p, n in zip(printers, names) if n.lower() == wanted.lower()] or [
            p for p, n in zip(printers, names) if wanted.lower() in n.lower()
        ]
        if len(matches) != 1:
            listing = ", ".join(n for n in names if n) or "none"
            if not matches:
                raise ObjectNotFound(f"No printer named '{wanted}'. Printers: {listing}.")
            raise AmbiguousTarget(f"'{wanted}' matches several printers: {listing}.")
        chosen = matches[0]
        chosen_name = names[printers.index(chosen)]
        previous = safe_get(app, "ActivePrinter")
        try:
            app.ActivePrinter = chosen
        except Exception as e:
            raise UnsupportedCapability(
                f"Setting Application.ActivePrinter to '{chosen_name}' failed ({e}). Select "
                "the printer in the CATIA UI and call again without 'printer'."
            ) from None
        now = str(safe_get(safe_get(app, "ActivePrinter"), "DeviceName", "") or "")
        if now and now != chosen_name:
            try:
                app.ActivePrinter = previous
            except Exception:
                pass
            raise UnsupportedCapability(
                f"Application.ActivePrinter reads '{now}' after selecting '{chosen_name}'. "
                "Select the printer in the CATIA UI."
            )
        return previous, chosen_name

    def _wait_for_file(self, path: str, before: float | None, timeout: float) -> bool:
        """True once `path` exists, is newer than `before` and its size held
        still for one poll (spoolers write the file after PrintToFile returns)."""
        deadline = time.monotonic() + timeout
        last_size = -1
        while True:
            if os.path.isfile(path):
                mtime = os.path.getmtime(path)
                size = os.path.getsize(path)
                if (before is None or mtime > before) and size > 0 and size == last_size:
                    return True
                last_size = size
            if time.monotonic() >= deadline:
                return os.path.isfile(path) and os.path.getsize(path) > 0 and (
                    before is None or os.path.getmtime(path) > before
                )
            time.sleep(self._PRINT_POLL)

    @staticmethod
    def _inspect_print(path: str, paper: tuple[float, float, str] | None) -> tuple[dict[str, Any], list[str]]:
        """Size, PDF header, text layer (fonts) and page size of a printed file."""
        size = os.path.getsize(path)
        info: dict[str, Any] = {"file": path, "bytes": size}
        warnings: list[str] = []
        with open(path, "rb") as handle:
            data = handle.read(64 * 1024 * 1024)
        info["pdf"] = data.startswith(b"%PDF")
        if not info["pdf"]:
            warnings.append(
                f"{os.path.basename(path)} is not a PDF: the active printer does not print to PDF."
            )
            return info, warnings
        if b"/Font" in data:
            info["text_layer"] = True
        elif b"/ObjStm" in data:
            info["text_layer"] = None  # compressed object streams: cannot tell
        else:
            info["text_layer"] = False
            warnings.append(
                f"{os.path.basename(path)} has no fonts: it is image-only (no text layer)."
            )
        box = re.search(
            rb"/MediaBox\s*\[\s*(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s*\]", data
        )
        if box:
            x1, y1, x2, y2 = (float(v) for v in box.groups())
            page = (round(abs(x2 - x1) * 25.4 / 72, 1), round(abs(y2 - y1) * 25.4 / 72, 1))
            info["page_mm"] = list(page)
            letter = sorted(page) == sorted((215.9, 279.4)) or all(
                abs(a - b) < 1.0 for a, b in zip(sorted(page), (215.9, 279.4))
            )
            if paper is not None:
                sheet_mm = sorted(paper[:2])
                if any(abs(a - b) > 2.0 for a, b in zip(sorted(page), sheet_mm)):
                    warnings.append(
                        f"{os.path.basename(path)} page is {page[0]:g} x {page[1]:g} mm"
                        + (" (Letter)" if letter else "")
                        + f" but the sheet is {paper[0]:g} x {paper[1]:g} mm: the printer "
                        "scaled the drawing."
                    )
        return info, warnings

    def _print(self, args: dict[str, Any]) -> str:
        self.conn.ensure_connected()
        raw = str(args.get("file_path") or "").strip()
        if not raw:
            raise InvalidArgument("'file_path' (absolute output folder or file) is required.")
        path = os.path.expanduser(raw)
        if not os.path.isabs(path):
            raise InvalidArgument(f"'file_path' must be absolute, got {raw!r}.")
        timeout = self._number(args, "timeout")
        timeout = 60.0 if timeout is None else max(0.0, min(timeout, 600.0))
        drawing = self._find_active_drawing()
        sheets = self._sheets(drawing)
        wanted = args.get("sheets") or "all"
        if isinstance(wanted, str):
            if wanted.strip().lower() != "all":
                wanted = [wanted]
            else:
                wanted = None
        if wanted is not None:
            by_name = {str(safe_get(s, "Name", "")): s for s in sheets}
            unknown = [str(n) for n in wanted if str(n) not in by_name]
            if unknown:
                raise ObjectNotFound(
                    f"No sheet named {', '.join(repr(n) for n in unknown)}. Sheets: "
                    f"{', '.join(by_name) or 'none'}."
                )
            sheets = [by_name[str(n)] for n in dict.fromkeys(str(n) for n in wanted)]

        title = self.plm.title_of(drawing) if self.conn.is_3dx else str(safe_get(drawing, "Name", "drawing"))
        title = os.path.splitext(str(title or "drawing"))[0]
        root, ext = os.path.splitext(path)
        as_folder = os.path.isdir(path) or raw.endswith(("/", "\\")) or not ext
        names = [str(safe_get(s, "Name", f"Sheet.{i}")) for i, s in enumerate(sheets, start=1)]
        if as_folder:
            folder = path
            outputs = [
                os.path.join(folder, f"{self._file_name_part(title)}_{self._file_name_part(n)}.pdf")
                for n in names
            ]
        else:
            folder = os.path.dirname(path)
            outputs = [path] if len(sheets) == 1 else [
                f"{root}_{self._file_name_part(n)}{ext}" for n in names
            ]
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as e:
            raise InvalidArgument(f"Cannot create the output folder '{folder}': {e}.") from None

        app = self.conn.app
        previous_printer = None
        if args.get("printer"):
            previous_printer, printer_name = self._select_printer(str(args["printer"]))
        else:
            printer_name = str(safe_get(safe_get(app, "ActivePrinter"), "DeviceName", "") or "") or None
        try:
            original_sheet = self._active_sheet(drawing)
        except Exception:
            original_sheet = None

        files: list[dict[str, Any]] = []
        warnings: list[str] = []
        try:
            for sheet, name, out in zip(sheets, names, outputs):
                before = os.path.getmtime(out) if os.path.exists(out) else None
                try:
                    sheet.Activate()  # unverified whether PrintToFile needs it
                except Exception:
                    pass
                try:
                    sheet.PrintToFile(out)
                except Exception as e:
                    files.append({"sheet": name, "file": out, "error": f"PrintToFile failed: {e}"})
                    continue
                if not self._wait_for_file(out, before, timeout):
                    files.append({
                        "sheet": name, "file": out,
                        "error": f"no new file on disk after {timeout:g} s (printer queue or dialog?)",
                    })
                    continue
                info, problems = self._inspect_print(out, self._paper_size_mm(sheet))
                files.append({"sheet": name, **info})
                warnings.extend(problems)
        finally:
            if original_sheet is not None:
                try:
                    original_sheet.Activate()
                except Exception:
                    pass
            if args.get("printer"):
                try:
                    app.ActivePrinter = previous_printer
                except Exception:
                    warnings.append("The previous active printer could not be restored.")
        written = [f for f in files if "error" not in f]
        if not written:
            raise CATIAToolError(
                "No sheet was printed to a file: "
                + "; ".join(f"{f['sheet']}: {f['error']}" for f in files)
                + ". Print from the CATIA UI.",
                code="FEATURE_NO_EFFECT",
            )
        return json.dumps({
            "experimental": True,
            "drawing": title,
            "printer": printer_name,
            "files": files,
            "warnings": [*dict.fromkeys(warnings), self._PDF_WARNING],
        }, indent=2, ensure_ascii=False)
