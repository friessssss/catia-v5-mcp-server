"""Shared drafting vocabulary: frame and dimension-type enums, sheet enums and
sheet <-> view coordinate conversion.

A leaf module (no tool imports) so every drafting tool can use it. Values
marked FIELD-PROVEN were verified live on CATIA 3DEXPERIENCE R428 SP6
(field reports 2026-11-03 and 2026-11-04, kept outside the repository);
everything else follows the V5 automation enums and is UNVERIFIED on
3DEXPERIENCE, so callers must read values back after writing them.

Stable API (other modules import these names):
    DIM_FRAMES, DIM_FRAME_ALIASES, DIM_FRAME_NAMES, dim_frame_code, dim_frame_name
    TEXT_FRAMES, TEXT_FRAME_ALIASES, TEXT_FRAME_NAMES, text_frame_code, text_frame_name
    DIM_TYPES, DIM_KINDS, dim_kind
    PAPER_SIZE_NAMES, ORIENTATION_NAMES, PROJECTION_NAMES
    GDT_SYMBOLS
    view_axis, sheet_to_view, view_to_sheet
"""

from __future__ import annotations

import math
from typing import Any

from catia_mcp.errors import InvalidArgument, UnsupportedCapability

# ── dimension value frames (DrawingDimension.ValueFrame, CatDimFrame) ──
# FIELD-PROVEN on R428 SP6 (field report 2026-11-04): 0 none, 1 circle,
# 4 square, 5 rectangle. A basic dimension is the rectangle: ValueFrame = 1
# drew a circle in the field. Reference dimensions have no frame code:
# wrapping the value in parentheses with GetValue().SetPSText(1, "(", ")")
# is a common approach, UNVERIFIED on R428.
DIM_FRAMES: dict[str, int] = {"none": 0, "circle": 1, "square": 4, "rectangle": 5}
DIM_FRAME_ALIASES: dict[str, str] = {"basic": "rectangle"}
DIM_FRAME_NAMES: dict[int, str] = {code: name for name, code in DIM_FRAMES.items()}

# ── text frames (DrawingText.FrameType, CatTextFrameType) ──
# A DIFFERENT enum from the dimension frames: 5 is a diamond here.
# FIELD-PROVEN on R428 SP6 (field report 2026-11-03/04): 3 circle (balloons,
# texts named "Balloon.N"), 5 diamond (marking flags), 1 rectangle (datum
# feature symbols, texts named "Datum Feature.N"). 0 = none. Other
# CatTextFrameType members are left out until verified.
TEXT_FRAMES: dict[str, int] = {"none": 0, "rectangle": 1, "circle": 3, "diamond": 5}
TEXT_FRAME_ALIASES: dict[str, str] = {"balloon": "circle", "flag": "diamond", "datum": "rectangle"}
TEXT_FRAME_NAMES: dict[int, str] = {code: name for name, code in TEXT_FRAMES.items()}

# ── dimension types (DrawingDimension.DimType, CatDimType, 0-based) ──
# Duplicated from catia_mcp/tools/drawing.py (DraftingTools._DIM_TYPES and
# _DIM_KINDS) so this module stays import-free; keep them in step. Angle = 4,
# Radius = 5 verified on V5R20; Diameter = 9 FIELD-PROVEN on R428 SP6
# (field report 2026-11-04). The other kinds follow the V5 enum order.
DIM_TYPES: dict[str, int] = {"distance": 0, "length": 2, "angle": 4, "radius": 5, "diameter": 9}
DIM_KINDS: dict[int, str] = {
    0: "distance", 1: "distance", 2: "length", 3: "length", 4: "angle",
    5: "radius", 6: "radius", 7: "radius", 8: "radius",
    9: "diameter", 10: "diameter", 11: "diameter", 12: "diameter", 13: "diameter",
}

# ── sheet enums ──
# PaperSize A0=2 .. A3=5, Orientation 1 = landscape and ProjectionMethod
# 1 = third angle FIELD-PROVEN on R428 SP6 (field report 2026-11-02, same
# map as drawing.py _PAPER_SIZES). The rest follow the V5 enums, UNVERIFIED.
PAPER_SIZE_NAMES: dict[int, str] = {
    0: "LETTER", 1: "LEGAL", 2: "A0", 3: "A1", 4: "A2", 5: "A3", 6: "A4",
    7: "ANSI A", 8: "TABLOID", 9: "ANSI C", 10: "ANSI D", 11: "ANSI E", 12: "ANSI F", 13: "USER",
}
ORIENTATION_NAMES: dict[int, str] = {0: "portrait", 1: "landscape"}
PROJECTION_NAMES: dict[int, str] = {0: "first_angle", 1: "third_angle"}

