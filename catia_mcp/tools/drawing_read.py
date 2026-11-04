"""catia_drawing_read: a drawing's structured content as JSON, read-only.

Recommendation 1 of field report 2026-11-04 (kept outside the repository):
a client checking a drawing (title block, notes, tables, dimensions with
their frames) should not re-solve template parsing in a hand-written
script. This tool only READS: it never calls Update, never touches the
Selection, never sets a property. Every COM read is guarded; one bad element
gives nulls for that element, never a failed call.

Field-proven on R428 SP6 (field report 2026-11-03/04) and relied on here:
  * text / table coordinates are view units from the view's axis origin
    (xAxisData, yAxisData) times the view Scale (drawing_model.view_to_sheet);
  * dimension ValueFrame codes (0 none, 1 circle, 4 square, 5 rectangle =
    basic) differ from text FrameType codes (3 circle = balloon,
    5 diamond = flag, 1 rectangle = datum feature "Datum Feature.N");
  * GetValue().GetFormatPrecision(1) and GetBaultText through a VBScript
    out-parameter call (4 outs, index 1); DimType 9 = diameter;
  * feature control frames are plain DrawingTable objects whose cells hold
    the GD&T symbol, tolerance and datum letters;
  * title blocks come in two layouts: label ending with ':' and the value in
    the ROW BELOW (newer template), or the value in the NEXT CELL to the
    right (older layout); notes may live in the background view.
Unverified (read defensively, reported as null when unreadable): GetPSText
prefix/suffix, table x/y following the text convention, rotated views
(sheet coordinates are not rotation-compensated), the BOM / revision /
title-block keyword heuristics.
"""

from __future__ import annotations

import math
import re
from typing import Any

from catia_mcp.automation import call_out_values, iter_items, safe_get
from catia_mcp.connection import CATIAConnection
from catia_mcp.drawing_model import (
    DIM_FRAMES,
    GDT_SYMBOLS,
    ORIENTATION_NAMES,
    PAPER_SIZE_NAMES,
    PROJECTION_NAMES,
    TEXT_FRAMES,
    dim_frame_name,
    dim_kind,
    text_frame_name,
    view_axis,
)
from catia_mcp.errors import InvalidArgument, ObjectNotFound
from catia_mcp.plm import PLMSession
from catia_mcp.results import operation_result
from catia_mcp.tools.drawing import DraftingTools

TOOL = "catia_drawing_read"

SECTIONS = (
    "sheets", "views", "texts", "tables", "dimensions", "title_block",
    "revision_table", "notes", "bom", "annotations",
)
DEFAULT_MAX_ITEMS = 200
_MAX_ITEMS_LIMIT = 5000

# Generic title-block labels (normalised: upper case, no trailing ':' / '.').
_TITLE_LABELS = frozenset({
    "TITLE", "DRAWN", "DRAWN BY", "DRAWN DATE", "CHECKED", "CHECKED BY", "APPROVED",
    "APPROVED BY", "ENGINEER", "DESIGNER", "DESIGNED BY", "DATE", "SCALE", "SIZE", "SHEET",
    "REV", "REVISION", "PART NUMBER", "PART NO", "P/N", "DWG NO", "DRAWING NO",
    "DRAWING NUMBER", "MATERIAL", "FINISH", "WEIGHT", "MASS", "UNITS", "TOLERANCES",
    "TOLERANCE", "PROJECTION", "DESCRIPTION", "CAGE CODE", "TREATMENT", "SHEET SIZE",
})
_REV_HEADERS = frozenset({"REV", "REVISION", "LTR", "REV LTR"})
_REV_COMPANIONS = frozenset({"DESCRIPTION", "DATE", "ECO", "ECN", "ECR", "ZONE", "APPROVED", "CHANGE"})
_BOM_ITEM = frozenset({"ITEM", "ITEM NO", "FIND NO", "FIND", "PART NUMBER", "PART NO", "P/N"})
_BOM_QTY = frozenset({"QTY", "QUANTITY", "QTY REQD", "QTY REQ"})

_NOTE_LINE = re.compile(r"^\s*(\d{1,3})[.)](?:\s+(.*))?$")
_NUMBER_ONLY = re.compile(r"^\s*(\d{1,3})\s*$")
_INLINE_LABEL = re.compile(r"^\s*([^:\n]{1,40}):\s*(\S.*)$", re.DOTALL)
_DATUM_LETTERS = re.compile(r"^[A-Z]{1,2}(?:-[A-Z]{1,2})?$")
# A datum reference cell of a feature control frame: the letter(s), optionally
# followed by a material-condition modifier (M / L / S / P / T / U / F circled).
_DATUM_REF = re.compile(r"^([A-Z]{1,2}(?:-[A-Z]{1,2})?)\s*[\u24c2\u24c1\u24c8\u24c5\u24c9\u24ca\u24bb]?$")
# Captions that end with ':' but are not title-block labels.
_CAPTIONS = frozenset({"NOTES", "NOTE", "GENERAL NOTES", "REVISIONS", "REVISION HISTORY"})


