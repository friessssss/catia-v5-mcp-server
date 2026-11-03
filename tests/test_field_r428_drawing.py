"""Offline tests for the drafting fixes from the R428 SP6 field report
(field report 2026-11-02, sections 4-6; kept outside the repository).

Covered: sheet-wide dimension generation reporting and view filtering,
the hole-internal filter, angles in degrees, sheet setup, view/dimension
moves, dimension deletion and texts, table cells, text edits, attribute
links (refused while the attribute is empty), template copy/paste, the
section view name's trailing dash and the first-angle note. In-memory
doubles only: these prove orchestration, not live CATIA behaviour.
"""

from __future__ import annotations

import asyncio
import json
import math
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

import catia_mcp.tools.drawing as drawing_module
from catia_mcp.connection import PLATFORM_3DX, CATIAConnection
from catia_mcp.server import CATIAMCPServer
from catia_mcp.tools.drawing import DraftingTools
from tests.fakes import App3DX, Coll, NewService, PLMObject, Selection, make_part


def run(coro):
    return asyncio.run(coro)


def server_on(app) -> CATIAMCPServer:
    server = CATIAMCPServer()
    server.connection.app = app
    server.connection.platform = PLATFORM_3DX
    return server


def call(app, name, args=None):
    async def go():
        server = server_on(app)
        try:
            return await server.call_tool(name, args or {})
        finally:
            await server.close()

    return run(go())


def text(result) -> str:
    return result.content[0].text


def payload(result) -> dict:
    assert not result.isError, text(result)
    return json.loads(text(result))


# ── doubles ──


class DimValue:
    def __init__(self, value: float) -> None:
        self.Value = value
        self.ps: list[tuple] = []
        self.bault: list[tuple] = []

    def SetPSText(self, *args):
        self.ps.append(args)

    def SetBaultText(self, *args):
        self.bault.append(args)


class Dim:
    def __init__(self, name: str, dim_type: int, value: float) -> None:
        self.Name = name
        self.DimType = dim_type
        self.DimStatus = 7
        self.value = DimValue(value)
        self.moves: list[tuple] = []

    def GetValue(self):
        return self.value

    def MoveValue(self, *args):
        self.moves.append(args)


class Table:
    def __init__(self, name: str, rows: int = 3, cols: int = 2) -> None:
        self.Name = name
        self.NumberOfRows = rows
        self.NumberOfColumns = cols
        self.cells: dict[tuple[int, int], str] = {}
        self.cell_objects: dict[tuple[int, int], Text] = {}
        self.AnchorPoint = 0
        self.x = self.y = 0.0

    def SetCellString(self, r, c, s):
        self.cells[(r, c)] = s

    def GetCellString(self, r, c):
        return self.cells.get((r, c), "")

    def GetCellObject(self, r, c):
        return self.cell_objects.setdefault((r, c), Text(f"cell{r}{c}", table=self, cell=(r, c)))


class Text:
    def __init__(self, name: str, table: Table | None = None, cell=None) -> None:
        self.Name = name
        self.Text = ""
        self.x = self.y = 0.0
        self.font_sizes: list[tuple] = []
        self.links: list[tuple] = []
        self.table, self.cell = table, cell

    def SetFontSize(self, *args):
        self.font_sizes.append(args)

    def InsertAttributeLink(self, *args):
        self.links.append(args)
        value = args[2].GetAttributeValue(args[4])
        if self.table is not None:
            self.table.cells[self.cell] = value
        self.Text = value


def make_view(name: str, dims=(), tables=(), texts=(), geometry=()) -> SimpleNamespace:
    return SimpleNamespace(
        Name=name, x=0.0, y=0.0, Scale=1.0, Dimensions=Coll(list(dims)), Tables=Coll(list(tables)),
        Texts=Coll(list(texts)), GeometricElements=Coll(list(geometry)), IsGenerative=lambda: True,
    )


