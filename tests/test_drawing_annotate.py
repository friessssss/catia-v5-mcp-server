"""Offline tests for the drawing annotation tools (field report 2026-11-03/04;
kept outside the repository): dimension frame names -> CatDimFrame codes,
precision, sheet -> view placement from the axis origin, datum stand-in
numbering, FCF tables and write-guard delegation. In-memory doubles only:
they prove orchestration, not live CATIA behaviour.
"""

from __future__ import annotations

import json
import math
from types import SimpleNamespace

import pytest

from catia_mcp.connection import PLATFORM_3DX, CATIAConnection
from catia_mcp.errors import CATIAToolError, InvalidArgument, ObjectNotFound, UnsupportedCapability
from catia_mcp.tools.drawing_annotate import DrawingAnnotateTools
from tests.fakes import App3DX, Coll, PLMObject, Selection

# ── doubles ──


class DimValue:
    def __init__(self, value: float = 50.0) -> None:
        self.Value = value
        self.precision = 0.01
        self.ps: list[tuple] = []
        self.ignore_precision = False

    def SetFormatPrecision(self, mode, precision):
        assert mode == 1
        if not self.ignore_precision:
            self.precision = precision

    def GetFormatPrecision(self, mode):
        return self.precision

    def SetPSText(self, *args):
        self.ps.append(args)


class Dim:
    def __init__(self, name: str, *, stuck_frame: int | None = None) -> None:
        self.Name = name
        self._frame = 0
        self.stuck_frame = stuck_frame
        self.value = DimValue()

    @property
    def ValueFrame(self):
        return self._frame

    @ValueFrame.setter
    def ValueFrame(self, code):
        self._frame = self.stuck_frame if self.stuck_frame is not None else code

    def GetValue(self):
        return self.value


class Text:
    def __init__(self, text: str, x: float, y: float, name: str) -> None:
        self.Text, self.x, self.y, self.Name = text, x, y, name
        self.FrameType = 0
        self.font_sizes: list[tuple] = []
        self.font_size = 3.5

    def SetFontSize(self, *args):
        self.font_sizes.append(args)

    def GetFontSize(self, *args):
        return self.font_size


class Texts(Coll):
    def __init__(self, items=None) -> None:
        super().__init__(items)
        self.counter = 100

    def Add(self, text, x, y):
        self.counter += 1
        t = Text(text, x, y, f"Text.{self.counter}")
        self.append(t)
        return t


class Cell:
    def __init__(self) -> None:
        self.font_names: list[tuple] = []
        self.font_sizes: list[tuple] = []

    def SetFontName(self, *args):
        self.font_names.append(args)

    def SetFontSize(self, *args):
        self.font_sizes.append(args)

    def GetFontName(self, *args):
        return "SOURCE FONT"

    def GetFontSize(self, *args):
        return 2.5


class Table:
    def __init__(self, name, x, y, rows, cols, row_height, col_width) -> None:
        self.Name, self.x, self.y = name, x, y
        self.NumberOfRows, self.NumberOfColumns = rows, cols
        self.row_height = row_height
        self.widths = {c: col_width for c in range(1, cols + 1)}
        self.cells: dict[tuple[int, int], str] = {}
        self.cell_objects: dict[tuple[int, int], Cell] = {}

    def SetColumnSize(self, c, w):
        self.widths[c] = w

    def GetColumnSize(self, c):
        return self.widths[c]

    def GetRowSize(self, r):
        return self.row_height

    def SetCellString(self, r, c, s):
        self.cells[(r, c)] = s

    def GetCellString(self, r, c):
        return self.cells.get((r, c), "")

    def GetCellObject(self, r, c):
        return self.cell_objects.setdefault((r, c), Cell())


class Tables(Coll):
    def __init__(self, items=None) -> None:
        super().__init__(items)
        self.calls: list[tuple] = []

    def Add(self, *args):
        self.calls.append(args)
        t = Table(f"Table.{len(self.items) + 1}", *args)
        self.append(t)
        return t


def make_view(name, *, x=0.0, y=0.0, ox=0.0, oy=0.0, scale=1.0, angle=0.0, dims=(), texts=(), tables=()):
    return SimpleNamespace(
        Name=name, x=x, y=y, xAxisData=ox, yAxisData=oy, Scale=scale, Angle=angle,
        Dimensions=Coll(list(dims)), Texts=Texts(list(texts)), Tables=Tables(list(tables)),
    )