def _norm(text: Any) -> str:
    return " ".join(str(text or "").upper().split()).rstrip(":.").strip()


def _lines(text: str) -> list[str]:
    return re.split(r"\r\n|\r|\n", text) if text else []


def _num(obj: Any, name: str, digits: int = 4) -> float | None:
    try:
        value = float(getattr(obj, name))
    except Exception:  # noqa: BLE001 - guarded COM read
        return None
    return round(value, digits) if math.isfinite(value) else None


def _int(obj: Any, name: str) -> int | None:
    try:
        return int(getattr(obj, name))
    except Exception:  # noqa: BLE001 - guarded COM read
        return None


class _Capped:
    """Reads at most `limit` items of a collection and remembers the total."""

    def __init__(self, collection: Any, limit: int) -> None:
        self.items: list[tuple[int, Any]] = []
        self.total = 0
        self.unreadable = collection is None
        if collection is None:
            return
        try:
            self.total = int(collection.Count)
        except Exception:  # noqa: BLE001 - guarded COM read
            self.total = 0
            self.unreadable = True
        for i in range(1, min(self.total, limit) + 1):
            try:
                self.items.append((i, collection.Item(i)))
            except Exception:  # noqa: BLE001 - guarded COM read
                self.items.append((i, None))

    @property
    def truncated(self) -> bool:
        return self.total > len(self.items)


class _View:
    """One view's content, read lazily and at most once."""

    def __init__(self, reader: DrawingReadTools, view: Any, index: int) -> None:
        self.reader = reader
        self.com = view
        self.index = index
        self.name = str(safe_get(view, "Name", f"<view {index}>"))
        self.role = DraftingTools._view_role(index)
        self._texts: list[dict[str, Any]] | None = None
        self._tables: list[dict[str, Any]] | None = None
        self._dims: list[dict[str, Any]] | None = None
        self._axis: tuple[float, float, float] | None = None
        self._axis_read = False

    def axis(self) -> tuple[float, float, float] | None:
        """(xAxisData, yAxisData, Scale), read once per view (not once per
        text); None when unreadable, so sheet coordinates come back null rather
        than computed from view.x / view.y."""
        if not self._axis_read:
            self._axis_read = True
            try:
                x0, y0, scale, _ = view_axis(self.com)
                self._axis = (x0, y0, scale)
            except Exception:  # noqa: BLE001 - guarded COM read
                self._axis = None
        return self._axis

    def to_sheet(self, x: float | None, y: float | None) -> tuple[float | None, float | None]:
        # Same formula as drawing_model.view_to_sheet (field-proven on R428 SP6,
        # field report 2026-11-04), with the axis read cached.
        axis = self.axis()
        if x is None or y is None or axis is None:
            return None, None
        x0, y0, scale = axis
        return round(x0 + x * scale, 3), round(y0 + y * scale, 3)

    def texts(self) -> list[dict[str, Any]]:
        if self._texts is None:
            capped = _Capped(safe_get(self.com, "Texts"), self.reader.max_items)
            self.reader.note_truncation("texts", self.name, capped)
            self._texts = [self.reader.text_row(self, i, t) for i, t in capped.items]
        return self._texts

    def tables(self) -> list[dict[str, Any]]:
        if self._tables is None:
            capped = _Capped(safe_get(self.com, "Tables"), self.reader.max_items)
            self.reader.note_truncation("tables", self.name, capped)
            self._tables = [self.reader.table_row(self, i, t) for i, t in capped.items]
        return self._tables

    def dimensions(self) -> list[dict[str, Any]]:
        if self._dims is None:
            capped = _Capped(safe_get(self.com, "Dimensions"), self.reader.max_items)
            self.reader.note_truncation("dimensions", self.name, capped)
            self._dims = [self.reader.dimension_row(i, d) for i, d in capped.items]
        return self._dims