class DeletingSelection(Selection):
    """Selection.Delete removes the selected objects from the views."""

    def __init__(self, views) -> None:
        super().__init__()
        self.views = views
        self.deleted: list = []

    def Delete(self):
        for obj in self.items:
            for view in self.views:
                for coll in (view.Dimensions, view.Tables, view.Texts):
                    if obj in coll.items:
                        coll.items.remove(obj)
            self.deleted.append(obj)
        self.items = []


def drawing_app(views, *, generate=None, title="Drawing00043936", part_editor=None):
    app = App3DX()
    if part_editor is not None:
        part_editor(app)
    view_coll = Coll(views)
    sheet = SimpleNamespace(
        Name="Sheet.1", Views=view_coll, ProjectionMethod=0, PaperSize=2, Orientation=0, Scale=1.0,
        GenerateDimensions=generate or (lambda: None),
        GetPaperWidth=lambda: 841.0, GetPaperHeight=lambda: 594.0,
    )
    sheets = Coll([sheet])
    sheets.ActiveSheet = sheet
    drawing = SimpleNamespace(Name=title, Sheets=sheets, Parent=PLMObject(V_Name=title))
    editor = app.open_editor(drawing, title)
    editor.Selection = DeletingSelection(views)
    return app, sheet, editor


def background_views(*user_views):
    return [make_view("View.1"), make_view("View.2"), *user_views]


# ── 1. Dimension generation ──


def _field_generation(top, section):
    """What GenerateDimensions did live (field report section 4): 2 dimensions
    in Top View, 7 more in Section A-A."""

    def generate():
        top.Dimensions.append(Dim("Dim.1", 0, 100.0))
        top.Dimensions.append(Dim("Dim.2", 0, 50.0))
        for n, (t, v) in enumerate(
            [(0, 60.0), (0, 60.0), (0, 60.0), (4, math.radians(120)), (9, 4.9175), (5, 5.0), (9, 6.0)],
            start=3,
        ):
            section.Dimensions.append(Dim(f"Dim.{n}", t, v))

    return generate


def test_generate_dimensions_with_view_name_deletes_other_views_dims():
    top, section = make_view("Top View"), make_view("Section A-A")
    app, _, editor = drawing_app(
        background_views(top, section), generate=_field_generation(top, section)
    )
    data = payload(call(app, "catia_drawing_generate_dimensions", {"view_name": "Top View"}))
    assert data["generated"] == 9
    assert [d["view"] for d in data["kept"]] == ["Top View", "Top View"]
    assert len(data["deleted"]) == 7
    assert all(d["view"] == "Section A-A" and "outside requested view" in d["reason"] for d in data["deleted"])
    assert section.Dimensions.Count == 0 and top.Dimensions.Count == 2
    assert len(editor.Selection.deleted) == 7


def test_generate_dimensions_reports_all_views_and_skips_hole_internals():
    top, section = make_view("Top View"), make_view("Section A-A")
    app, _, _ = drawing_app(background_views(top, section), generate=_field_generation(top, section))
    data = payload(call(app, "catia_drawing_generate_dimensions", {}))
    assert data["generated"] == 9
    kept = [(d["view"], d["kind"], d["value"]) for d in data["kept"]]
    assert kept == [
        ("Top View", "distance", 100.0), ("Top View", "distance", 50.0),
        ("Section A-A", "distance", 60.0), ("Section A-A", "radius", 5.0), ("Section A-A", "diameter", 6.0),
    ]
    reasons = sorted(d["reason"] for d in data["deleted"])
    assert len(reasons) == 4  # field report: 4 of 9 had to be deleted by hand
    assert sum("duplicate" in r for r in reasons) == 2
    assert any("drill-point angle (120 deg)" in r for r in reasons)
    assert any("tap-drill diameter of M6x1" in r for r in reasons)
    assert section.Dimensions.Count == 3
    assert any("sheet-wide" in n for n in data["notes"])


