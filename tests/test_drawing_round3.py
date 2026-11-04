"""Offline tests for the drafting fixes from the R428 SP6 field reports of
2026-11-03 and 2026-11-04 (kept outside the repository).

Covered: catia_drawing_apply_template (doubled paste cleaned up and counts
that match the background, duplicate source elements, title blocks kept in
the template's main view, repeated application refused), sheet coordinates
for texts and tables, the ZX orientation hint and up_axis, the section view
name fallback and catia_drawing_print. In-memory doubles only: these prove
orchestration, not live CATIA behaviour.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

import catia_mcp.tools.drawing as drawing_module
from catia_mcp.connection import PLATFORM_3DX, CATIAConnection
from catia_mcp.server import CATIAMCPServer
from catia_mcp.tools.drawing import DraftingTools, _sheet_to_view, _up_axis_angle
from tests.fakes import App3DX, Coll, PLMObject, Selection

# ── harness ──


def call(app, name, args=None):
    async def go():
        server = CATIAMCPServer()
        server.connection.app = app
        server.connection.platform = PLATFORM_3DX
        try:
            return await server.call_tool(name, args or {})
        finally:
            await server.close()

    return asyncio.run(go())


def text(result) -> str:
    return result.content[0].text


def payload(result) -> dict:
    assert not result.isError, text(result)
    return json.loads(text(result))


class Table:
    def __init__(self, name: str) -> None:
        self.Name = name
        self.AnchorPoint = 0
        self.x = self.y = 0.0


class Text:
    def __init__(self, name: str) -> None:
        self.Name = name
        self.Text = ""
        self.x = self.y = 0.0


def make_view(name, tables=(), texts=(), geometry=(), **extra):
    view = SimpleNamespace(
        Name=name, x=0.0, y=0.0, Scale=1.0, Tables=Coll(list(tables)), Texts=Coll(list(texts)),
        GeometricElements=Coll(list(geometry)), Dimensions=Coll(), IsGenerative=lambda: True,
    )
    for key, value in extra.items():
        setattr(view, key, value)
    return view


class DeletingSelection(Selection):
    def __init__(self, views) -> None:
        super().__init__()
        self.views = views
        self.deleted: list = []

    def Delete(self):
        for obj in self.items:
            for view in self.views:
                for coll in (view.Tables, view.Texts, view.GeometricElements, view.Dimensions):
                    if obj in coll.items:
                        coll.items.remove(obj)
            self.deleted.append(obj)
        self.items = []


def drawing_app(views, *, extra_editor=None, title="Drawing00000001"):
    app = App3DX()
    if extra_editor is not None:
        extra_editor(app)
    sheet = SimpleNamespace(
        Name="Sheet.1", Views=Coll(views), ProjectionMethod=1, PaperSize=2, Orientation=1, Scale=1.0,
        GetPaperWidth=lambda: 841.0, GetPaperHeight=lambda: 594.0,
    )
    sheets = Coll([sheet])
    sheets.ActiveSheet = sheet
    drawing = SimpleNamespace(Name=title, Sheets=sheets, Parent=PLMObject(V_Name=title))
    editor = app.open_editor(drawing, title)
    editor.Selection = DeletingSelection(views)
    return app, sheet, editor, drawing


def copy_of(obj):
    if isinstance(obj, Table):
        return Table(obj.Name)
    if isinstance(obj, Text):
        return Text(obj.Name)
    return SimpleNamespace(Name=obj.Name)


def kind_of(obj) -> str:
    return {Table: "Tables", Text: "Texts"}.get(type(obj), "GeometricElements")


def template_app(template_bg, template_main=None, target_bg=None):
    template_sheet = SimpleNamespace(
        Name="Sheet.1", Views=Coll([template_main or make_view("View.1"), template_bg]),
    )
    template = SimpleNamespace(
        Sheets=SimpleNamespace(ActiveSheet=template_sheet),
        Parent=PLMObject(V_Name="COMPANY DWG TEMPLATE"),
    )
    target_bg = target_bg or make_view("View.2")
    app, sheet, editor, _ = drawing_app(
        [make_view("View.1"), target_bg],
        extra_editor=lambda app: app.open_editor(template, "template"),
    )
    return app, sheet, editor, target_bg


@pytest.fixture(autouse=True)
def _reset_clipboard():
    Selection.shared_clipboard = None
    yield
    Selection.shared_clipboard = None


# ── 1. apply_template ──


def test_apply_template_removes_a_doubled_paste_and_reports_real_counts():
    """Field report 2026-11-03: the background ended up doubled while one
    copy was reported. Here CATIA pastes everything twice."""
    template_bg = make_view(
        "View.2", tables=[Table("TitleBlock"), Table("Table.2")],
        texts=[Text("Note.1")], geometry=[SimpleNamespace(Name=f"Line.{i}") for i in range(3)],
    )
    app, _sheet, editor, target_bg = template_app(template_bg)

    def doubled_paste(selected, clipboard):
        for _ in range(2):
            for obj in clipboard:
                getattr(target_bg, kind_of(obj)).append(copy_of(obj))

    editor.Selection.on_paste = doubled_paste
    data = payload(call(app, "catia_drawing_apply_template", {"template": "COMPANY"}))
    assert data["copied"] == {"tables": 2, "texts": 1, "geometry": 3}
    assert data["added_to_background"] == {"tables": 2, "texts": 1, "geometry": 3}
    assert data["duplicates_removed"] == {"tables": 2, "texts": 1, "geometry": 3}
    # Reported counts match the background.
    assert (target_bg.Tables.Count, target_bg.Texts.Count, target_bg.GeometricElements.Count) == (2, 1, 3)
    assert data["background_counts"]["after"] == {"tables": 2, "texts": 1, "geometry": 3}
    assert any("pasted more than was copied" in n for n in data["notes"])


def test_apply_template_never_deletes_pre_existing_items_and_dedupes_sources():
    note = Text("Note.1")
    # The same COM object listed twice by the collection.
    template_bg = make_view("View.2", texts=[note, note])
    own = Text("Mine")
    target_bg = make_view("View.2", texts=[own])
    app, _sheet, editor, target_bg = template_app(template_bg, target_bg=target_bg)
    pasted = []

    def paste(selected, clipboard):
        pasted.append(list(clipboard))
        for obj in clipboard:
            target_bg.Texts.append(copy_of(obj))

    editor.Selection.on_paste = paste
    data = payload(call(app, "catia_drawing_apply_template", {
        "template": "COMPANY", "include_tables": False, "include_geometry": False,
    }))
    assert pasted == [[note]]
    assert data["selected"] == {"texts": 1}
    assert data["added_to_background"] == {"tables": 0, "texts": 1, "geometry": 0}
    assert own in target_bg.Texts.items and target_bg.Texts.Count == 2
    assert "duplicates_removed" not in data
    assert any("listed twice" in n for n in data["notes"])


def test_apply_template_reports_missing_items():
    template_bg = make_view("View.2", texts=[Text("A"), Text("B")])
    app, _sheet, editor, target_bg = template_app(template_bg)
    editor.Selection.on_paste = lambda sel, clip: target_bg.Texts.append(copy_of(clip[0]))
    data = payload(call(app, "catia_drawing_apply_template", {"template": "COMPANY"}))
    assert data["added_to_background"]["texts"] == 1
    assert any("Fewer items arrived" in n and "texts: 1 missing" in n for n in data["notes"])


def test_apply_template_copies_main_view_title_block_and_anchors_it_inside_the_border():
    """Field report 2026-11-03: the site template keeps title block and
    revision table in its main view, off-sheet at negative y."""
    title, revisions = Table("TitleBlock"), Table("RevisionTable")
    title.y, revisions.y = -120.0, -195.0
    template_main = make_view("View.1", tables=[title, revisions, Table("Scratch")])
    template_bg = make_view("View.2", geometry=[SimpleNamespace(Name="Frame")])
    app, _sheet, editor, target_bg = template_app(template_bg, template_main=template_main)
    pastes = []

    def paste(selected, clipboard):
        pastes.append([o.Name for o in clipboard])
        for obj in clipboard:
            getattr(target_bg, kind_of(obj)).append(copy_of(obj))

    editor.Selection.on_paste = paste
    data = payload(call(app, "catia_drawing_apply_template", {
        "template": "COMPANY", "title_block_table": "title", "revision_table": "revision",
    }))
    assert pastes == [["Frame"], ["TitleBlock", "RevisionTable"]]  # 'Scratch' stays behind
    pasted_title, pasted_rev = target_bg.Tables.items
    # Default border offset 10 mm on the 841 x 594 sheet.
    assert (pasted_title.AnchorPoint, pasted_title.x, pasted_title.y) == (8, 831.0, 10.0)
    assert (pasted_rev.AnchorPoint, pasted_rev.x, pasted_rev.y) == (6, 831.0, 584.0)
    assert {a["from"] for a in data["anchored_tables"]} == {"main view"}
    assert data["added_to_background"] == {"tables": 2, "texts": 0, "geometry": 1}
    assert [t["role"] for t in data["main_view_tables"]] == ["title_block_table", "revision_table"]


def test_apply_template_anchor_uses_paper_size_enum_when_width_is_unreadable():
    sheet = SimpleNamespace(PaperSize=3, Orientation=1, GetPaperWidth=lambda: 1 / 0)
    assert DraftingTools._paper_size_mm(sheet) == (841.0, 594.0, "PaperSize/Orientation")


def test_apply_template_refuses_a_repeat_unless_allowed():
    template_bg = make_view("View.2", tables=[Table("TitleBlock")])
    target_bg = make_view("View.2", tables=[Table("TitleBlock")])
    app, _sheet, editor, target_bg = template_app(template_bg, target_bg=target_bg)
    editor.Selection.on_paste = lambda sel, clip: target_bg.Tables.append(copy_of(clip[0]))
    refused = call(app, "catia_drawing_apply_template", {"template": "COMPANY"})
    assert refused.structuredContent["code"] == "ALREADY_APPLIED"
    assert refused.structuredContent["effects"]["modified"] is False
    assert target_bg.Tables.Count == 1
    data = payload(call(app, "catia_drawing_apply_template", {"template": "COMPANY", "allow_repeat": True}))
    assert data["added_to_background"]["tables"] == 1 and target_bg.Tables.Count == 2


def _texted(name: str, value: str) -> Text:
    item = Text(name)
    item.Text = value
    return item


def test_apply_template_refuses_a_repeat_detected_by_text_content():
    """Default table names prove nothing; the template's texts plus counts do
    (field report 2026-11-03: +9 reported on a background then holding 18,
    so one copy was already there before the call)."""
    template_bg = make_view(
        "View.2", tables=[Table("Table.1")],
        texts=[_texted("Text.1", "DRAWN BY"), _texted("Text.2", "SCALE")],
    )
    target_bg = make_view(
        "View.2", tables=[Table("Table.4")],
        texts=[_texted("Text.7", "DRAWN BY"), _texted("Text.8", "SCALE")],
    )
    app, _sheet, editor, target_bg = template_app(template_bg, target_bg=target_bg)
    editor.Selection.on_paste = lambda sel, clip: None
    refused = call(app, "catia_drawing_apply_template", {"template": "COMPANY"})
    assert refused.structuredContent["code"] == "ALREADY_APPLIED"
    assert target_bg.Texts.Count == 2

    # Same texts but fewer items than the template: not a repeat.
    target_bg.Tables.items.clear()
    pasted = []
    editor.Selection.on_paste = lambda sel, clip: pasted.append(len(clip))
    result = call(app, "catia_drawing_apply_template", {"template": "COMPANY"})
    assert pasted == [3], text(result)


# ── 3. Sheet coordinates ──


def test_sheet_to_view_uses_the_axis_origin_and_scale():
    view = SimpleNamespace(Name="V", x=500.0, y=400.0, xAxisData=100.0, yAxisData=50.0, Scale=2.0, Angle=0.0)
    assert _sheet_to_view(view, 120.0, 70.0) == (10.0, 10.0, [])


def test_add_text_converts_sheet_coordinates():
    added = []

    def add(value, x, y):
        added.append((value, x, y))
        return SimpleNamespace(Name="Text.9")

    rear = make_view("REAR", x=600.0, y=300.0, xAxisData=580.0, yAxisData=280.0, Scale=0.5, Angle=0.0)
    rear.Texts.Add = add
    app, *_ = drawing_app([make_view("View.1"), make_view("View.2"), rear])
    result = call(app, "catia_drawing_add_text", {
        "text": "5", "x": 590, "y": 290, "coords": "sheet", "view_name": "REAR",
    })
    assert not result.isError, text(result)
    assert added == [("5", 20.0, 20.0)]
    assert "from sheet (590, 290)" in text(result)
    # Default stays view coordinates.
    call(app, "catia_drawing_add_text", {"text": "6", "x": 1, "y": 2, "view_name": "REAR"})
    assert added[-1] == ("6", 1.0, 2.0)


def test_edit_text_and_add_table_convert_sheet_coordinates():
    note = Text("Text.1")
    tables = []

    def add_table(x, y, rows, cols, rh, cw):
        tables.append((x, y))
        return SimpleNamespace(Name="Table.1", SetColumnSize=lambda *a: None, SetCellString=lambda *a: None)

    view = make_view("DETAIL", texts=[note], xAxisData=200.0, yAxisData=100.0, Scale=2.0, Angle=0.0)
    view.Tables.Add = add_table
    app, *_ = drawing_app([make_view("View.1"), make_view("View.2"), view])
    result = call(app, "catia_drawing_edit_text", {
        "view_name": "DETAIL", "text_index": 1, "x": 220, "coords": "sheet",
    })
    assert not result.isError, text(result)
    assert (note.x, note.y) == (10.0, 0.0)
    result = call(app, "catia_drawing_add_table", {
        "view_name": "DETAIL", "x": 210, "y": 110, "rows": 1, "columns": 1, "coords": "sheet",
    })
    assert not result.isError, text(result)
    assert tables == [(5.0, 5.0)]


def test_sheet_coordinates_refused_without_axis_origin():
    view = make_view("OLD")  # no xAxisData
    app, *_ = drawing_app([make_view("View.1"), make_view("View.2"), view])
    result = call(app, "catia_drawing_add_text", {"text": "x", "x": 1, "y": 1, "coords": "sheet"})
    assert result.structuredContent["code"] == "UNSUPPORTED_CAPABILITY"
    assert view.Texts.Count == 0


def test_rotated_view_conversion_warns():
    view = SimpleNamespace(Name="V", xAxisData=0.0, yAxisData=0.0, Scale=1.0, Angle=math.pi / 2)
    _, _, notes = _sheet_to_view(view, 1.0, 1.0)
    assert notes and "rotated" in notes[0]


# ── 2. View orientation ──


def test_up_axis_maps_to_the_angle():
    assert _up_axis_angle("zx", "z", None)[0] == 90.0
    assert _up_axis_angle("zx", "x", None)[0] == 0.0
    assert _up_axis_angle("yz", "z", None) == (0.0, [])
    angle, notes = _up_axis_angle("zx", None, None)
    assert angle is None and "HORIZONTAL" in notes[0] and "up_axis='z'" in notes[0]
    assert _up_axis_angle("zx", None, 90) == (90.0, [])
    with pytest.raises(drawing_module.InvalidArgument):
        _up_axis_angle("xy", "z", None)  # normal to the sheet
    with pytest.raises(drawing_module.InvalidArgument):
        _up_axis_angle("zx", "z", 45)


_FRONT_VIEW = [
    {"name": n, "type": t, "out": False, "optional": False}
    for n, t in (
        ("iXPos", "double"), ("iYpos", "double"), ("iListofPrdInst", "array"), ("iPlane", "array"),
        ("iViewStyle", "string"), ("iComputeUpdate", "bool"), ("iViewProp", "object"),
    )
]


def _front_view_app(monkeypatch):
    monkeypatch.setattr(drawing_module, "method_params", lambda obj, method: _FRONT_VIEW)
    app = App3DX()
    app.session_services["CATDrawingGenService"] = SimpleNamespace(DrawingGenViewProp=object())
    reference = PLMObject(V_Name="100200 BRACKET")
    app.open_editor(SimpleNamespace(Name="root", ReferenceRootOccurrenceOf=lambda: reference), "100200 BRACKET")
    views = Coll([SimpleNamespace(Name="Main"), SimpleNamespace(Name="Background")])
    created = []

    def define(*args):
        view = SimpleNamespace(Name="FRONT", x=0.0, y=0.0, Scale=1.0, Angle=0.0, Update=lambda: None)
        created.append(view)
        views.append(view)
        return view

    views.DrawingDefineGenView = SimpleNamespace(DefineFrontView=define)
    sheet = SimpleNamespace(Name="Sheet.1", Views=views)
    app.open_editor(SimpleNamespace(Sheets=SimpleNamespace(ActiveSheet=sheet)), "Drawing")
    return app, created


def test_add_view_on_zx_hints_and_up_axis_rotates(monkeypatch):
    app, created = _front_view_app(monkeypatch)
    result = call(app, "catia_drawing_add_view", {"plane": "zx"})
    assert not result.isError, text(result)
    assert "Z axis HORIZONTAL" in text(result) and created[0].Angle == 0.0

    app, created = _front_view_app(monkeypatch)
    result = call(app, "catia_drawing_add_view", {"plane": "zx", "up_axis": "z"})
    assert not result.isError, text(result)
    assert created[0].Angle == pytest.approx(math.pi / 2)
    assert "rotated 90 deg" in text(result) and "HORIZONTAL" not in text(result)


# ── 5. Section view name ──


class DashedView:
    """CATIA ignores Name; GetViewName shows the dash in the ident."""

    def __init__(self) -> None:
        self._name = "Section A-A-"
        self.calls: list = []

    @property
    def Name(self):
        return self._name

    @Name.setter
    def Name(self, value):
        pass

    def SetViewName(self, *parts):
        self.calls.append(parts)

    def _outs_GetViewName(self):
        return ["Section ", "A-A-", ""]


def test_section_name_fallback_reports_where_the_dash_lives():
    app = App3DX()
    view = DashedView()
    note = DraftingTools._strip_trailing_dash(view, app)
    assert view.calls == []  # suffix already empty; the ident is never touched
    assert "kept the trailing '-'" in note and "ident='A-A-'" in note


# ── 6. Print ──


_PDF_IMAGE_ONLY_LETTER = b"%PDF-1.7\n1 0 obj << /Type /Page /MediaBox [0 0 612 792] >> endobj\n%%EOF"


def test_print_writes_one_file_per_sheet_and_warns(monkeypatch, tmp_path):
    monkeypatch.setattr(DraftingTools, "_PRINT_POLL", 0.0)
    app, sheet, _, drawing = drawing_app([make_view("View.1"), make_view("View.2")])
    printed = []

    def printer(s):
        def print_to_file(path):
            printed.append((s.Name, path))
            with open(path, "wb") as handle:
                handle.write(_PDF_IMAGE_ONLY_LETTER)
        return print_to_file

    sheet.PrintToFile = printer(sheet)
    second = SimpleNamespace(
        Name="Sheet.2", PaperSize=2, Orientation=1,
        GetPaperWidth=lambda: 1189.0, GetPaperHeight=lambda: 841.0, Activate=lambda: None,
    )
    second.PrintToFile = printer(second)
    drawing.Sheets.append(second)

    out_dir = tmp_path / "prints"
    data = payload(call(app, "catia_drawing_print", {"file_path": str(out_dir)}))
    assert [p[0] for p in printed] == ["Sheet.1", "Sheet.2"]
    assert len(data["files"]) == 2 and data["experimental"] is True
    for row in data["files"]:
        assert row["pdf"] is True and row["text_layer"] is False
        assert row["bytes"] == len(_PDF_IMAGE_ONLY_LETTER)
        assert row["page_mm"] == [215.9, 279.4]
    assert sorted(p.name for p in out_dir.iterdir()) == sorted(os.path.basename(p[1]) for p in printed)
    warnings = " ".join(data["warnings"])
    assert "image-only" in warnings and "(Letter)" in warnings and "Microsoft Print to PDF" in warnings

    # One named sheet to an explicit file.
    target = tmp_path / "out" / "sheet2.pdf"
    data = payload(call(app, "catia_drawing_print", {"file_path": str(target), "sheets": ["Sheet.2"]}))
    assert target.is_file() and data["files"][0]["sheet"] == "Sheet.2"


def test_print_refuses_bad_input_and_reports_nothing_written(monkeypatch, tmp_path):
    monkeypatch.setattr(DraftingTools, "_PRINT_POLL", 0.0)
    app, sheet, *_ = drawing_app([make_view("View.1"), make_view("View.2")])
    assert call(app, "catia_drawing_print", {"file_path": "relative/dir"}).structuredContent["code"] == (
        "INVALID_ARGUMENT"
    )
    missing = call(app, "catia_drawing_print", {"file_path": str(tmp_path), "sheets": ["Nope"]})
    assert missing.structuredContent["code"] == "OBJECT_NOT_FOUND"
    sheet.PrintToFile = lambda path: None  # prints nothing
    none = call(app, "catia_drawing_print", {"file_path": str(tmp_path), "timeout": 0})
    assert none.structuredContent["code"] == "FEATURE_NO_EFFECT"


def test_print_selects_and_restores_the_printer(monkeypatch, tmp_path):
    monkeypatch.setattr(DraftingTools, "_PRINT_POLL", 0.0)
    app, sheet, *_ = drawing_app([make_view("View.1"), make_view("View.2")])
    office = SimpleNamespace(DeviceName="Office Laser")
    pdf = SimpleNamespace(DeviceName="Microsoft Print to PDF")
    app.Printers = Coll([office, pdf])
    app.ActivePrinter = office
    used = []

    def print_to_file(path):
        used.append(app.ActivePrinter.DeviceName)
        with open(path, "wb") as handle:
            handle.write(b"%PDF-1.4 /Font /F1 /MediaBox [0 0 2383.94 1683.78]")

    sheet.PrintToFile = print_to_file
    data = payload(call(app, "catia_drawing_print", {"file_path": str(tmp_path), "printer": "print to pdf"}))
    assert used == ["Microsoft Print to PDF"] and app.ActivePrinter is office
    assert data["printer"] == "Microsoft Print to PDF"
    assert data["files"][0]["text_layer"] is True
    # A0 page on the A0 sheet: only the general format warning.
    assert len(data["warnings"]) == 1

    unknown = call(app, "catia_drawing_print", {"file_path": str(tmp_path), "printer": "Plotter"})
    assert unknown.structuredContent["code"] == "OBJECT_NOT_FOUND"


def test_round3_schemas_are_valid():
    conn = CATIAConnection(platform=PLATFORM_3DX)
    by_name = {d["name"]: d for d in DraftingTools(conn).get_tool_definitions()}
    for definition in by_name.values():
        Draft202012Validator.check_schema(definition["inputSchema"])
    assert "coords" in by_name["catia_drawing_add_text"]["inputSchema"]["properties"]
    assert "up_axis" in by_name["catia_drawing_add_view"]["inputSchema"]["properties"]
    assert "EXPERIMENTAL" in by_name["catia_drawing_print"]["description"]
    assert "not addressable" in by_name["catia_drawing_generate_dimensions"]["description"].lower()