class DrawingReadTools:
    """Read-only structured view of a CATIA drawing (V5 and 3DEXPERIENCE)."""

    def __init__(self, connection: CATIAConnection) -> None:
        self.conn = connection
        self.plm = PLMSession(connection)
        self.drafting = DraftingTools(connection)
        self.max_items = DEFAULT_MAX_ITEMS
        self.truncated: list[dict[str, Any]] = []
        self.unreadable: list[str] = []

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": TOOL,
                "description": (
                    "Read the active drawing's structured content as JSON, without changing "
                    "anything: sheets (paper, orientation, projection, scale), views (axis "
                    "origin, scale, generative), texts with SHEET coordinates and kind "
                    "(balloon, flag, datum_feature, note), tables with cells and kind "
                    "(title_block, revision_table, bom, fcf), title-block fields (value below "
                    "a 'LABEL:' cell or in the next cell, both layouts), revision rows, BOM "
                    "rows, numbered notes (background and main view), and dimensions with "
                    "value, type, frame (basic = rectangle), precision and before/after/"
                    "upper/lower texts. Use it before checking or editing a drawing instead "
                    "of scripting the reads. Reads the active sheet unless 'sheet' is given; "
                    "'view' limits views/texts/tables/dimensions/annotations to one view."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "sections": {
                            "type": "array",
                            "items": {"type": "string", "enum": list(SECTIONS)},
                            "description": "Sections to return (default: all).",
                        },
                        "sheet": {"type": "string", "description": "Sheet name (default: the active sheet)."},
                        "view": {
                            "type": "string",
                            "description": "View name, or 'main' / 'background' (default: every view).",
                        },
                        "max_items": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": _MAX_ITEMS_LIMIT,
                            "description": (
                                f"Cap per collection (texts, tables, dimensions per view; table "
                                f"rows). Default {DEFAULT_MAX_ITEMS}; 'truncated' lists what was cut."
                            ),
                        },
                    },
                },
                "annotations": {"readOnlyHint": True, "openWorldHint": False},
            }
        ]

    def execute(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool_name != TOOL:
            raise ValueError(f"Unknown drawing-read tool: {tool_name}")
        return self.read(arguments)

    # ── arguments ──

    @staticmethod
    def _sections(args: dict[str, Any]) -> list[str]:
        raw = args.get("sections")
        if raw is None:
            return list(SECTIONS)
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list) or not raw:
            raise InvalidArgument("'sections' must be a non-empty list of section names.")
        wanted = [str(s).strip().lower() for s in raw]
        unknown = [s for s in wanted if s not in SECTIONS]
        if unknown:
            raise InvalidArgument(f"Unknown section(s) {unknown}. Use: {', '.join(SECTIONS)}.")
        return [s for s in SECTIONS if s in wanted]

    @staticmethod
    def _max_items(args: dict[str, Any]) -> int:
        raw = args.get("max_items")
        if raw is None:
            return DEFAULT_MAX_ITEMS
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise InvalidArgument(f"'max_items' must be an integer, got {raw!r}.") from None
        if value < 1 or value > _MAX_ITEMS_LIMIT:
            raise InvalidArgument(f"'max_items' must be between 1 and {_MAX_ITEMS_LIMIT}.")
        return value

    def note_truncation(self, what: str, where: str, capped: _Capped) -> None:
        if capped.unreadable:
            self.unreadable.append(f"{what} of view '{where}'")
        if capped.truncated:
            self.truncated.append(
                {"collection": what, "where": where, "total": capped.total, "returned": len(capped.items)}
            )

    # ── element rows ──

    def text_row(self, view: _View, index: int, text: Any) -> dict[str, Any]:
        name = str(safe_get(text, "Name", f"<text {index}>"))
        content = safe_get(text, "Text")
        content = None if content is None else str(content)
        x, y = _num(text, "x"), _num(text, "y")
        sx, sy = view.to_sheet(x, y)
        frame_code = _int(text, "FrameType")
        return {
            "index": index,
            "name": name,
            "text": content,
            "x": x,
            "y": y,
            "sheet_x": sx,
            "sheet_y": sy,
            "frame": text_frame_name(frame_code),
            "frame_code": frame_code,
            "kind": _text_kind(name, content, frame_code),
        }

    def table_row(self, view: _View, index: int, table: Any) -> dict[str, Any]:
        rows, cols = _int(table, "NumberOfRows"), _int(table, "NumberOfColumns")
        cells: list[list[str | None]] = []
        if rows and cols:
            if rows > self.max_items:
                self.truncated.append({
                    "collection": "table rows",
                    "where": f"{view.name} / {safe_get(table, 'Name', index)}",
                    "total": rows, "returned": self.max_items,
                })
            for r in range(1, min(rows, self.max_items) + 1):
                row = []
                for c in range(1, cols + 1):
                    try:
                        value = table.GetCellString(r, c)
                        row.append(None if value is None else str(value))
                    except Exception:  # noqa: BLE001 - guarded COM read
                        row.append(None)
                cells.append(row)
        # UNVERIFIED: table x/y assumed to follow the text convention
        # (view units from the axis origin).
        x, y = _num(table, "x"), _num(table, "y")
        sx, sy = view.to_sheet(x, y)
        return {
            "index": index,
            "name": str(safe_get(table, "Name", f"<table {index}>")),
            "rows": rows,
            "cols": cols,
            "x": x,
            "y": y,
            "sheet_x": sx,
            "sheet_y": sy,
            "kind": _table_kind(cells),
            "cells": cells,
        }

    def dimension_row(self, index: int, dim: Any) -> dict[str, Any]:
        if dim is None:
            return {"index": index, "name": None, "error": "unreadable"}
        try:
            row = DraftingTools._dimension_row(dim, index)
        except Exception:  # noqa: BLE001 - guarded COM read
            row = {"index": index, "name": str(safe_get(dim, "Name", "<unnamed>"))}
        row.setdefault("value", None)
        dim_type = row.get("dim_type", _int(dim, "DimType"))
        row["dim_type"] = dim_type
        row["dim_type_name"] = dim_kind(dim_type)
        frame_code = _int(dim, "ValueFrame")
        row["frame"] = dim_frame_name(frame_code)
        row["frame_code"] = frame_code
        row["basic"] = frame_code == DIM_FRAMES["rectangle"] if frame_code is not None else None
        try:
            value = dim.GetValue()
        except Exception:  # noqa: BLE001 - guarded COM read
            value = None
        row["precision"] = None
        texts: dict[str, Any] = {"before": None, "after": None, "upper": None, "lower": None}
        row["prefix"] = row["suffix"] = None
        if value is not None:
            try:
                precision = float(value.GetFormatPrecision(1))
                row["precision"] = precision if math.isfinite(precision) else None
            except Exception:  # noqa: BLE001, S110 - guarded COM read
                pass
            app = self.conn.app
            try:
                # Field-proven on R428 SP6 (field report 2026-11-04): GetBaultText
                # through a VBScript out-parameter call, 4 outs, index 1.
                outs = call_out_values(app, value, "GetBaultText", 4, 1)
                for key, out in zip(("before", "after", "upper", "lower"), outs):
                    texts[key] = None if out is None else str(out)
            except Exception:  # noqa: BLE001, S110 - guarded COM read
                pass
            try:
                # UNVERIFIED on R428: GetPSText(index, prefix, suffix) by analogy.
                prefix, suffix = call_out_values(app, value, "GetPSText", 2, 1)[:2]
                row["prefix"] = None if prefix is None else str(prefix)
                row["suffix"] = None if suffix is None else str(suffix)
            except Exception:  # noqa: BLE001, S110 - guarded COM read
                pass
        row["texts"] = texts
        return row

    # ── read ──

    def read(self, args: dict[str, Any]) -> dict[str, Any]:
        sections = self._sections(args)
        self.max_items = self._max_items(args)
        self.truncated = []
        self.unreadable = []
        warnings: list[str] = []
        self.conn.ensure_connected()

        try:
            drawing_com = self.drafting._find_active_drawing()
        except RuntimeError as error:  # the V5 branch raises a bare RuntimeError
            raise ObjectNotFound(str(error)) from None
        all_sheets = self.drafting._sheets(drawing_com)
        try:
            active_name = str(self.drafting._active_sheet(drawing_com).Name)
        except Exception:  # noqa: BLE001 - guarded COM read
            active_name = None
        sheet = self.drafting._find_sheet(drawing_com, args.get("sheet"))
        sheet_name = str(safe_get(sheet, "Name", "<sheet>"))

        views = [_View(self, v, i) for i, v in enumerate(iter_items(sheet.Views), start=1)]
        scoped = views
        if args.get("view"):
            scoped = [self._pick_view(views, str(args["view"]))]
        for v in scoped:
            angle = _num(v.com, "Angle", 6)
            if angle:
                warnings.append(
                    f"View '{v.name}' is rotated ({round(math.degrees(angle), 3)} deg): sheet "
                    "coordinates of its texts are not rotation-compensated (unverified)."
                )

        data: dict[str, Any] = {"drawing": self._drawing_info(drawing_com), "sheet": sheet_name}
        if "sheets" in sections:
            data["sheets"] = [self._sheet_info(s, active_name) for s in all_sheets]
        if "views" in sections:
            data["views"] = [self._view_info(v) for v in scoped]
        if "texts" in sections:
            data["texts"] = [{"view": v.name, "texts": v.texts()} for v in scoped]
        if "tables" in sections:
            data["tables"] = [{"view": v.name, "tables": v.tables()} for v in scoped]
        if "dimensions" in sections:
            data["dimensions"] = [{"view": v.name, "dimensions": v.dimensions()} for v in scoped]
        sheet_level = [v for v in views if v.role == "background"] + [v for v in views if v.role == "main"]
        if "title_block" in sections:
            data["title_block"] = self._title_block(sheet_level)
        if "revision_table" in sections:
            data["revision_table"] = self._keyed_tables(views, "revision_table")
        if "bom" in sections:
            data["bom"] = self._keyed_tables(views, "bom")
        if "notes" in sections:
            data["notes"] = self._notes(sheet_level, views)
        if "annotations" in sections:
            data["annotations"] = self._annotations(scoped)
        data["truncated"] = self.truncated
        if self.unreadable:
            warnings.append(
                "Could not read (reported empty): " + "; ".join(dict.fromkeys(self.unreadable)) + "."
            )
        if self.truncated:
            warnings.append(
                f"{len(self.truncated)} collection(s) were cut at max_items={self.max_items}; "
                "see data.truncated."
            )
        title = data["drawing"].get("title") or "drawing"
        return operation_result(
            tool=TOOL,
            ok=True,
            code="OK",
            message=(
                f"Read {', '.join(sections)} of '{title}', sheet '{sheet_name}' "
                f"({len(views)} view(s)). Nothing was modified."
            ),
            data=data,
            modified=False,
            warnings=warnings,
        )

    @staticmethod
    def _pick_view(views: list[_View], wanted: str) -> _View:
        for v in views:
            if v.name == wanted:
                return v
        alias = DraftingTools._VIEW_ALIASES.get(" ".join(wanted.lower().split()))
        if alias is not None and len(views) >= alias:
            return views[alias - 1]
        names = ", ".join(v.name for v in views)
        raise ObjectNotFound(
            f"No view named '{wanted}'. Views: {names or 'none'}. 'main' and 'background' also work."
        )

    def _drawing_info(self, drawing: Any) -> dict[str, Any]:
        info: dict[str, Any] = {"name": str(safe_get(drawing, "Name", "<drawing>"))}
        try:
            plm_object = self.plm.plm_object_of(drawing) if self.conn.is_3dx else None
        except Exception:  # noqa: BLE001 - guarded COM read
            plm_object = None
        if plm_object is not None:
            try:
                plm = self.plm.describe(plm_object)
            except Exception:  # noqa: BLE001 - guarded COM read
                plm = {}
            info["plm"] = plm
            info["title"] = plm.get("V_Name") or info["name"]
            info["plm_id"] = plm.get("PLM_ExternalID")
        else:
            info["title"] = info["name"]
            info["plm_id"] = None
        return info

    @staticmethod
    def _sheet_info(sheet: Any, active_name: str | None) -> dict[str, Any]:
        name = str(safe_get(sheet, "Name", "<sheet>"))
        paper = _int(sheet, "PaperSize")
        orientation = _int(sheet, "Orientation")
        projection = _int(sheet, "ProjectionMethod")
        row: dict[str, Any] = {
            "name": name,
            "active": name == active_name,
            "paper_size": PAPER_SIZE_NAMES.get(paper) if paper is not None else None,
            "paper_size_code": paper,
            "orientation": ORIENTATION_NAMES.get(orientation) if orientation is not None else None,
            "projection": PROJECTION_NAMES.get(projection) if projection is not None else None,
            "scale": _num(sheet, "Scale", 6),
        }
        for key, method in (("paper_width", "GetPaperWidth"), ("paper_height", "GetPaperHeight")):
            try:
                row[key] = round(float(getattr(sheet, method)()), 2)
            except Exception:  # noqa: BLE001 - guarded COM read
                row[key] = None
        return row

    def _view_info(self, v: _View) -> dict[str, Any]:
        com = v.com
        angle = _num(com, "Angle", 6)
        row: dict[str, Any] = {
            "index": v.index,
            "name": v.name,
            "role": v.role,
            "x": _num(com, "x"),
            "y": _num(com, "y"),
            "xAxisData": _num(com, "xAxisData"),
            "yAxisData": _num(com, "yAxisData"),
            "scale": _num(com, "Scale", 6),
            "angle_deg": round(math.degrees(angle), 4) if angle is not None else None,
        }
        generative = None
        try:
            generative = bool(com.IsGenerative())
        except Exception:  # noqa: BLE001, S110 - guarded COM read
            pass
        behavior = safe_get(com, "GenerativeBehavior")
        linked = None
        if behavior is not None:
            document = safe_get(behavior, "Document")
            if document is not None:
                try:
                    linked = self.plm.title_of(document) if self.conn.is_3dx else str(document.Name)
                except Exception:  # noqa: BLE001 - guarded COM read
                    linked = safe_get(document, "Name")
            if generative is None:
                generative = document is not None
        row["generative"] = generative
        row["linked_document"] = None if linked is None else str(linked)
        for key, coll in (("texts", "Texts"), ("tables", "Tables"), ("dimensions", "Dimensions")):
            row[f"{key}_count"] = _int(safe_get(com, coll), "Count")
        return row

    # ── title block ──

    def _title_block(self, sheet_level: list[_View]) -> dict[str, Any]:
        fields: list[dict[str, Any]] = []
        sources = []
        candidates = [(v, t) for v in sheet_level for t in v.tables() if t["kind"] == "title_block"]
        for v, table in candidates:
            parsed = parse_title_block_cells(table["cells"])
            for f in parsed:
                f.update({"view": v.name, "table": table["name"]})
            fields.extend(parsed)
            sources.append({"view": v.name, "table": table["name"]})
        seen = {_norm(f["label"]) for f in fields}
        background = [v for v in sheet_level if v.role == "background"]
        for v in background:
            for f in parse_title_block_texts(v.texts()):
                if _norm(f["label"]) not in seen:
                    seen.add(_norm(f["label"]))
                    f["view"] = v.name
                    fields.append(f)
        values: dict[str, Any] = {}
        layouts: dict[str, Any] = {}
        for f in fields:
            values.setdefault(f["label"], f["value"])
            layouts.setdefault(f["label"], f["layout"])
        return {"found": bool(fields), "fields": values, "layouts": layouts, "sources": sources, "detail": fields}

    # ── keyed tables (revision, BOM) ──

    @staticmethod
    def _keyed_tables(views: list[_View], kind: str) -> list[dict[str, Any]]:
        out = []
        for v in views:
            for table in v.tables():
                if table["kind"] != kind:
                    continue
                header_index = _header_row(table["cells"], kind)
                if header_index is None:
                    out.append({"view": v.name, "table": table["name"], "header": None, "rows": [], "cells": table["cells"]})
                    continue
                header = table["cells"][header_index]
                keys = [str(h).strip() if h and str(h).strip() else f"col{c + 1}" for c, h in enumerate(header)]
                rows = []
                for r, cells in enumerate(table["cells"]):
                    if r == header_index or not any((c or "").strip() for c in cells):
                        continue
                    if r < header_index and kind == "revision_table" and sum(1 for c in cells if (c or "").strip()) <= 1:
                        continue  # a "REVISIONS" caption row above the header
                    rows.append({"row": r + 1, **{k: c for k, c in zip(keys, cells)}})
                out.append({"view": v.name, "table": table["name"], "header": keys, "header_row": header_index + 1, "rows": rows})
        return out

    # ── notes ──

    def _notes(self, sheet_level: list[_View], views: list[_View]) -> dict[str, Any]:
        notes: list[dict[str, Any]] = []
        for v in sheet_level:
            for t in v.texts():
                if t["kind"] in ("balloon", "flag", "datum_feature") or not t["text"]:
                    continue
                for number, body in parse_numbered_notes(t["text"]):
                    notes.append({
                        "number": number, "text": body, "view": v.name, "view_role": v.role,
                        "source": t["name"], "sheet_x": t["sheet_x"], "sheet_y": t["sheet_y"],
                    })
        flags = []
        for v in views:
            for t in v.texts():
                if t["kind"] != "flag":
                    continue
                match = _NUMBER_ONLY.match(t["text"] or "")
                flags.append({
                    "number": int(match.group(1)) if match else None, "text": t["text"], "view": v.name,
                    "name": t["name"], "sheet_x": t["sheet_x"], "sheet_y": t["sheet_y"],
                })
        flagged = {f["number"] for f in flags if f["number"] is not None}
        for n in notes:
            n["flagged"] = n["number"] in flagged
        notes.sort(key=lambda n: n["number"])
        numbers = [n["number"] for n in notes]
        expected = list(range(1, max(numbers) + 1)) if numbers else []
        return {
            "notes": notes,
            "flags": flags,
            "missing_numbers": [n for n in expected if n not in numbers],
            "duplicate_numbers": sorted({n for n in numbers if numbers.count(n) > 1}),
        }

    # ── annotations ──

    @staticmethod
    def _annotations(views: list[_View]) -> dict[str, Any]:
        balloons, flags, datums, fcfs = [], [], [], []
        for v in views:
            for t in v.texts():
                entry = {"view": v.name, "name": t["name"], "text": t["text"]}
                if t["kind"] == "balloon":
                    balloons.append(entry)
                elif t["kind"] == "flag":
                    flags.append(entry)
                elif t["kind"] == "datum_feature":
                    letter = (t["text"] or "").strip()
                    datums.append({**entry, "letter": letter or None})
            for table in v.tables():
                if table["kind"] == "fcf":
                    row = table["cells"][0] if table["cells"] else []
                    fcfs.append({
                        "view": v.name, "table": table["name"],
                        "frame": " | ".join((c or "").strip() for c in row),
                        "symbol": next(((c or "").strip()[:1] for c in row if (c or "").strip()), None),
                        # Datum cells follow the symbol / tolerance cell(s); a tolerance
                        # never matches, so scanning row[1:] also handles a frame
                        # whose symbol and tolerance share the first cell.
                        "datums": [m.group(1) for c in row[1:] if c and (m := _DATUM_REF.match(c.strip()))],
                    })
        return {
            "balloon_count": len(balloons), "balloons": balloons,
            "flag_count": len(flags), "flags": flags,
            "datum_feature_count": len(datums), "datum_features": datums,
            "datum_letters": sorted({d["letter"] for d in datums if d["letter"]}),
            "fcf_count": len(fcfs), "fcf_tables": fcfs,
        }