def test_angles_are_reported_in_degrees():
    top, section = make_view("Top View"), make_view("Section A-A")
    app, _, _ = drawing_app(background_views(top, section), generate=_field_generation(top, section))
    data = payload(call(app, "catia_drawing_generate_dimensions", {"skip_hole_internals": False}))
    angle = next(d for d in data["kept"] if d["kind"] == "angle")
    assert angle["value"] == 120.0 and angle["unit"] == "deg"
    listed = payload(call(app, "catia_drawing_list_dimensions", {"view_name": "Section A-A"}))
    row = next(d for d in listed["dimensions"] if d["kind"] == "angle")
    assert row["value"] == 120.0 and row["unit"] == "deg" and row["index"] == 4


def test_square_block_keeps_equal_dimensions_and_lone_angles():
    """A 100 x 100 block's top view has two equal 100s (the field part); a
    view with no hole signature must keep them, and a 120 deg angle with no
    diameter beside it (a hexagon) is not a drill point."""
    top, hexagon = make_view("Top View"), make_view("Hex View")

    def generate():
        top.Dimensions.append(Dim("Dim.1", 0, 100.0))
        top.Dimensions.append(Dim("Dim.2", 0, 100.0))
        hexagon.Dimensions.append(Dim("Dim.3", 4, math.radians(120)))
        hexagon.Dimensions.append(Dim("Dim.4", 0, 20.0))

    app, _, _ = drawing_app(background_views(top, hexagon), generate=generate)
    data = payload(call(app, "catia_drawing_generate_dimensions", {"view_name": "Top View"}))
    assert [(d["view"], d["value"]) for d in data["kept"]] == [("Top View", 100.0), ("Top View", 100.0)]
    assert all("outside requested view" in d["reason"] for d in data["deleted"])
    assert top.Dimensions.Count == 2

    top2, hexagon2 = make_view("Top View"), make_view("Hex View")
    top, hexagon = top2, hexagon2
    app, _, _ = drawing_app(background_views(top2, hexagon2), generate=generate)
    data = payload(call(app, "catia_drawing_generate_dimensions", {}))
    assert data["deleted"] == []
    assert len(data["kept"]) == 4


def test_link_attribute_still_tries_the_link_when_the_pre_read_fails():
    class Unreadable(PLMObject):
        def GetAttributeValue(self, name):
            raise RuntimeError("attribute not readable")

    reference = Unreadable(V_Name="test part A")
    table = Table("TitleBlock")
    views = background_views()
    views[1] = make_view("View.2", tables=[table])
    app, _, _ = drawing_app(views, part_editor=_part_editor(reference))
    result = call(app, "catia_drawing_link_attribute", {"attribute": "V_Name", "row": 1, "column": 1})
    # The fake link reads the value too, so it fails: the error names both causes.
    assert result.isError
    message = result.structuredContent["message"]
    assert "InsertAttributeLink" in message and "could not be read" in message
    assert result.structuredContent["code"] == "COM_ERROR"


# ── 2. Layout tools ──


def test_set_sheet_sets_paper_orientation_projection_and_reads_back():
    app, sheet, _ = drawing_app(background_views())
    data = payload(call(app, "catia_drawing_set_sheet", {
        "paper_size": "A1", "orientation": "landscape", "projection": "third_angle",
    }))
    assert (sheet.PaperSize, sheet.Orientation, sheet.ProjectionMethod) == (3, 1, 1)
    assert data["read_back"]["PaperSize"] == 3 and data["read_back"]["ProjectionMethod"] == 1
    assert data["warnings"] == []  # all three field-proven


def test_set_sheet_flags_unverified_values_and_refuses_unknown_before_changing():
    app, sheet, _ = drawing_app(background_views())
    data = payload(call(app, "catia_drawing_set_sheet", {"paper_size": "letter", "orientation": "portrait"}))
    assert (sheet.PaperSize, sheet.Orientation) == (0, 0)
    assert len(data["warnings"]) == 2
    result = call(app, "catia_drawing_set_sheet", {"paper_size": "A1", "projection": "fourth_angle"})
    assert result.structuredContent["code"] == "INVALID_ARGUMENT"
    assert sheet.PaperSize == 0


