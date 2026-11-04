"""Drawing annotation tools: basic/reference dimension frames, marking
flags, datum feature stand-ins and feature control frames.

Field-proven on R428 SP6 (field report 2026-11-03/04; kept outside the
repository), and implemented as found there:

- DrawingDimension.ValueFrame (CatDimFrame): 0 none, 1 circle, 4 square,
  5 rectangle. A basic dimension is 5; 1 draws a circle (the bug hit live).
- DrawingText.FrameType (CatTextFrameType) is a DIFFERENT enum: 3 circle
  (balloon), 5 diamond (marking flag). Datum feature stand-ins are texts
  named "Datum Feature.N" with FrameType 1.
- Basic-dimension precision: GetValue().SetFormatPrecision(1, 0.001);
  read back with GetFormatPrecision(1).
- DrawingTexts.Add takes view coordinates measured from the view's axis
  origin (xAxisData, yAxisData) and divided by view.Scale, NOT from
  view.x / view.y:  x = (sheet_x - xAxisData) / Scale. DrawingTables.Add is
  ASSUMED to use the same frame (unverified on R428; read back).
- DrawingText.SetFontSize(0, 0, size) and GetValue().SetPSText (field
  report 2026-11-02).
- Feature control frames on the site's drawings are plain DrawingTable
  objects (symbol | tolerance | A | B | C), font "DS ISO 1 (OpenType)" at
  3.5 mm; view.GDTs was empty. datum feature symbols have no automation
  object at all.

Everything else is marked unverified where it is used and read back after
being set. Alternative not implemented: DrawingGDTs.Add created one FCF in
the 2026-11-03 session, but the site's drawings use tables, so tables are
what these tools create.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

from catia_mcp.automation import call_out_values, iter_items, safe_get
from catia_mcp.connection import CATIAConnection
from catia_mcp.drawing_model import (
    DIM_FRAME_ALIASES,
    DIM_FRAMES,
    TEXT_FRAMES,
    dim_frame_code,
    sheet_to_view,
    text_frame_code,
    view_axis,
    view_to_sheet,
)
from catia_mcp.errors import (
    CATIAToolError,
    InvalidArgument,
    ObjectNotFound,
    UnsupportedCapability,
)
from catia_mcp.tools.drawing import DraftingTools

# ── enums ──

# Dimension frames: drawing_model.DIM_FRAMES (CatDimFrame, field-proven on
# R428 SP6: none 0, circle 1, square 4, rectangle 5; 'basic' = rectangle).
# 'reference' has no frame code: no frame, value in parentheses (SetPSText,
# unverified on R428).
_DIM_FRAME_CHOICES = sorted([*DIM_FRAMES, *DIM_FRAME_ALIASES, "reference"])

# Text frames: drawing_model.TEXT_FRAMES (CatTextFrameType, field-proven:
# none 0, rectangle 1, circle 3, diamond 5). triangle = 6 follows the V5 enum
# order and is UNVERIFIED on R428: read back and flagged in the result.
_UNVERIFIED_TEXT_FRAMES: dict[str, int] = {"triangle": 6}
_TEXT_FRAME_CHOICES = sorted([*TEXT_FRAMES, *_UNVERIFIED_TEXT_FRAMES])


def _text_frame(shape: str) -> tuple[int, bool]:
    """(FrameType code, field_proven) for a shape name."""
    key = str(shape or "").strip().lower()
    if key in _UNVERIFIED_TEXT_FRAMES:
        return _UNVERIFIED_TEXT_FRAMES[key], False
    return text_frame_code(key), True


# GD&T characteristic glyphs (Unicode, as typed into the site's FCF tables).
_GDT_SYMBOLS: dict[str, str] = {
    "position": "⌖",  # ⌖
    "profile_surface": "⌓",  # ⌓
    "profile_line": "⌒",  # ⌒
    "flatness": "⏥",  # ⏥
    "straightness": "⏤",  # ⏤
    "perpendicularity": "⊥",  # ⊥
    "parallelism": "∥",  # ∥
    "angularity": "∠",  # ∠
    "circularity": "○",  # ○
    "cylindricity": "⌭",  # ⌭
    "symmetry": "⌯",  # ⌯
    "concentricity": "◎",  # ◎
    "circular_runout": "↗",  # ↗
    "total_runout": "⌰",  # ⌰
}
_DIAMETER = "Ø"  # Ø
_ALL_AROUND = "○"  # ○ (stand-in, see _add_fcf)

# FCF style found on the site's drawings (field report 2026-11-04).
_FCF_FONT = "DS ISO 1 (OpenType)"
_FCF_FONT_SIZE = 3.5
# Cell geometry: unverified defaults (a frame twice the text height, square
# symbol/datum cells); style_from copies the real widths.
_FCF_ROW_HEIGHT = 7.0
_FCF_CELL = 7.0

_DATUM_NAME = re.compile(r"^Datum Feature\.(\d+)$")

_PROVEN = "field-proven on R428 SP6 (field report 2026-11-03/04)"


# ── coordinates ──
# drawing_model.sheet_to_view / view_to_sheet: field-proven formula
# x = (sheet_x - xAxisData) / Scale (never view.x / view.y). A rotated view
# is not compensated there (unverified), so these tools warn about it.


def _rotated(view: Any) -> bool:
    try:
        return abs(view_axis(view)[3]) > 1e-12
    except Exception:
        return False


def _num(args: dict[str, Any], key: str, *, required: bool = False) -> float | None:
    value = args.get(key)
    if value is None:
        if required:
            raise InvalidArgument(f"'{key}' is required.")
        return None
    if isinstance(value, bool):
        raise InvalidArgument(f"'{key}' must be a number, got {value!r}.")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise InvalidArgument(f"'{key}' must be a number, got {value!r}.") from None
    if not math.isfinite(number):
        raise InvalidArgument(f"'{key}' must be finite.")
    return number


def _r(value: Any) -> Any:
    try:
        return round(float(value), 4)
    except Exception:
        return None


_VIEW_PROP = {
    "type": "string",
    "description": (
        "View that will own the annotation, by name (catia_drawing_list_views); "
        "'main' / 'background' reach the sheet's Main / Background view."
    ),
}
_SHEET_XY = {
    "sheet_x": {"type": "number", "description": "X on the sheet in mm (sheet origin = lower-left corner)"},
    "sheet_y": {"type": "number", "description": "Y on the sheet in mm"},
}


class DrawingAnnotateTools:
    """Annotation tools for drawings (basic frames, flags, datums, FCFs)."""

    def __init__(self, connection: CATIAConnection) -> None:
        self.conn = connection
        # Composed, not inherited: reuses the active-drawing, view and member
        # lookups and the write-guard target of the drafting tools.
        self.drafting = DraftingTools(connection)

    # ── definitions ──

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        write = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}
        return [
            {
                "name": "catia_drawing_set_dimension_frame",
                "description": (
                    "Frame existing dimensions of a drawing view: 'basic' (rectangle, ASME basic "
                    "dimension), 'rectangle', 'circle', 'square', 'none', or 'reference' (no frame, "
                    "value in parentheses). The CatDimFrame codes are mapped internally (basic = 5; "
                    "1 would draw a circle). Optional 'precision' sets the displayed precision, "
                    "e.g. 0.001 for 3 decimals. Every dimension is read back; a frame or precision "
                    "that did not take fails with WRONG_GEOMETRY_EFFECT."
                ),
                "annotations": write,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "view_name": _VIEW_PROP,
                        "dimension": {
                            "description": (
                                "Dimension(s) by 1-based index or name (catia_drawing_list_dimensions); "
                                "a list frames several at once."
                            ),
                            "oneOf": [
                                {"type": "integer"},
                                {"type": "string"},
                                {"type": "array", "items": {"type": ["integer", "string"]}, "minItems": 1},
                            ],
                        },
                        "frame": {"type": "string", "enum": _DIM_FRAME_CHOICES},
                        "precision": {
                            "type": "number",
                            "exclusiveMinimum": 0,
                            "description": "Display precision, e.g. 0.001 (3 decimals) or 0.1",
                        },
                    },
                    "required": ["view_name", "dimension", "frame"],
                },
            },
            {
                "name": "catia_drawing_add_flag",
                "description": (
                    "Add a framed marking text (flag note) such as a diamond '5' at a sheet position. "
                    "Sheet mm are converted to the view's coordinates from its axis origin and scale "
                    "(not view.x / view.y). shape: diamond (marking flag), circle (balloon), "
                    "rectangle, triangle (unverified), none. Returns the text name and the sheet "
                    "position read back."
                ),
                "annotations": write,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "view_name": _VIEW_PROP,
                        "text": {"type": "string", "minLength": 1},
                        **_SHEET_XY,
                        "shape": {"type": "string", "enum": _TEXT_FRAME_CHOICES, "default": "diamond"},
                        "font_size": {"type": "number", "exclusiveMinimum": 0, "description": "Text height in mm"},
                        "name": {"type": "string", "description": "Optional name for the text"},
                    },
                    "required": ["view_name", "text", "sheet_x", "sheet_y"],
                },
            },
            {
                "name": "catia_drawing_add_datum",
                "description": (
                    "Add a datum feature label (letter in a rectangle frame) at a sheet position. "
                    "CATIA exposes no automation object for datum feature symbols, so this creates "
                    "the stand-in used on existing drawings: a text named 'Datum Feature.N' (next "
                    "free N) with a rectangle frame. It has no leader or attachment triangle: add "
                    "those in the CATIA UI if the drawing standard needs them."
                ),
                "annotations": write,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "view_name": _VIEW_PROP,
                        "letter": {"type": "string", "pattern": "^[A-Za-z]{1,2}$"},
                        **_SHEET_XY,
                        "font_size": {
                            "type": "number",
                            "exclusiveMinimum": 0,
                            "description": "Text height in mm (default: copied from an existing datum, if readable)",
                        },
                    },
                    "required": ["view_name", "letter", "sheet_x", "sheet_y"],
                },
            },
            {
                "name": "catia_drawing_add_fcf",
                "description": (
                    "Add a feature control frame as a one-row drawing table "
                    "(symbol | tolerance | datum cells), the form used on existing drawings, "
                    "with GD&T glyphs (position, profile_surface, flatness, ...) and the font "
                    "'DS ISO 1 (OpenType)' 3.5 mm. 'style_from' copies column widths, row height "
                    "and font from an existing FCF table. Sheet mm position the table's anchor. "
                    "The frame has no leader: attach it in the CATIA UI if required."
                ),
                "annotations": write,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "view_name": _VIEW_PROP,
                        "symbol": {"type": "string", "enum": sorted(_GDT_SYMBOLS)},
                        "tolerance": {"type": "string", "minLength": 1, "description": "e.g. '1', '0.2' or 'Ø1'"},
                        "diameter": {"type": "boolean", "default": False, "description": "Prefix the tolerance with Ø"},
                        "datums": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1},
                            "maxItems": 3,
                            "description": "Datum references in order, e.g. ['A', 'B', 'C'] or ['B', 'C']",
                        },
                        **_SHEET_XY,
                        "all_around": {
                            "type": "boolean",
                            "default": False,
                            "description": (
                                "Also place an all-around circle text just left of the frame "
                                "(stand-in: tables have no leader; check the layout)"
                            ),
                        },
                        "style_from": {
                            "type": "string",
                            "description": "Name of an existing FCF table (this view first, then the sheet) to copy the style of",
                        },
                    },
                    "required": ["view_name", "symbol", "tolerance", "sheet_x", "sheet_y"],
                },
            },
        ]

    def execute(self, tool_name: str, arguments: dict[str, Any]) -> str:
        match tool_name:
            case "catia_drawing_set_dimension_frame":
                return self._set_dimension_frame(arguments)
            case "catia_drawing_add_flag":
                return self._add_flag(arguments)
            case "catia_drawing_add_datum":
                return self._add_datum(arguments)
            case "catia_drawing_add_fcf":
                return self._add_fcf(arguments)
            case _:
                raise ValueError(f"Unknown drawing annotation tool: {tool_name}")

    def write_target(self, name: str, args: dict[str, Any]) -> Any | None:
        """The drawing these tools write, for the server's write guard."""
        return self.drafting.write_target(name, args)

    # ── shared ──

    def _view(self, args: dict[str, Any]) -> Any:
        name = args.get("view_name") or args.get("view")
        if not name:
            raise InvalidArgument("'view_name' is required.")
        return self.drafting._find_view(str(name))

    def _sheet_views(self) -> list[Any]:
        drawing = self.drafting._find_active_drawing()
        views: list[Any] = []
        for sheet in self.drafting._sheets(drawing):
            try:
                views.extend(iter_items(sheet.Views))
            except Exception:
                continue
        return views

    @staticmethod
    def _to_view(view: Any, sheet_x: float, sheet_y: float) -> tuple[float, float]:
        """Sheet mm -> view coordinates (field-proven axis-origin formula).
        A rotated view is refused before anything is written: the shared
        conversion does not compensate rotation (unverified)."""
        if _rotated(view):
            raise UnsupportedCapability(
                f"View '{view.Name}' is rotated; sheet positions cannot be converted reliably "
                "for it. Place the annotation in an unrotated view (e.g. 'background'), or "
                "use catia_drawing_add_text with view coordinates."
            )
        return sheet_to_view(view, sheet_x, sheet_y)

    def _remove(self, obj: Any) -> bool:
        try:
            self.drafting._delete_objects(self.drafting._find_active_drawing(), [obj])
            return True
        except Exception:
            return False

    @staticmethod
    def _position(view: Any, obj: Any) -> dict[str, Any]:
        vx, vy = safe_get(obj, "x"), safe_get(obj, "y")
        out: dict[str, Any] = {"view_x": _r(vx), "view_y": _r(vy)}
        try:
            sx, sy = view_to_sheet(view, float(vx), float(vy))
            out.update(sheet_x=_r(sx), sheet_y=_r(sy))
        except Exception:
            out.update(sheet_x=None, sheet_y=None)
        return out

    def _place_text(
        self, view: Any, content: str, sheet_x: float, sheet_y: float,
        frame: int, font_size: float | None, name: str | None,
    ) -> tuple[Any, list[str]]:
        """Texts.Add at the converted position, then frame, size and name.
        Removes the text again when the frame does not take."""
        x, y = self._to_view(view, sheet_x, sheet_y)
        warnings: list[str] = []
        text = view.Texts.Add(content, x, y)
        try:
            text.FrameType = frame
            actual = int(text.FrameType)
        except Exception as e:
            actual = None
            error: Exception | None = e
        else:
            error = None
        if actual != frame:
            removed = self._remove(text)
            raise CATIAToolError(
                f"Text '{safe_get(text, 'Name', '?')}' was created but FrameType reads {actual} "
                f"after setting {frame}" + (f" ({error})" if error else "") + ". "
                + ("It was deleted again." if removed else "Delete it in CATIA."),
                code="WRONG_GEOMETRY_EFFECT",
                modified=not removed,
            )
        if font_size is not None:
            # DrawingText.SetFontSize(0, 0, size): field-proven on R428 SP6.
            try:
                text.SetFontSize(0, 0, font_size)
            except Exception as e:
                warnings.append(f"Font size {font_size} was not applied: {e}.")
        if name:
            # Renaming a DrawingText: unverified, read back.
            try:
                text.Name = name
            except Exception as e:
                warnings.append(f"Could not name the text '{name}': {e}.")
            if str(safe_get(text, "Name", "")) != name:
                warnings.append(
                    f"The text is named '{safe_get(text, 'Name', '?')}', not '{name}'."
                )
        return text, warnings

    # ── 1. dimension frames ──

    def _dimension_keys(self, raw: Any) -> list[tuple[int | None, str | None]]:
        items = raw if isinstance(raw, list) else [raw]
        if not items:
            raise InvalidArgument("'dimension' is empty.")
        keys: list[tuple[int | None, str | None]] = []
        for item in items:
            if isinstance(item, bool) or item is None:
                raise InvalidArgument(f"Invalid dimension reference {item!r}.")
            if isinstance(item, int) or (isinstance(item, float) and item.is_integer()):
                keys.append((int(item), None))
            elif isinstance(item, str) and item.strip():
                keys.append((None, item.strip()))
            else:
                raise InvalidArgument(f"Invalid dimension reference {item!r}: use an index or a name.")
        return keys

    def _set_dimension_frame(self, args: dict[str, Any]) -> str:
        self.conn.ensure_connected()
        frame_name = str(args.get("frame") or "").strip().lower()
        code = 0 if frame_name == "reference" else dim_frame_code(frame_name)
        precision = _num(args, "precision")
        if precision is not None and precision <= 0:
            raise InvalidArgument("'precision' must be positive, e.g. 0.001.")
        keys = self._dimension_keys(args.get("dimension"))
        view = self._view(args)
        # Resolve every dimension before changing any.
        dims = []
        for index, name in keys:
            if name is not None:
                try:
                    dim = self.drafting._find_member(view.Dimensions, None, name, "dimension", f"view '{view.Name}'")
                except ObjectNotFound:
                    if not name.isdigit():
                        raise
                    dim = self.drafting._find_member(
                        view.Dimensions, int(name), None, "dimension", f"view '{view.Name}'"
                    )
            else:
                dim = self.drafting._find_member(view.Dimensions, index, None, "dimension", f"view '{view.Name}'")
            if all(dim is not d for d in dims):
                dims.append(dim)

        results: list[dict[str, Any]] = []
        failures: list[str] = []
        warnings: list[str] = []
        for dim in dims:
            dname = str(safe_get(dim, "Name", "<dimension>"))
            row: dict[str, Any] = {"name": dname}
            try:
                row["frame_before"] = int(dim.ValueFrame)
            except Exception:
                row["frame_before"] = None
            try:
                self._apply_frame(dim, code, frame_name, precision, row, warnings)
            except Exception as e:
                failures.append(f"{dname}: {e}")
                row["error"] = str(e)
            if row.get("frame") != code:
                failures.append(f"{dname}: ValueFrame reads {row.get('frame')} after setting {code}")
            if (
                precision is not None
                and row.get("precision") is not None
                and not math.isclose(row["precision"], precision, rel_tol=1e-6, abs_tol=1e-12)
            ):
                failures.append(f"{dname}: precision reads {row['precision']} after setting {precision}")
            results.append(row)
        self.conn.refresh_display()
        if failures:
            raise CATIAToolError(
                f"Dimension frame '{frame_name}' did not take on view '{view.Name}': "
                + "; ".join(failures) + f". Per-dimension read back: {json.dumps(results, ensure_ascii=False)}",
                code="WRONG_GEOMETRY_EFFECT",
                modified=None,
            )
        note = f"ValueFrame codes and SetFormatPrecision are {_PROVEN}."
        if frame_name == "reference":
            note += " Reference parentheses use SetPSText(1, '(', ')'), unverified: check with catia_screenshot."
        return json.dumps(
            {
                "view": str(view.Name),
                "frame": frame_name,
                "value_frame": code,
                "precision": precision,
                "dimensions": results,
                "warnings": warnings,
                "note": note,
            },
            indent=2,
            ensure_ascii=False,
        )

    def _apply_frame(
        self, dim: Any, code: int, frame_name: str, precision: float | None,
        row: dict[str, Any], warnings: list[str],
    ) -> None:
        dim.ValueFrame = code
        try:
            row["frame"] = int(dim.ValueFrame)
        except Exception:
            row["frame"] = None
        value = dim.GetValue()
        if precision is not None:
            value.SetFormatPrecision(1, precision)
            try:
                row["precision"] = float(value.GetFormatPrecision(1))
            except Exception:
                row["precision"] = None
                warnings.append(f"{row['name']}: precision could not be read back.")
        if frame_name == "reference":
            # Parentheses through prefix/suffix: unverified on R428. Keep an
            # existing prefix/suffix when it can be read (GetPSText out-params).
            current = self._ps_text(value)
            prefix, suffix = current or ("", "")
            if current is None:
                warnings.append(
                    f"{row['name']}: the existing prefix/suffix could not be read and was replaced by '(' ')'."
                )
            if not prefix.startswith("("):
                prefix = "(" + prefix
            if not suffix.endswith(")"):
                suffix = suffix + ")"
            # SetPSText itself is field-proven (field report 2026-11-02); the
            # parentheses-as-reference convention is not. Read back when the
            # getter works.
            value.SetPSText(1, prefix, suffix)
            row["prefix"], row["suffix"] = prefix, suffix
            after = self._ps_text(value)
            if after is not None and after != (prefix, suffix):
                raise RuntimeError(f"prefix/suffix read back {after!r} after setting {(prefix, suffix)!r}")
        else:
            # Leaving 'reference': the parentheses are not removed
            # automatically (an existing prefix may be intentional).
            current = self._ps_text(value)
            if current is not None and current[0].startswith("(") and current[1].endswith(")"):
                warnings.append(
                    f"{row['name']}: its value is still wrapped in parentheses (prefix {current[0]!r}, "
                    f"suffix {current[1]!r}); clear them with catia_drawing_set_dimension_text if this "
                    "is no longer a reference dimension."
                )

    def _ps_text(self, value: Any) -> tuple[str, str] | None:
        """(prefix, suffix) via GetPSText(1, out, out) out-params, or None when
        unreadable (unverified getter on R428)."""
        try:
            prefix, suffix = (str(v or "") for v in call_out_values(self.conn.app, value, "GetPSText", 2, 1))
        except Exception:
            return None
        return prefix, suffix

    # ── 2. flags ──

    def _add_flag(self, args: dict[str, Any]) -> str:
        self.conn.ensure_connected()
        content = str(args.get("text") or "")
        if not content:
            raise InvalidArgument("'text' is required.")
        sx, sy = _num(args, "sheet_x", required=True), _num(args, "sheet_y", required=True)
        shape = str(args.get("shape") or "diamond").strip().lower()
        code, proven = _text_frame(shape)
        size = _num(args, "font_size")
        if size is not None and size <= 0:
            raise InvalidArgument("'font_size' must be positive.")
        view = self._view(args)
        text, warnings = self._place_text(view, content, sx, sy, code, size, args.get("name"))
        if not proven:
            warnings.append(f"Frame '{shape}' (FrameType {code}) is not field-proven: check with catia_screenshot.")
        self.conn.refresh_display()
        return json.dumps(
            {
                "view": str(view.Name),
                "name": str(safe_get(text, "Name", "<text>")),
                "text": str(safe_get(text, "Text", content)),
                "shape": shape,
                "frame_type": code,
                "requested": {"sheet_x": sx, "sheet_y": sy},
                "position": self._position(view, text),
                "warnings": warnings,
            },
            indent=2,
            ensure_ascii=False,
        )

    # ── 3. datum feature stand-ins ──

    def _existing_datums(self) -> list[tuple[int, Any]]:
        found = []
        for view in self._sheet_views():
            try:
                texts = list(iter_items(view.Texts))
            except Exception:
                continue
            for t in texts:
                m = _DATUM_NAME.match(str(safe_get(t, "Name", "")))
                if m:
                    found.append((int(m.group(1)), t))
        return found

    def _add_datum(self, args: dict[str, Any]) -> str:
        self.conn.ensure_connected()
        letter = str(args.get("letter") or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{1,2}", letter):
            raise InvalidArgument(f"'letter' must be one or two letters, got {args.get('letter')!r}.")
        sx, sy = _num(args, "sheet_x", required=True), _num(args, "sheet_y", required=True)
        size = _num(args, "font_size")
        if size is not None and size <= 0:
            raise InvalidArgument("'font_size' must be positive.")
        view = self._view(args)
        existing = self._existing_datums()
        warnings: list[str] = []
        same = [t for _, t in existing if str(safe_get(t, "Text", "")).strip().upper() == letter]
        if same:
            warnings.append(
                f"Datum {letter} already exists as {', '.join(str(safe_get(t, 'Name', '?')) for t in same)}: "
                "a datum letter should appear once per drawing."
            )
        if size is None and existing:
            # DrawingText.GetFontSize(0, 0): unverified getter.
            try:
                size = float(existing[0][1].GetFontSize(0, 0))
            except Exception:
                size = None
            if size is not None and not (math.isfinite(size) and size > 0):
                warnings.append(f"Existing datum font size read as {size}; CATIA's default size used.")
                size = None
        number = max((n for n, _ in existing), default=0) + 1
        name = f"Datum Feature.{number}"
        text, more = self._place_text(view, letter, sx, sy, TEXT_FRAMES["rectangle"], size, name)
        warnings.extend(more)
        self.conn.refresh_display()
        return json.dumps(
            {
                "view": str(view.Name),
                "name": str(safe_get(text, "Name", name)),
                "letter": letter,
                "frame_type": TEXT_FRAMES["rectangle"],
                "font_size": size,
                "requested": {"sheet_x": sx, "sheet_y": sy},
                "position": self._position(view, text),
                "warnings": warnings,
                "note": (
                    "Datum feature symbols have no automation object; this is the framed-text "
                    f"stand-in ({_PROVEN}). It has no leader or datum triangle."
                ),
            },
            indent=2,
            ensure_ascii=False,
        )

    # ── 4. feature control frames ──

    def _find_table_any(self, view: Any, name: str) -> Any:
        candidates = [view] + [v for v in self._sheet_views() if v is not view]
        for v in candidates:
            try:
                tables = list(iter_items(v.Tables))
            except Exception:
                continue
            for t in tables:
                if str(safe_get(t, "Name", "")) == name:
                    return t
        raise ObjectNotFound(f"No table named '{name}' in view '{view.Name}' or elsewhere on the drawing.")

    @staticmethod
    def _table_style(table: Any) -> dict[str, Any]:
        """Column widths, row height and cell font of an existing table.
        GetColumnSize / GetRowSize / GetFontName / GetFontSize are V5
        getters, unverified on R428: whatever cannot be read is left out."""
        style: dict[str, Any] = {}
        try:
            cols = int(table.NumberOfColumns)
            style["columns"] = cols
            widths = [float(table.GetColumnSize(c)) for c in range(1, cols + 1)]
            style["column_widths"] = widths
        except Exception:
            pass
        try:
            style["row_height"] = float(table.GetRowSize(1))
        except Exception:
            pass
        try:
            cell = table.GetCellObject(1, 1)
        except Exception:
            cell = None
        if cell is not None:
            try:
                style["font_name"] = str(cell.GetFontName(0, 0))
            except Exception:
                pass
            try:
                style["font_size"] = float(cell.GetFontSize(0, 0))
            except Exception:
                pass
        return style

    def _add_fcf(self, args: dict[str, Any]) -> str:
        self.conn.ensure_connected()
        symbol = str(args.get("symbol") or "").strip().lower()
        if symbol not in _GDT_SYMBOLS:
            raise InvalidArgument(f"Unknown symbol '{args.get('symbol')}'. Use one of: {', '.join(sorted(_GDT_SYMBOLS))}.")
        tolerance = str(args.get("tolerance") or "").strip()
        if not tolerance:
            raise InvalidArgument("'tolerance' is required, e.g. '0.2'.")
        if args.get("diameter") and not tolerance.startswith(_DIAMETER):
            tolerance = _DIAMETER + tolerance
        datums = [str(d).strip() for d in (args.get("datums") or [])]
        if any(not d for d in datums):
            raise InvalidArgument("Datum references must not be empty.")
        if len(datums) > 3:
            raise InvalidArgument("A feature control frame takes at most 3 datum references.")
        sx, sy = _num(args, "sheet_x", required=True), _num(args, "sheet_y", required=True)
        view = self._view(args)
        cells = [_GDT_SYMBOLS[symbol], tolerance, *datums]
        cols = len(cells)

        warnings: list[str] = []
        style: dict[str, Any] = {}
        if args.get("style_from"):
            source = self._find_table_any(view, str(args["style_from"]))
            style = self._table_style(source)
            missing = [k for k in ("column_widths", "row_height", "font_name", "font_size") if k not in style]
            if missing:
                warnings.append(
                    f"Could not read {', '.join(missing)} from '{args['style_from']}' (unverified getters); "
                    "defaults used for those."
                )
        widths = list(style.get("column_widths") or [])
        if widths and len(widths) != cols:
            # Different datum count: keep the symbol/tolerance widths, reuse
            # the source's datum width for every datum cell.
            datum_w = widths[2] if len(widths) > 2 else _FCF_CELL
            widths = [*widths[:2], *[datum_w] * len(datums)][:cols]
            while len(widths) < cols:
                widths.append(_FCF_CELL if len(widths) != 1 else max(10.0, 2.5 * len(tolerance) + 4))
        if not widths:
            widths = [_FCF_CELL, max(10.0, 2.5 * len(tolerance) + 4), *[_FCF_CELL] * len(datums)]
        row_height = float(style.get("row_height") or _FCF_ROW_HEIGHT)
        font_name = str(style.get("font_name") or _FCF_FONT)
        font_size = float(style.get("font_size") or _FCF_FONT_SIZE)

        x, y = self._to_view(view, sx, sy)
        # DrawingTables.Add(x, y, rows, cols, row height, column width), as
        # catia_drawing_add_table calls it.
        table = view.Tables.Add(x, y, 1, cols, row_height, widths[0])
        written: list[dict[str, Any]] = []
        for c, w in enumerate(widths, start=1):
            try:
                table.SetColumnSize(c, float(w))
            except Exception as e:
                warnings.append(f"Column {c} width {w} not applied: {e}.")
        mismatched = []
        for c, value in enumerate(cells, start=1):
            try:
                table.SetCellString(1, c, value)
            except Exception as e:
                mismatched.append(f"SetCellString(1, {c}) failed: {e}")
                break
            try:
                actual = str(table.GetCellString(1, c))
            except Exception:
                actual = None
            if actual is not None and actual != value:
                mismatched.append(f"cell {c} reads {actual!r}, expected {value!r}")
            written.append({"column": c, "text": value, "read_back": actual})
            # Cell font: GetCellObject + SetFontSize field-proven;
            # SetFontName unverified.
            try:
                cell = table.GetCellObject(1, c)
            except Exception as e:
                warnings.append(f"Cell {c} font not set (GetCellObject failed: {e}).")
                continue
            try:
                cell.SetFontName(0, 0, font_name)
            except Exception as e:
                warnings.append(f"Cell {c} font '{font_name}' not set: {e}.")
            try:
                cell.SetFontSize(0, 0, font_size)
            except Exception as e:
                warnings.append(f"Cell {c} font size {font_size} not set: {e}.")
        if mismatched:
            removed = self._remove(table)
            raise CATIAToolError(
                f"FCF table '{safe_get(table, 'Name', '?')}' was created but "
                + "; ".join(mismatched)
                + ". The glyphs may not have survived the COM call. "
                + ("The table was deleted again." if removed else "Delete the table in CATIA."),
                code="WRONG_GEOMETRY_EFFECT",
                modified=not removed,
            )
        extra = None
        if args.get("all_around"):
            # A table has no leader; the all-around circle is placed as a
            # separate text just left of the frame (unverified layout).
            try:
                extra_text, more = self._place_text(view, _ALL_AROUND, sx - 3.0, sy, 0, font_size, None)
                warnings.extend(more)
                extra = str(safe_get(extra_text, "Name", "<text>"))
                warnings.append(
                    "all_around: the circle is a separate text beside the frame, not on a leader; "
                    "move it onto the leader elbow in the CATIA UI if required."
                )
            except Exception as e:
                warnings.append(f"all_around circle not added: {e}.")
        self.conn.refresh_display()
        return json.dumps(
            {
                "view": str(view.Name),
                "table": str(safe_get(table, "Name", "<table>")),
                "frame": " | ".join(cells),
                "cells": written,
                "column_widths": widths,
                "row_height": row_height,
                "font": {"name": font_name, "size": font_size},
                "style_from": args.get("style_from"),
                "all_around_text": extra,
                "requested": {"sheet_x": sx, "sheet_y": sy},
                "position": self._position(view, table),
                "warnings": warnings,
                "note": (
                    f"FCFs as plain DrawingTables are {_PROVEN}. Tables.Add is assumed to anchor the "
                    "table at the requested point in the same frame as texts (unverified): check the "
                    "layout with catia_screenshot."
                ),
            },
            indent=2,
            ensure_ascii=False,
        )