# ── pure parsing helpers (no COM) ──


def _text_kind(name: str, text: str | None, frame_code: int | None) -> str:
    lowered = name.lower()
    if lowered.startswith("datum feature"):
        return "datum_feature"
    if lowered.startswith("balloon") or frame_code == TEXT_FRAMES["circle"]:
        return "balloon"
    if frame_code == TEXT_FRAMES["diamond"]:
        return "flag"
    # A rectangle-framed datum letter used as a stand-in datum symbol (field
    # report 2026-11-03 placed such texts by script). Heuristic, UNVERIFIED.
    if frame_code == TEXT_FRAMES["rectangle"] and text and _DATUM_LETTERS.match(text.strip()):
        return "datum_feature"
    if text and any(_NOTE_LINE.match(line) for line in _lines(text)):
        return "note"
    return "other"


def _has_gdt(cell: str | None) -> bool:
    return bool(cell) and any(ch in GDT_SYMBOLS for ch in cell)


def _header_row(cells: list[list[str | None]], kind: str) -> int | None:
    for r, row in enumerate(cells[:4] if kind == "revision_table" else cells):
        # Revision headers are bare words; a 'REV:' cell is a title-block label.
        norms = {_norm(c) for c in row if c and not str(c).strip().endswith(":")}
        if kind == "revision_table" and norms & _REV_HEADERS and norms & _REV_COMPANIONS:
            return r
        if kind == "bom" and norms & _BOM_ITEM and norms & _BOM_QTY:
            return r
    return None