def test_new_drawing_applies_sheet_options():
    app = App3DX()
    app.session_services["PLMNewService"] = NewService(app)
    result = call(app, "catia_new_drawing", {"paper_size": "A1", "projection": "third_angle"})
    assert not result.isError, text(result)
    sheet = app.ActiveEditor.ActiveObject.Sheets.ActiveSheet
    assert sheet.PaperSize == 3 and sheet.ProjectionMethod == 1
    assert "third_angle" in text(result)


def test_move_view_and_move_delete_dimension():
    dims = [Dim("Dim.1", 0, 100.0), Dim("Dim.2", 0, 60.0)]
    top = make_view("Top View", dims=dims)
    app, _, editor = drawing_app(background_views(top))
    assert not call(app, "catia_drawing_move_view", {"view_name": "Top View", "x": 200, "y": 150}).isError
    assert (top.x, top.y) == (200.0, 150.0)

    result = call(app, "catia_drawing_move_dimension", {"view_name": "Top View", "dimension_index": 2, "x": 10, "y": -40})
    assert not result.isError, text(result)
    assert dims[1].moves == [(10.0, -40.0, 0, 0)]

    result = call(app, "catia_drawing_delete_dimension", {"view_name": "Top View", "dimension_name": "Dim.1"})
    assert not result.isError, text(result)
    assert top.Dimensions.items == [dims[1]] and editor.Selection.deleted == [dims[0]]

    missing = call(app, "catia_drawing_delete_dimension", {"view_name": "Top View", "dimension_index": 5})
    assert missing.structuredContent["code"] == "OBJECT_NOT_FOUND"


def test_set_dimension_text_writes_the_field_thread_callout():
    dim = Dim("Dim.7", 9, 6.0)
    app, _, _ = drawing_app(background_views(make_view("Section A-A", dims=[dim])))
    result = call(app, "catia_drawing_set_dimension_text", {
        "view_name": "Section A-A", "dimension_index": 1, "prefix": "M", "after": "x1.0 - 6H THRU",
    })
    assert not result.isError, text(result)
    assert dim.value.ps == [(1, "M", "")]
    assert dim.value.bault == [(1, "", "x1.0 - 6H THRU", "", "")]


def test_set_table_cell_and_edit_text_default_to_background_view():
    table, note = Table("TitleBlock"), Text("Text.1")
    views = background_views()
    views[1] = make_view("View.2", tables=[table], texts=[note])
    app, _, _ = drawing_app(views)
    data = payload(call(app, "catia_drawing_set_table_cell", {
        "row": 1, "column": 2, "text": "BLOCK", "cells": [{"row": 2, "column": 1, "text": "A"}],
    }))
    assert table.cells == {(1, 2): "BLOCK", (2, 1): "A"}
    assert [c["read_back"] for c in data["cells"]] == ["BLOCK", "A"]
    outside = call(app, "catia_drawing_set_table_cell", {"row": 9, "column": 1, "text": "x"})
    assert outside.structuredContent["code"] == "INVALID_ARGUMENT"

    result = call(app, "catia_drawing_edit_text", {"text_index": 1, "text": "DRAFT", "x": 20, "y": 30, "font_size": 7})
    assert not result.isError, text(result)
    assert (note.Text, note.x, note.y, note.font_sizes) == ("DRAFT", 20.0, 30.0, [(0, 0, 7.0)])


def _part_editor(reference):
    def open_part(app):
        root = SimpleNamespace(Name="root", ReferenceRootOccurrenceOf=lambda: reference)
        app.open_editor(
            make_part("Shape", rep=PLMObject(V_Name="test part A shape")),
            "test part A",
            services={"PLMProductService": SimpleNamespace(RootOccurrence=root)},
        )

    return open_part