class DeletingSelection(Selection):
    def __init__(self, views) -> None:
        super().__init__()
        self.views = views
        self.deleted: list = []

    def Delete(self):
        for obj in self.items:
            for view in self.views:
                for coll in (view.Texts, view.Tables):
                    if obj in coll.items:
                        coll.items.remove(obj)
            self.deleted.append(obj)
        self.items = []


def setup(*user_views):
    views = [make_view("View.1"), make_view("View.2"), *user_views]
    app = App3DX()
    sheet = SimpleNamespace(Name="Sheet.1", Views=Coll(views))
    sheets = Coll([sheet])
    sheets.ActiveSheet = sheet
    rep = PLMObject(V_Name="100200 BRACKET", PLM_ExternalID="dwg-1")
    drawing = SimpleNamespace(Name="100200 BRACKET", Sheets=sheets, Parent=rep)
    editor = app.open_editor(drawing, "100200 BRACKET A.1")
    editor.Selection = DeletingSelection(views)
    conn = CATIAConnection(platform=PLATFORM_3DX)
    conn.app = app
    conn.platform = PLATFORM_3DX
    return DrawingAnnotateTools(conn), editor, rep


def run(tools, name, args):
    return json.loads(tools.execute(name, args))


# ── definitions ──


def test_definitions_are_write_tools_with_valid_schemas():
    from jsonschema import Draft202012Validator

    tools, _, _ = setup()
    defs = {d["name"]: d for d in tools.get_tool_definitions()}
    assert set(defs) == {
        "catia_drawing_set_dimension_frame", "catia_drawing_add_flag",
        "catia_drawing_add_datum", "catia_drawing_add_fcf",
    }
    for d in defs.values():
        Draft202012Validator.check_schema(d["inputSchema"])
        assert d["annotations"]["readOnlyHint"] is False
    assert "basic" in defs["catia_drawing_set_dimension_frame"]["inputSchema"]["properties"]["frame"]["enum"]


# ── 1. dimension frames ──


def test_basic_frame_is_rectangle_code_5_not_circle():
    d1, d2 = Dim("Dim.1"), Dim("Dim.2")
    tools, _, _ = setup(make_view("Front View", dims=[d1, d2]))
    data = run(tools, "catia_drawing_set_dimension_frame",
               {"view_name": "Front View", "dimension": [1, "Dim.2"], "frame": "basic", "precision": 0.001})
    assert d1.ValueFrame == 5 and d2.ValueFrame == 5  # 1 would draw a circle
    assert d1.value.precision == 0.001 and d2.value.precision == 0.001
    assert data["value_frame"] == 5
    assert [r["frame"] for r in data["dimensions"]] == [5, 5]
    assert [r["precision"] for r in data["dimensions"]] == [0.001, 0.001]


@pytest.mark.parametrize(("frame", "code"), [("circle", 1), ("square", 4), ("rectangle", 5), ("none", 0)])
def test_frame_names_map_to_catdimframe(frame, code):
    dim = Dim("Dim.1")
    dim._frame = 3
    tools, _, _ = setup(make_view("Front View", dims=[dim]))
    run(tools, "catia_drawing_set_dimension_frame", {"view_name": "Front View", "dimension": 1, "frame": frame})
    assert dim.ValueFrame == code


def test_reference_frame_wraps_value_in_parentheses():
    dim = Dim("Dim.1")
    dim._frame = 5
    tools, _, _ = setup(make_view("Front View", dims=[dim]))
    data = run(tools, "catia_drawing_set_dimension_frame",
               {"view_name": "Front View", "dimension": "Dim.1", "frame": "reference"})
    assert dim.ValueFrame == 0
    assert dim.value.ps == [(1, "(", ")")]
    assert "unverified" in data["note"]


def test_frame_that_does_not_take_fails_typed():
    dim = Dim("Dim.1", stuck_frame=1)
    tools, _, _ = setup(make_view("Front View", dims=[dim]))
    with pytest.raises(CATIAToolError) as err:
        tools.execute("catia_drawing_set_dimension_frame",
                      {"view_name": "Front View", "dimension": 1, "frame": "basic"})
    assert err.value.code == "WRONG_GEOMETRY_EFFECT"
    assert err.value.modified is None


def test_precision_that_does_not_take_fails_typed():
    dim = Dim("Dim.1")
    dim.value.ignore_precision = True
    tools, _, _ = setup(make_view("Front View", dims=[dim]))
    with pytest.raises(CATIAToolError) as err:
        tools.execute("catia_drawing_set_dimension_frame",
                      {"view_name": "Front View", "dimension": 1, "frame": "basic", "precision": 0.001})
    assert err.value.code == "WRONG_GEOMETRY_EFFECT"


