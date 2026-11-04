"""Offline tests for catia_drawing_read and catia_mcp.drawing_model.

The facts encoded here are field-proven on R428 SP6 (field reports
2026-11-03/04, kept outside the repository): frame enums, axis-origin text
coordinates, both title-block layouts, notes in the background view, FCFs as
plain tables. The doubles prove orchestration and parsing only, not live
CATIA behaviour. Every double records attribute writes and any access to a
member it does not define, so the tests can prove the tool never mutates.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from catia_mcp import drawing_model
from catia_mcp.connection import PLATFORM_3DX, CATIAConnection
from catia_mcp.errors import InvalidArgument, ObjectNotFound, UnsupportedCapability
from catia_mcp.tools.drawing_read import (
    DrawingReadTools,
    parse_numbered_notes,
    parse_title_block_cells,
    parse_title_block_texts,
)
from tests.fakes import App3DX, Coll, PLMObject, Selection

MUTATIONS: list[str] = []
_SAFE_PROBES = {"ReferenceRootOccurrenceOf", "InstanceOccurrenceOf", "ReferenceInstanceOf", "Instances",
                "RepInstances", "GetAttributeValue", "ActiveSheet", "Angle", "GenerativeBehavior"}


class Strict:
    """Records every attribute write after construction and every access to
    an undefined member (an Update, Set*, Delete... the tool must not call)."""

    def __init__(self, **attrs) -> None:
        object.__setattr__(self, "_frozen", False)
        for key, value in attrs.items():
            setattr(self, key, value)
        object.__setattr__(self, "_frozen", True)

    def __setattr__(self, key, value) -> None:
        if self._frozen:
            MUTATIONS.append(f"set {type(self).__name__}.{key}")
        object.__setattr__(self, key, value)

    def __getattr__(self, key):
        if key.startswith("__") or key in _SAFE_PROBES:
            raise AttributeError(key)
        MUTATIONS.append(f"access {type(self).__name__}.{key}")
        raise AttributeError(key)


class Text(Strict):
    def __init__(self, name, text, x=0.0, y=0.0, frame=0) -> None:
        super().__init__(Name=name, Text=text, x=x, y=y, FrameType=frame)


class Table(Strict):
    def __init__(self, name, cells, x=0.0, y=0.0) -> None:
        super().__init__(
            Name=name, cells=cells, x=x, y=y,
            NumberOfRows=len(cells), NumberOfColumns=max(len(r) for r in cells),
        )

    def GetCellString(self, r, c):
        row = self.cells[r - 1]
        return row[c - 1] if c <= len(row) else ""


class DimValue(Strict):
    def __init__(self, value, precision, bault, ps) -> None:
        super().__init__(Value=value, precision=precision, bault=bault, ps=ps)

    def GetFormatPrecision(self, index):
        assert index == 1
        return self.precision

    def _outs_GetBaultText(self, index):
        assert index == 1
        return list(self.bault)

    def _outs_GetPSText(self, index):
        return list(self.ps)


class Dim(Strict):
    def __init__(self, name, dim_type, value, frame=0, precision=0.1, bault=("", "", "", ""), ps=("", "")) -> None:
        super().__init__(
            Name=name, DimType=dim_type, DimStatus=7, ValueFrame=frame,
            value=DimValue(value, precision, bault, ps),
        )

    def GetValue(self):
        return self.value


class View(Strict):
    def __init__(self, name, *, x=0.0, y=0.0, x0=0.0, y0=0.0, scale=1.0, texts=(), tables=(), dims=(),
                 generative=False, document=None) -> None:
        attrs = {
            "Name": name, "x": x, "y": y, "xAxisData": x0, "yAxisData": y0, "Scale": scale, "Angle": 0.0,
            "Texts": Coll(list(texts)), "Tables": Coll(list(tables)), "Dimensions": Coll(list(dims)),
        }
        if document is not None:
            attrs["GenerativeBehavior"] = SimpleNamespace(Document=document)
        super().__init__(**attrs)
        object.__setattr__(self, "_generative", generative)

    def IsGenerative(self):
        return self._generative


class RecordingSelection(Selection):
    def __getattribute__(self, key):
        if key in {"Clear", "Add", "Search", "Copy", "Paste", "Delete"}:
            MUTATIONS.append(f"selection.{key}")
        return object.__getattribute__(self, key)


@pytest.fixture(autouse=True)
def _reset():
    MUTATIONS.clear()
    yield


def make_app(views, *, title="100200 BRACKET DWG", sheet_name="Sheet.1", extra_sheets=()):
    app = App3DX()
    sheet = SimpleNamespace(
        Name=sheet_name, Views=Coll(views), PaperSize=2, Orientation=1, ProjectionMethod=1, Scale=1.0,
        GetPaperWidth=lambda: 1189.0, GetPaperHeight=lambda: 841.0,
    )
    sheets = Coll([sheet, *extra_sheets])
    sheets.ActiveSheet = sheet
    drawing = SimpleNamespace(
        Name=title, Sheets=sheets,
        Parent=PLMObject(V_Name=title, PLM_ExternalID="dwg-0001", current="IN_WORK", owner="jane.doe"),
    )
    editor = app.open_editor(drawing, title)
    editor.Selection = RecordingSelection()
    return app


def tools(app) -> DrawingReadTools:
    conn = CATIAConnection(platform=PLATFORM_3DX)
    conn.app = app
    conn.platform = PLATFORM_3DX
    return DrawingReadTools(conn)


def read(app, **args) -> dict:
    result = tools(app).execute("catia_drawing_read", args)
    assert result["ok"] and result["effects"]["modified"] is False, result
    return result["data"]


# ── a drawing on the newer template ──

NEW_TITLE_BLOCK = [
    ["TITLE:", "DWG NO:", "REV:"],
    ["100200 BRACKET", "100200", "C"],
    ["DRAWN:", "SCALE:", "SHEET:"],
    ["jane.doe", "2:1", ""],
]
REVISIONS = [
    ["REVISIONS", "", ""],
    ["REV", "DESCRIPTION", "DATE"],
    ["A", "INITIAL RELEASE", "2026-10-01"],
    ["B", "UPDATED NOTES", "2026-11-01"],
]


def newer_template_drawing():
    background = View(
        "View.2",
        tables=[Table("Title Block", NEW_TITLE_BLOCK, x=900, y=20), Table("Revisions", REVISIONS)],
        texts=[
            Text("Notes", "NOTES:\n1. BREAK ALL SHARP EDGES.\n2. ANODIZE CLEAR PER SPEC-001\n   AFTER MACHINING."),
            Text("Note 3", "3. DEBURR."),
        ],
    )
    main = View(
        "View.1",
        texts=[Text("Note 4", "4. MARK PART NUMBER.")],
        tables=[Table("BOM", [["ITEM", "PART NUMBER", "QTY"], ["1", "100201 PIN", "2"]])],
    )
    front = View(
        "Front View", x=200.0, y=150.0, x0=210.0, y0=160.0, scale=2.0, generative=True,
        document=PLMObject(V_Name="100200 BRACKET"),
        texts=[
            Text("Text.5", "2", x=5.0, y=-10.0, frame=5),
            Text("Balloon.1", "1", x=-20.0, y=0.0, frame=3),
            Text("Datum Feature.1", "A", x=0.0, y=30.0, frame=1),
        ],
        tables=[Table("Table.7", [["⌖", "Ø0.2", "A", "B", "C"]])],
        dims=[
            Dim("Dim.1", 9, 12.0, frame=5, precision=0.001, bault=("2X", "THRU", "", ""), ps=("(", ")")),
            Dim("Dim.2", 4, math.radians(90), frame=1),
        ],
    )
    return make_app([main, background, front])


def test_definition_is_read_only():
    (definition,) = tools(App3DX()).get_tool_definitions()
    assert definition["name"] == "catia_drawing_read"
    assert definition["annotations"]["readOnlyHint"] is True


def test_newer_template_title_block_value_below_label():
    data = read(newer_template_drawing())
    tb = data["title_block"]
    assert tb["found"]
    assert tb["fields"]["TITLE"] == "100200 BRACKET"
    assert tb["fields"]["DWG NO"] == "100200"
    assert tb["fields"]["REV"] == "C"
    assert tb["fields"]["DRAWN"] == "jane.doe"
    assert tb["fields"]["SCALE"] == "2:1"
    assert tb["fields"]["SHEET"] == ""  # unfilled field, not a missing one
    assert set(tb["layouts"].values()) == {"below"}
    assert tb["sources"] == [{"view": "View.2", "table": "Title Block"}]


def test_older_layout_title_block_value_in_next_cell():
    cells = [
        ["DRAWN", "jane.doe", "DATE", "2026-11-01"],
        ["TITLE", "100200 BRACKET", "SCALE", "1:1"],
        ["MATERIAL", "", "REV", "B"],
    ]
    app = make_app([View("View.1"), View("View.2", tables=[Table("TB", cells)])])
    tb = read(app, sections=["title_block"])["title_block"]
    assert tb["fields"] == {
        "DRAWN": "jane.doe", "DATE": "2026-11-01", "TITLE": "100200 BRACKET",
        "SCALE": "1:1", "MATERIAL": "", "REV": "B",
    }
    assert set(tb["layouts"].values()) == {"right"}


def test_colon_labels_with_values_to_the_right_still_parse():
    fields = parse_title_block_cells([["DRAWN:", "jane.doe"], ["CHECKED:", "john.doe"]])
    assert [(f["label"], f["value"], f["layout"]) for f in fields] == [
        ("DRAWN", "jane.doe", "right"), ("CHECKED", "john.doe", "right"),
    ]


def test_title_block_from_loose_texts():
    texts = [
        {"name": "t1", "text": "DRAWN:", "kind": "other", "sheet_x": 900.0, "sheet_y": 40.0},
        {"name": "t2", "text": "jane.doe", "kind": "other", "sheet_x": 901.0, "sheet_y": 34.0},
        {"name": "t3", "text": "SCALE: 2:1", "kind": "other", "sheet_x": 1000.0, "sheet_y": 40.0},
        {"name": "t4", "text": "PART NUMBER:", "kind": "other", "sheet_x": 800.0, "sheet_y": 80.0},
        {"name": "t5", "text": "100200", "kind": "other", "sheet_x": 840.0, "sheet_y": 80.5},
    ]
    got = {f["label"]: (f["value"], f["layout"]) for f in parse_title_block_texts(texts)}
    assert got == {
        "DRAWN": ("jane.doe", "text_below"),
        "SCALE": ("2:1", "text_inline"),
        "PART NUMBER": ("100200", "text_right"),
    }


def test_notes_from_background_and_main_view_in_order_with_flags():
    notes = read(newer_template_drawing(), sections=["notes"])["notes"]
    assert [(n["number"], n["view_role"]) for n in notes["notes"]] == [
        (1, "background"), (2, "background"), (3, "background"), (4, "main"),
    ]
    assert notes["notes"][1]["text"] == "ANODIZE CLEAR PER SPEC-001 AFTER MACHINING."
    assert [n["flagged"] for n in notes["notes"]] == [False, True, False, False]
    assert notes["flags"][0]["number"] == 2 and notes["flags"][0]["view"] == "Front View"
    assert notes["missing_numbers"] == [] and notes["duplicate_numbers"] == []


def test_numbered_note_parser_ignores_decimals_and_captions():
    assert parse_numbered_notes("NOTES:\n1. A\n1.5 MM IS NOT A NOTE\n2) B") == [(1, "A 1.5 MM IS NOT A NOTE"), (2, "B")]


def test_text_sheet_coordinates_use_axis_origin_not_view_position():
    data = read(newer_template_drawing(), sections=["texts"], view="Front View")
    (entry,) = data["texts"]
    flag = next(t for t in entry["texts"] if t["name"] == "Text.5")
    # (210 + 5*2, 160 - 10*2); view.x/view.y (200, 150) would give (210, 130).
    assert (flag["sheet_x"], flag["sheet_y"]) == (220.0, 140.0)
    assert flag["frame"] == "diamond" and flag["kind"] == "flag"
    kinds = {t["name"]: (t["kind"], t["frame"]) for t in entry["texts"]}
    assert kinds["Balloon.1"] == ("balloon", "circle")
    assert kinds["Datum Feature.1"] == ("datum_feature", "rectangle")


def test_sheet_view_conversion_round_trip():
    view = View("V", x0=210.0, y0=160.0, scale=2.0)
    assert drawing_model.sheet_to_view(view, 220.0, 140.0) == (5.0, -10.0)
    assert drawing_model.view_to_sheet(view, 5.0, -10.0) == (220.0, 140.0)
    with pytest.raises(UnsupportedCapability):
        drawing_model.sheet_to_view(View("Z", scale=0.0), 1, 1)


def test_frame_enums_differ_between_dimensions_and_texts():
    assert drawing_model.dim_frame_code("basic") == 5 == drawing_model.dim_frame_code("rectangle")
    assert drawing_model.dim_frame_name(1) == "circle"
    assert drawing_model.text_frame_name(5) == "diamond"
    assert drawing_model.text_frame_code("balloon") == 3
    assert drawing_model.dim_frame_name(7) == "unknown(7)"
    with pytest.raises(InvalidArgument):
        drawing_model.dim_frame_code("reference")


def test_dimension_frame_precision_and_bault_text():
    data = read(newer_template_drawing(), sections=["dimensions"], view="Front View")
    dims = data["dimensions"][0]["dimensions"]
    basic = dims[0]
    assert basic["dim_type_name"] == "diameter" and basic["value"] == 12.0
    assert basic["frame"] == "rectangle" and basic["basic"] is True
    assert basic["precision"] == 0.001
    assert basic["texts"] == {"before": "2X", "after": "THRU", "upper": "", "lower": ""}
    assert (basic["prefix"], basic["suffix"]) == ("(", ")")
    angle = dims[1]
    assert angle["value"] == 90.0 and angle["unit"] == "deg"
    assert angle["frame"] == "circle" and angle["basic"] is False


def test_fcf_table_detection_and_annotation_summary():
    data = read(newer_template_drawing(), sections=["tables", "annotations"])
    kinds = {t["name"]: t["kind"] for entry in data["tables"] for t in entry["tables"]}
    assert kinds == {"BOM": "bom", "Title Block": "title_block", "Revisions": "revision_table", "Table.7": "fcf"}
    ann = data["annotations"]
    assert ann["fcf_count"] == 1
    assert ann["fcf_tables"][0]["frame"] == "⌖ | Ø0.2 | A | B | C"
    assert ann["fcf_tables"][0]["datums"] == ["A", "B", "C"]
    assert ann["balloon_count"] == 1 and ann["flag_count"] == 1
    assert ann["flags"][0]["text"] == "2"
    assert ann["datum_letters"] == ["A"]


def test_revision_table_and_bom_rows_by_header():
    data = read(newer_template_drawing(), sections=["revision_table", "bom"])
    (rev,) = data["revision_table"]
    assert rev["header"] == ["REV", "DESCRIPTION", "DATE"]
    assert [r["REV"] for r in rev["rows"]] == ["A", "B"]
    (bom,) = data["bom"]
    assert bom["rows"] == [{"row": 2, "ITEM": "1", "PART NUMBER": "100201 PIN", "QTY": "2"}]


def test_sheets_views_and_drawing_identity():
    data = read(newer_template_drawing(), sections=["sheets", "views"])
    assert data["drawing"]["plm_id"] == "dwg-0001"
    (sheet,) = data["sheets"]
    assert sheet["paper_size"] == "A0" and sheet["orientation"] == "landscape"
    assert sheet["projection"] == "third_angle" and sheet["active"] is True
    roles = [(v["name"], v["role"]) for v in data["views"]]
    assert roles == [("View.1", "main"), ("View.2", "background"), ("Front View", "user")]
    front = data["views"][2]
    assert front["xAxisData"] == 210.0 and front["scale"] == 2.0
    assert front["generative"] is True and front["linked_document"] == "100200 BRACKET"


def test_nothing_is_modified():
    app = newer_template_drawing()
    read(app)
    assert MUTATIONS == []


def test_max_items_truncates_and_says_so():
    texts = [Text(f"T{i}", f"text {i}") for i in range(5)]
    app = make_app([View("View.1", texts=texts), View("View.2")])
    result = tools(app).execute("catia_drawing_read", {"sections": ["texts"], "max_items": 2})
    assert len(result["data"]["texts"][0]["texts"]) == 2
    assert result["data"]["truncated"] == [{"collection": "texts", "where": "View.1", "total": 5, "returned": 2}]
    assert result["warnings"]


def test_one_bad_element_does_not_fail_the_read():
    class Broken(Strict):
        @property
        def Text(self):
            raise RuntimeError("COM error")

    app = make_app([View("View.1", texts=[Broken(Name="Text.1", x=1.0, y=2.0, FrameType=0)]), View("View.2")])
    (row,) = read(app, sections=["texts"])["texts"][0]["texts"]
    assert row["name"] == "Text.1" and row["text"] is None and row["sheet_x"] == 1.0


def test_bad_arguments_are_typed_errors():
    app = newer_template_drawing()
    with pytest.raises(InvalidArgument):
        tools(app).execute("catia_drawing_read", {"sections": ["everything"]})
    with pytest.raises(InvalidArgument):
        tools(app).execute("catia_drawing_read", {"max_items": 0})
    with pytest.raises(ObjectNotFound):
        tools(app).execute("catia_drawing_read", {"view": "Nope"})
    with pytest.raises(ObjectNotFound):
        tools(app).execute("catia_drawing_read", {"sheet": "Nope"})


# ── review additions ──


def test_fcf_datums_when_symbol_and_tolerance_share_a_cell():
    app = make_app([View("View.1"), View("View.2"), View("Top", tables=[Table("T", [["⌖ Ø1", "B", "C Ⓜ"]])])])
    ann = read(app, sections=["annotations"])["annotations"]
    assert ann["fcf_tables"][0]["datums"] == ["B", "C"]
    assert ann["fcf_tables"][0]["symbol"] == "⌖"


def test_loose_text_title_block_ignores_notes_caption_and_note_bodies():
    texts = [
        {"name": "c", "text": "NOTES:", "kind": "other", "sheet_x": 50.0, "sheet_y": 200.0},
        {"name": "n", "text": "1. BREAK ALL SHARP EDGES.", "kind": "note", "sheet_x": 50.0, "sheet_y": 195.0},
        {"name": "l", "text": "DRAWN:", "kind": "other", "sheet_x": 900.0, "sheet_y": 40.0},
        {"name": "i", "text": "SCALE: 2:1", "kind": "other", "sheet_x": 901.0, "sheet_y": 35.0},
        {"name": "v", "text": "jane.doe", "kind": "other", "sheet_x": 902.0, "sheet_y": 30.0},
    ]
    got = {f["label"]: f["value"] for f in parse_title_block_texts(texts)}
    assert got == {"DRAWN": "jane.doe", "SCALE": "2:1"}


def test_rectangle_framed_letter_is_a_stand_in_datum():
    app = make_app([View("View.1"), View("View.2"), View("Top", texts=[Text("Text.9", "B", frame=1)])])
    ann = read(app, sections=["annotations"])["annotations"]
    assert ann["datum_letters"] == ["B"]


def test_unreadable_collection_is_reported_not_silently_empty():
    view = View("Top")
    object.__setattr__(view, "Tables", None)
    app = make_app([View("View.1"), View("View.2"), view])
    result = tools(app).execute("catia_drawing_read", {"sections": ["tables"], "view": "Top"})
    assert result["data"]["tables"] == [{"view": "Top", "tables": []}]
    assert any("tables of view 'Top'" in w for w in result["warnings"])


def test_axis_origin_read_once_per_view():
    reads: list[str] = []

    class CountingView(View):
        def __getattribute__(self, key):
            if key in {"xAxisData", "yAxisData", "Scale"}:
                reads.append(key)
            return object.__getattribute__(self, key)

    texts = [Text(f"T{i}", "x", x=float(i)) for i in range(5)]
    app = make_app([View("View.1"), View("View.2"), CountingView("Top", x0=10.0, scale=2.0, texts=texts)])
    rows = read(app, sections=["texts"], view="Top")["texts"][0]["texts"]
    assert [r["sheet_x"] for r in rows] == [10.0, 12.0, 14.0, 16.0, 18.0]
    assert reads.count("xAxisData") == 1


def test_v5_drawing_reads_and_missing_drawing_is_typed():
    from catia_mcp.connection import PLATFORM_V5
    from tests.fakes import SystemService

    sheet = SimpleNamespace(
        Name="Sheet.1", Views=Coll([View("Main View"), View("Background View", tables=[Table("TB", NEW_TITLE_BLOCK)])]),
        PaperSize=5, Orientation=1, ProjectionMethod=0, Scale=1.0,
        GetPaperWidth=lambda: 420.0, GetPaperHeight=lambda: 297.0,
    )
    sheets = Coll([sheet])
    sheets.ActiveSheet = sheet
    document = SimpleNamespace(Name="100200.CATDrawing", Sheets=sheets)
    app = SimpleNamespace(Caption="CATIA V5", ActiveDocument=document, Documents=Coll([document]),
                          SystemService=SystemService())
    conn = CATIAConnection(platform=PLATFORM_V5)
    conn.app = app
    conn.platform = PLATFORM_V5
    result = DrawingReadTools(conn).execute("catia_drawing_read", {"sections": ["sheets", "title_block"]})
    assert result["ok"] and result["effects"]["modified"] is False
    assert result["data"]["drawing"] == {"name": "100200.CATDrawing", "title": "100200.CATDrawing", "plm_id": None}
    assert result["data"]["sheets"][0]["paper_size"] == "A3"
    assert result["data"]["title_block"]["fields"]["TITLE"] == "100200 BRACKET"

    empty = SimpleNamespace(Caption="CATIA V5", ActiveDocument=None, Documents=Coll([]))
    conn.app = empty
    with pytest.raises(ObjectNotFound):
        DrawingReadTools(conn).execute("catia_drawing_read", {})