def test_link_attribute_refuses_an_empty_ein_then_links_once_assigned():
    reference = PLMObject(V_Name="test part A", V_PartNumber="")
    table = Table("TitleBlock")
    views = background_views()
    views[1] = make_view("View.2", tables=[table])
    app, _, _ = drawing_app(views, part_editor=_part_editor(reference))

    args = {"attribute": "V_PartNumber", "row": 2, "column": 2}
    refused = call(app, "catia_drawing_link_attribute", args)
    assert refused.structuredContent["code"] == "ATTRIBUTE_EMPTY"
    message = refused.structuredContent["message"]
    assert "V_PartNumber is empty on 'test part A'" in message and "EIN" in message
    assert refused.structuredContent["effects"]["modified"] is False
    assert table.cell_objects.get((2, 2)) is None or table.cell_objects[(2, 2)].links == []

    reference.attributes["V_PartNumber"] = "1000001"  # user assigned the EIN in the UI
    result = call(app, "catia_drawing_link_attribute", args)
    assert not result.isError, text(result)
    assert table.cell_objects[(2, 2)].links == [(0, 0, reference, "VPMReference", "V_PartNumber")]
    assert "1000001" in text(result)


def test_apply_template_copies_background_and_anchors_tables():
    Selection.shared_clipboard = None
    title_block, revisions = Table("TitleBlock"), Table("RevisionTable")
    template_bg = make_view(
        "View.2", tables=[title_block, revisions], texts=[Text("Note.1"), Text("DRAFT")],
        geometry=[SimpleNamespace(Name=f"Line.{i}") for i in range(1, 5)],
    )
    template_sheet = SimpleNamespace(Name="Sheet.1", Views=Coll([make_view("View.1"), template_bg]))
    template = SimpleNamespace(
        Sheets=SimpleNamespace(ActiveSheet=template_sheet),
        Parent=PLMObject(V_Name="COMPANY DWG TEMPLATE_V1.0"),
    )

    def open_template(app):
        app.open_editor(template, "template")

    app, sheet, editor = drawing_app(background_views(), part_editor=open_template)
    target_bg = sheet.Views.Item(2)
    pasted_into = []

    def paste(selected, clipboard):
        pasted_into.extend(selected)
        for obj in clipboard:
            kind = {Table: "Tables", Text: "Texts"}.get(type(obj), "GeometricElements")
            copy = Table(obj.Name) if isinstance(obj, Table) else SimpleNamespace(Name=obj.Name)
            getattr(target_bg, kind).append(copy)

    editor.Selection.on_paste = paste
    data = payload(call(app, "catia_drawing_apply_template", {
        "template": "COMPANY", "title_block_table": "title", "revision_table": "revision",
        "border_offset": 10,
    }))
    assert data["target"] == "Drawing00043936"
    assert data["selected"] == {"tables": 2, "texts": 2, "geometry": 4}
    assert data["added_to_background"] == {"tables": 2, "texts": 2, "geometry": 4}
    assert pasted_into == [target_bg]
    pasted_title, pasted_rev = target_bg.Tables.items
    assert (pasted_title.AnchorPoint, pasted_title.x, pasted_title.y) == (8, 831.0, 10.0)
    assert (pasted_rev.AnchorPoint, pasted_rev.x, pasted_rev.y) == (6, 831.0, 584.0)
    assert any("zone frame was NOT resized" in n for n in data["notes"])


def test_apply_template_refuses_unknown_template():
    app, _, _ = drawing_app(background_views())
    result = call(app, "catia_drawing_apply_template", {"template": "COMPANY"})
    assert result.structuredContent["code"] == "OBJECT_NOT_FOUND"


# ── 4./5. Section name and projection method ──


_SECTION_VIEW = [
    {"name": n, "type": t, "out": False, "optional": False}
    for n, t in (
        ("iXPos", "double"), ("iYpos", "double"), ("iProfile", "array"), ("iSectionType", "string"),
        ("iProfileType", "string"), ("iSideToDraw", "int"), ("iParentView", "object"),
        ("iViewStyle", "string"), ("iComputeUpdate", "bool"), ("iViewProp", "object"),
    )
]