def _table_kind(cells: list[list[str | None]]) -> str:
    """Classify a table from its cells: fcf, bom, revision_table,
    title_block or other (keyword heuristics, unverified beyond the field
    templates' shapes)."""
    nonempty = [(c or "").strip() for row in cells for c in row if (c or "").strip()]
    if not nonempty:
        return "other"
    first = nonempty[0]
    if (len(first) == 1 and first in GDT_SYMBOLS) or (
        len(cells) <= 3 and first[:1] in GDT_SYMBOLS
    ) or (len(cells) <= 2 and any(_has_gdt(c) for c in nonempty)):
        return "fcf"
    if _header_row(cells, "bom") is not None:
        return "bom"
    title_score = sum(1 for c in nonempty if _norm(c) in _TITLE_LABELS or _inline_label(c))
    colon_labels = sum(1 for c in nonempty if c.endswith(":"))
    caption = any(_norm(c) in ("REVISIONS", "REVISION HISTORY") for c in nonempty)
    if (_header_row(cells, "revision_table") is not None or caption) and title_score < 6:
        return "revision_table"
    if title_score >= 3 or colon_labels >= 3:
        return "title_block"
    return "other"


def _is_label(cell: str | None) -> bool:
    text = (cell or "").strip()
    return bool(text) and (text.endswith(":") or _norm(text) in _TITLE_LABELS)