def test_unknown_dimension_or_frame_changes_nothing():
    d1 = Dim("Dim.1")
    tools, _, _ = setup(make_view("Front View", dims=[d1]))
    with pytest.raises(ObjectNotFound):
        tools.execute("catia_drawing_set_dimension_frame",
                      {"view_name": "Front View", "dimension": [1, "Dim.9"], "frame": "basic"})
    assert d1.ValueFrame == 0  # resolved all before writing any
    with pytest.raises(InvalidArgument):
        tools.execute("catia_drawing_set_dimension_frame",
                      {"view_name": "Front View", "dimension": 1, "frame": "oval"})


def _fake_ps(monkeypatch, store):
    """GetPSText out-params read from `store[id(value)]` (prefix, suffix)."""
    import catia_mcp.tools.drawing_annotate as mod

    def fake(app, obj, method, n_out, *in_args):
        assert (method, n_out, in_args) == ("GetPSText", 2, (1,))
        return list(store.get(id(obj), ("", "")))

    monkeypatch.setattr(mod, "call_out_values", fake)


def test_reference_keeps_existing_prefix_and_reads_back(monkeypatch):
    dim = Dim("Dim.1")
    store = {id(dim.value): ("Ø", "")}
    dim.value.SetPSText = lambda i, p, s: (dim.value.ps.append((i, p, s)), store.__setitem__(id(dim.value), (p, s)))
    _fake_ps(monkeypatch, store)
    tools, _, _ = setup(make_view("Front View", dims=[dim]))
    data = run(tools, "catia_drawing_set_dimension_frame",
               {"view_name": "Front View", "dimension": 1, "frame": "reference"})
    assert dim.value.ps == [(1, "(Ø", ")")]
    assert data["dimensions"][0]["prefix"] == "(Ø" and data["warnings"] == []


def test_reference_prefix_that_does_not_take_fails_typed(monkeypatch):
    dim = Dim("Dim.1")
    _fake_ps(monkeypatch, {id(dim.value): ("", "")})  # SetPSText silently ignored
    tools, _, _ = setup(make_view("Front View", dims=[dim]))
    with pytest.raises(CATIAToolError) as err:
        tools.execute("catia_drawing_set_dimension_frame",
                      {"view_name": "Front View", "dimension": 1, "frame": "reference"})
    assert err.value.code == "WRONG_GEOMETRY_EFFECT"


def test_leaving_reference_warns_about_parentheses(monkeypatch):
    dim = Dim("Dim.1")
    _fake_ps(monkeypatch, {id(dim.value): ("(", ")")})
    tools, _, _ = setup(make_view("Front View", dims=[dim]))
    data = run(tools, "catia_drawing_set_dimension_frame",
               {"view_name": "Front View", "dimension": 1, "frame": "basic"})
    assert dim.ValueFrame == 5
    assert any("parentheses" in w for w in data["warnings"])


def test_precision_read_back_tolerates_float_noise():
    dim = Dim("Dim.1")
    dim.value.GetFormatPrecision = lambda mode: 0.0010000000474974513  # float32 round trip
    tools, _, _ = setup(make_view("Front View", dims=[dim]))
    run(tools, "catia_drawing_set_dimension_frame",
        {"view_name": "Front View", "dimension": 1, "frame": "basic", "precision": 0.001})


# ── 2. flags ──


def test_add_flag_converts_sheet_to_view_from_axis_origin_not_view_xy():
    # view.x / view.y hundreds of mm away from the axis origin (the field case)
    rear = make_view("REAR", x=600.0, y=400.0, ox=200.0, oy=100.0, scale=2.0)
    tools, _, _ = setup(rear)
    data = run(tools, "catia_drawing_add_flag",
               {"view_name": "REAR", "text": "5", "sheet_x": 260.0, "sheet_y": 140.0, "font_size": 3.5})
    text = rear.Texts.items[-1]
    assert (text.x, text.y) == (30.0, 20.0)  # (260-200)/2, (140-100)/2
    assert text.FrameType == 5  # diamond in the TEXT enum
    assert text.font_sizes == [(0, 0, 3.5)]
    assert data["position"]["sheet_x"] == 260.0 and data["position"]["sheet_y"] == 140.0
    assert data["frame_type"] == 5 and data["name"] == text.Name