class CatiaNamedView:
    """A view whose Name assignment CATIA ignores; SetViewName composes it."""

    def __init__(self) -> None:
        self._name = "Section A-A-"
        self.x = self.y = 0.0
        self.Scale = 1.0

    @property
    def Name(self):
        return self._name

    @Name.setter
    def Name(self, value):
        pass

    def SetViewName(self, prefix, ident, suffix):
        self.set_calls = [*getattr(self, "set_calls", []), (prefix, ident, suffix)]
        self._name = f"{prefix}{ident}{suffix}"

    def _outs_GetViewName(self):  # read through SystemService.Evaluate
        return ["Section ", "A-A", "-"]

    def Update(self):
        pass


def test_section_view_name_loses_trailing_dash(monkeypatch):
    monkeypatch.setattr(drawing_module, "method_params", lambda obj, method: _SECTION_VIEW)
    parent = make_view("Front View")
    views = background_views(parent)
    app, sheet, _ = drawing_app(views)
    app.session_services["CATDrawingGenService"] = SimpleNamespace(DrawingGenViewProp=object())

    def define(*args):
        view = CatiaNamedView()
        sheet.Views.append(view)
        return view

    sheet.Views.DrawingDefineGenView = SimpleNamespace(DefineSectionView=define)
    result = call(app, "catia_drawing_section_view", {"profile": [0, -60, 0, 60]})
    assert not result.isError, text(result)
    assert sheet.Views.Item(4).Name == "Section A-A"
    # Only the suffix is rewritten: the ident is shared with the parent's callout.
    assert sheet.Views.Item(4).set_calls == [("Section ", "A-A", "")]
    assert "'Section A-A'" in text(result) and "trailing '-' removed" in text(result)

    plain = SimpleNamespace(Name="Section B-B-")
    assert "removed" in DraftingTools._strip_trailing_dash(plain) and plain.Name == "Section B-B"


def test_projection_view_explains_first_angle(monkeypatch):
    params = [
        {"name": n, "type": "x", "out": False, "optional": False}
        for n in ("iXPos", "iYpos", "iParentView", "iType", "iViewStyle", "iComputeUpdate", "iViewProp")
    ]
    monkeypatch.setattr(drawing_module, "method_params", lambda obj, method: params)
    parent = make_view("Front View")
    parent.x, parent.y = 300.0, 150.0
    app, sheet, _ = drawing_app(background_views(parent))
    app.session_services["CATDrawingGenService"] = SimpleNamespace(DrawingGenViewProp=object())
    placed = []

    def define(x, y, *rest):
        placed.append((x, y))
        view = SimpleNamespace(Name="View", x=0.0, y=0.0, Scale=1.0, Update=lambda: None)
        sheet.Views.append(view)
        return view

    sheet.Views.DrawingDefineGenView = SimpleNamespace(DefineProjectionView=define)
    result = call(app, "catia_drawing_projection_view", {"direction": "bottom"})
    assert not result.isError, text(result)
    assert placed == [(300.0, 250.0)]  # first angle: bottom view above the parent
    assert "FIRST-ANGLE" in text(result) and "above" in text(result)


def test_new_tool_schemas_are_valid():
    conn = CATIAConnection(platform=PLATFORM_3DX)
    names = set()
    for definition in DraftingTools(conn).get_tool_definitions():
        Draft202012Validator.check_schema(definition["inputSchema"])
        names.add(definition["name"])
    assert {
        "catia_drawing_set_sheet", "catia_drawing_move_view", "catia_drawing_move_dimension",
        "catia_drawing_delete_dimension", "catia_drawing_set_dimension_text",
        "catia_drawing_set_table_cell", "catia_drawing_edit_text", "catia_drawing_link_attribute",
        "catia_drawing_apply_template",
    } <= names


@pytest.fixture(autouse=True)
def _reset_clipboard():
    yield
    Selection.shared_clipboard = None