def _inline_label(cell: str) -> tuple[str, str] | None:
    match = _INLINE_LABEL.match(cell or "")
    if match and _norm(match.group(1)) in _TITLE_LABELS:
        return match.group(1).strip(), match.group(2).strip()
    return None


def _label_key(cell: str) -> str:
    return " ".join(cell.split()).rstrip(":").strip()


def parse_title_block_cells(cells: list[list[str | None]]) -> list[dict[str, Any]]:
    """Title-block fields of one table, both field-proven layouts (R428 SP6,
    field report 2026-11-04):
      'below' - label ends with ':' and the value is in the row below
                (newer template);
      'right' - the value is in the next cell to the right (older layout);
      'inline' - 'LABEL: value' inside one cell.
    A colon label tries below first, a bare keyword label tries right first.
    An empty value cell gives value "" (an unfilled field)."""
    fields = []

    def cell(r: int, c: int) -> str | None:
        if 0 <= r < len(cells) and 0 <= c < len(cells[r]):
            return cells[r][c] if cells[r][c] is not None else ""
        return None

    for r, row in enumerate(cells):
        for c, raw in enumerate(row):
            text = (raw or "").strip()
            if not text:
                continue
            inline = _inline_label(text) if not text.endswith(":") else None
            if inline:
                fields.append({"label": inline[0], "value": inline[1], "layout": "inline", "row": r + 1, "column": c + 1})
                continue
            if not _is_label(text):
                continue
            below, right = cell(r + 1, c), cell(r, c + 1)
            options = {
                "below": below if below is not None and not _is_label(below) else None,
                "right": right if right is not None and not _is_label(right) else None,
            }
            order = ("below", "right") if text.endswith(":") else ("right", "below")
            layout = next((k for k in order if options[k] and options[k].strip()), None)
            if layout is None:
                layout = next((k for k in order if options[k] is not None), None)
            value = options[layout].strip() if layout else None
            fields.append({"label": _label_key(text), "value": value, "layout": layout, "row": r + 1, "column": c + 1})
    return fields