def test_add_flag_circle_and_name():
    view = make_view("Front View", ox=10.0, oy=10.0)
    tools, _, _ = setup(view)
    data = run(tools, "catia_drawing_add_flag",
               {"view_name": "Front View", "text": "1", "sheet_x": 10, "sheet_y": 10,
                "shape": "circle", "name": "Balloon.7"})
    assert view.Texts.items[-1].FrameType == 3
    assert data["name"] == "Balloon.7" and data["warnings"] == []


def test_add_flag_refuses_rotated_view_and_missing_axis():
    rotated = make_view("Front View", angle=math.pi / 2)
    tools, _, _ = setup(rotated)
    with pytest.raises(UnsupportedCapability):
        tools.execute("catia_drawing_add_flag",
                      {"view_name": "Front View", "text": "5", "sheet_x": 1, "sheet_y": 1})
    assert rotated.Texts.Count == 0
    bare = SimpleNamespace(Name="Old View", x=0.0, y=0.0, Scale=1.0, Texts=Texts())
    tools, _, _ = setup(bare)
    with pytest.raises(UnsupportedCapability):
        tools.execute("catia_drawing_add_flag",
                      {"view_name": "Old View", "text": "5", "sheet_x": 1, "sheet_y": 1})
    assert bare.Texts.Count == 0


def test_add_flag_frame_not_taken_is_rolled_back():
    view = make_view("Front View")

    class StuckText(Text):
        def __setattr__(self, key, value):
            if key == "FrameType" and hasattr(self, "FrameType"):
                return  # silently ignored
            super().__setattr__(key, value)

    def add(text, x, y):
        t = StuckText(text, x, y, "Text.1")
        view.Texts.append(t)
        return t

    view.Texts.Add = add
    tools, editor, _ = setup(view)
    with pytest.raises(CATIAToolError) as err:
        tools.execute("catia_drawing_add_flag",
                      {"view_name": "Front View", "text": "5", "sheet_x": 1, "sheet_y": 1})
    assert err.value.code == "WRONG_GEOMETRY_EFFECT"
    assert err.value.modified is False
    assert view.Texts.Count == 0 and len(editor.Selection.deleted) == 1


# ── 3. datums ──


def test_add_datum_uses_next_free_number_and_rectangle_frame():
    existing_a = Text("A", 0, 0, "Datum Feature.1")
    existing_a.font_size = 5.0
    other = make_view("Top View", texts=[existing_a, Text("B", 0, 0, "Datum Feature.3")])
    front = make_view("Front View", ox=100.0, oy=50.0)
    tools, _, _ = setup(other, front)
    data = run(tools, "catia_drawing_add_datum",
               {"view_name": "Front View", "letter": "c", "sheet_x": 110, "sheet_y": 60})
    text = front.Texts.items[-1]
    assert text.Name == "Datum Feature.4"
    assert text.Text == "C" and text.FrameType == 1
    assert (text.x, text.y) == (10.0, 10.0)
    assert text.font_sizes == [(0, 0, 5.0)]  # copied from the existing datum
    assert data["name"] == "Datum Feature.4" and data["warnings"] == []
    assert "no automation object" in data["note"]


def test_add_datum_warns_on_duplicate_letter():
    tools, _, _ = setup(make_view("Front View", texts=[Text("A", 0, 0, "Datum Feature.1")]))
    data = run(tools, "catia_drawing_add_datum",
               {"view_name": "Front View", "letter": "A", "sheet_x": 1, "sheet_y": 1})
    assert data["name"] == "Datum Feature.2"
    assert any("already exists" in w for w in data["warnings"])


def test_add_datum_ignores_unusable_copied_font_size():
    existing = Text("A", 0, 0, "Datum Feature.1")
    existing.font_size = 0.0
    front = make_view("Front View")
    tools, _, _ = setup(make_view("Top View", texts=[existing]), front)
    data = run(tools, "catia_drawing_add_datum",
               {"view_name": "Front View", "letter": "B", "sheet_x": 1, "sheet_y": 1})
    assert front.Texts.items[-1].font_sizes == []
    assert data["font_size"] is None and any("font size" in w for w in data["warnings"])


# ── 4. feature control frames ──