# GD&T characteristic symbols as typed in drawing texts and tables. Feature
# control frames on the field drawings were plain DrawingTable objects
# (symbol | tolerance | datums), not GDT objects (field report 2026-11-04).
GDT_SYMBOLS = "⌖⌓⊥∥○⌭⏥⌒∠◎⌯↗⌰⟂▱"


def _lookup(value: Any, table: dict[str, int], aliases: dict[str, str], what: str) -> int:
    key = str(value or "").strip().lower()
    key = aliases.get(key, key)
    if key not in table:
        names = sorted([*table, *aliases])
        raise InvalidArgument(f"Unknown {what} '{value}'. Use one of: {', '.join(names)}.")
    return table[key]


def _name(code: Any, names: dict[int, str]) -> str | None:
    if code is None:
        return None
    try:
        number = int(code)
    except (TypeError, ValueError):
        return None
    return names.get(number, f"unknown({number})")


def dim_frame_code(name: str) -> int:
    """ValueFrame code for 'none' | 'circle' | 'square' | 'rectangle' | 'basic'."""
    return _lookup(name, DIM_FRAMES, DIM_FRAME_ALIASES, "dimension frame")


def dim_frame_name(code: Any) -> str | None:
    return _name(code, DIM_FRAME_NAMES)


def text_frame_code(name: str) -> int:
    """FrameType code for 'none' | 'rectangle' | 'circle' | 'diamond' (or
    'balloon', 'flag', 'datum')."""
    return _lookup(name, TEXT_FRAMES, TEXT_FRAME_ALIASES, "text frame")


def text_frame_name(code: Any) -> str | None:
    return _name(code, TEXT_FRAME_NAMES)


def dim_kind(dim_type: Any) -> str:
    if dim_type is None:
        return "unknown"
    try:
        return DIM_KINDS.get(int(dim_type), "other")
    except (TypeError, ValueError):
        return "unknown"


# ── coordinates ──


def view_axis(view: Any) -> tuple[float, float, float, float]:
    """(xAxisData, yAxisData, Scale, Angle in radians) of a drawing view.

    Raises UnsupportedCapability when the axis origin or scale cannot be
    read, so a caller never silently falls back to view.x / view.y."""
    try:
        x0 = float(view.xAxisData)
        y0 = float(view.yAxisData)
        scale = float(view.Scale)
    except Exception as error:  # noqa: BLE001 - guarded COM read
        raise UnsupportedCapability(
            f"Could not read the axis origin / scale of view '{getattr(view, 'Name', '?')}': {error}"
        ) from None
    if not scale or not math.isfinite(scale):
        raise UnsupportedCapability(f"View '{getattr(view, 'Name', '?')}' reports scale {scale}.")
    try:
        angle = float(view.Angle)
    except Exception:  # noqa: BLE001 - guarded COM read
        angle = 0.0
    return x0, y0, scale, angle


def sheet_to_view(view: Any, sheet_x: float, sheet_y: float) -> tuple[float, float]:
    """Sheet millimetres -> the view coordinates DrawingTexts.Add (and
    DrawingTables.Add) expect.

    FIELD-PROVEN on R428 SP6 (field report 2026-11-04): text coordinates are
    view units from the view's AXIS ORIGIN (xAxisData, yAxisData) divided by
    the view Scale, NOT relative to view.x / view.y. A rotated view
    (Angle != 0) is not compensated: UNVERIFIED, callers should warn."""
    x0, y0, scale, _ = view_axis(view)
    return (float(sheet_x) - x0) / scale, (float(sheet_y) - y0) / scale


def view_to_sheet(view: Any, view_x: float, view_y: float) -> tuple[float, float]:
    """Inverse of sheet_to_view: a text's (x, y) in the view -> sheet mm."""
    x0, y0, scale, _ = view_axis(view)
    return x0 + float(view_x) * scale, y0 + float(view_y) * scale