def parse_title_block_texts(texts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fallback for title blocks built from loose texts (background view):
    'LABEL: value' in one text, or a 'LABEL:' text with its value text just
    below or to the right, matched on sheet coordinates (tolerances in mm,
    heuristic and unverified)."""
    fields = []
    plain = [t for t in texts if t.get("text") and t["kind"] in ("other", "note")]
    labels = [
        t for t in plain
        if t["text"].strip().endswith(":") and len(t["text"].strip()) <= 41
        and _norm(t["text"]) not in _CAPTIONS
    ]
    # Numbered notes and 'LABEL: value' texts are never another label's value.
    values = [
        t for t in plain
        if t["kind"] == "other" and not _is_label(t["text"]) and not _inline_label(t["text"].strip())
        and t["sheet_x"] is not None and t["sheet_y"] is not None
    ]
    for t in plain:
        inline = _inline_label(t["text"].strip())
        if inline:
            fields.append({"label": inline[0], "value": inline[1], "layout": "text_inline", "source": t["name"]})
    for label in labels:
        lx, ly = label["sheet_x"], label["sheet_y"]
        if lx is None or ly is None:
            continue
        below = [v for v in values if 0 < ly - v["sheet_y"] <= 12 and abs(v["sheet_x"] - lx) <= 25]
        right = [v for v in values if abs(v["sheet_y"] - ly) <= 3 and 0 < v["sheet_x"] - lx <= 100]
        if below:
            match, layout = min(below, key=lambda v: ly - v["sheet_y"]), "text_below"
        elif right:
            match, layout = min(right, key=lambda v: v["sheet_x"] - lx), "text_right"
        else:
            match, layout = None, None
        fields.append({
            "label": _label_key(label["text"].strip()),
            "value": match["text"].strip() if match else None,
            "layout": layout,
            "source": label["name"],
        })
    return fields


def parse_numbered_notes(text: str) -> list[tuple[int, str]]:
    """Numbered notes in one text: lines starting '1.' / '2)' start a note,
    following unnumbered lines continue it; lines before the first number
    (a 'NOTES:' caption) are skipped."""
    notes: list[list[Any]] = []
    for line in _lines(text):
        match = _NOTE_LINE.match(line)
        if match:
            notes.append([int(match.group(1)), (match.group(2) or "").strip()])
        elif notes and line.strip():
            notes[-1][1] = f"{notes[-1][1]} {line.strip()}".strip()
    return [(n, body) for n, body in notes]


__all__ = [
    "DrawingReadTools",
    "parse_numbered_notes",
    "parse_title_block_cells",
    "parse_title_block_texts",
]