def test_add_fcf_builds_table_with_glyphs_and_font():
    view = make_view("Front View", ox=50.0, oy=20.0, scale=0.5)
    tools, _, _ = setup(view)
    data = run(tools, "catia_drawing_add_fcf",
               {"view_name": "Front View", "symbol": "position", "tolerance": "1", "diameter": True,
                "datums": ["B", "C"], "sheet_x": 60, "sheet_y": 25})
    table = view.Tables.items[-1]
    x, y, rows, cols, *_ = view.Tables.calls[-1]
    assert (x, y, rows, cols) == (20.0, 10.0, 1, 4)
    assert [table.cells[(1, c)] for c in range(1, 5)] == ["⌖", "Ø1", "B", "C"]
    cell = table.cell_objects[(1, 1)]
    assert cell.font_names == [(0, 0, "DS ISO 1 (OpenType)")]
    assert cell.font_sizes == [(0, 0, 3.5)]
    assert data["frame"] == "⌖ | Ø1 | B | C"
    assert data["position"]["sheet_x"] == 60.0


@pytest.mark.parametrize(("symbol", "glyph"), [
    ("profile_surface", "⌓"), ("flatness", "⏥"), ("perpendicularity", "⊥"),
    ("parallelism", "∥"), ("total_runout", "⌰"),
])
def test_fcf_symbol_glyphs(symbol, glyph):
    view = make_view("Front View")
    tools, _, _ = setup(view)
    run(tools, "catia_drawing_add_fcf",
        {"view_name": "Front View", "symbol": symbol, "tolerance": "5", "datums": ["A"], "sheet_x": 0, "sheet_y": 0})
    assert view.Tables.items[-1].cells[(1, 1)] == glyph
    assert view.Tables.items[-1].cells[(1, 2)] == "5"


def test_fcf_style_from_copies_widths_row_height_and_font():
    source = Table("Table.9", 0, 0, 1, 5, 6.0, 0)
    source.widths = {1: 6.0, 2: 12.0, 3: 5.0, 4: 5.0, 5: 5.0}
    note_view = make_view("background", tables=[source])
    note_view.Name = "View.2b"
    front = make_view("Front View")
    tools, _, _ = setup(note_view, front)
    data = run(tools, "catia_drawing_add_fcf",
               {"view_name": "Front View", "symbol": "profile_surface", "tolerance": "5",
                "datums": ["A", "B"], "sheet_x": 0, "sheet_y": 0, "style_from": "Table.9"})
    table = front.Tables.items[-1]
    assert [table.widths[c] for c in range(1, 5)] == [6.0, 12.0, 5.0, 5.0]
    assert data["row_height"] == 6.0
    assert data["font"] == {"name": "SOURCE FONT", "size": 2.5}
    with pytest.raises(ObjectNotFound):
        tools.execute("catia_drawing_add_fcf",
                      {"view_name": "Front View", "symbol": "position", "tolerance": "1",
                       "sheet_x": 0, "sheet_y": 0, "style_from": "Table.404"})


def test_fcf_rejects_bad_arguments_before_writing():
    view = make_view("Front View")
    tools, _, _ = setup(view)
    for bad in (
        {"symbol": "wobble", "tolerance": "1"},
        {"symbol": "position", "tolerance": ""},
        {"symbol": "position", "tolerance": "1", "datums": ["A", "B", "C", "D"]},
    ):
        with pytest.raises(InvalidArgument):
            tools.execute("catia_drawing_add_fcf", {"view_name": "Front View", "sheet_x": 0, "sheet_y": 0, **bad})
    assert view.Tables.calls == []


def test_fcf_all_around_adds_separate_circle_text():
    view = make_view("Front View")
    tools, _, _ = setup(view)
    data = run(tools, "catia_drawing_add_fcf",
               {"view_name": "Front View", "symbol": "profile_surface", "tolerance": "1",
                "datums": ["A"], "sheet_x": 10, "sheet_y": 10, "all_around": True})
    assert data["all_around_text"] == view.Texts.items[-1].Name
    assert view.Texts.items[-1].Text == "○"


# ── write guard ──


def test_write_target_delegates_to_drafting():
    tools, _, rep = setup(make_view("Front View"))
    for name in ("catia_drawing_add_flag", "catia_drawing_set_dimension_frame"):
        assert tools.write_target(name, {"view_name": "Front View"}) is rep
    calls = []
    tools.drafting.write_target = lambda name, args: calls.append((name, args)) or "sentinel"
    assert tools.write_target("catia_drawing_add_fcf", {"x": 1}) == "sentinel"
    assert calls == [("catia_drawing_add_fcf", {"x": 1})]
