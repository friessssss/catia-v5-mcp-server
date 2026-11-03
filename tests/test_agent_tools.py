"""Offline contract tests for the agent tools: screenshot as image, view
presets, session status, selection, undo/redo and installed API docs.

They use in-memory doubles; they prove orchestration (what is called, what
is verified, what is returned), not live CATIA behavior.
"""

from __future__ import annotations

import base64
import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.types import ImageContent, TextContent, Tool

from catia_mcp import apidocs
from catia_mcp.connection import PLATFORM_3DX, PLATFORM_V5, CATIAConnection
from catia_mcp.errors import CATIAToolError, InvalidArgument, ObjectNotFound
from catia_mcp.tools import export as export_module
from catia_mcp.tools.export import VIEW_PRESETS, ExportTools, orthogonal_up, resolve_view
from catia_mcp.tools.session import SessionTools
from tests.fakes import App3DX, Selection, make_part

JPEG_BYTES = b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9"


@pytest.fixture(autouse=True)
def state_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CATIA_MCP_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("CATIA_MCP_API_CHM", raising=False)
    return tmp_path / "state"


class FakeViewpoint:
    def __init__(self) -> None:
        self.sight = None
        self.up = None

    def PutSightDirection(self, v):
        self.sight = list(v)

    def PutUpDirection(self, v):
        self.up = list(v)


class FakeViewer:
    """ActiveViewer whose CaptureToFile writes bytes (or nothing)."""

    def __init__(self, write: bool = True) -> None:
        self.write = write
        self.Viewpoint3D = FakeViewpoint()
        self.calls: list[tuple] = []

    def Reframe(self):
        self.calls.append(("Reframe",))

    def Update(self):
        self.calls.append(("Update",))

    def CaptureToFile(self, fmt, path):
        self.calls.append(("CaptureToFile", fmt, path))
        if self.write:
            Path(path).write_bytes(JPEG_BYTES)


class V5App:
    Caption = "CATIA V5"

    def __init__(self, doc=None) -> None:
        self.viewer = FakeViewer()
        self.ActiveWindow = SimpleNamespace(Caption="Part1.CATPart", ActiveViewer=self.viewer)
        self.Windows = SimpleNamespace(Count=1)
        self.Documents = SimpleNamespace(Count=1 if doc else 0)
        self.SystemConfiguration = SimpleNamespace(Version=5, Release=30, ServicePack=4)
        self.FullName = r"C:\Program Files\Dassault Systemes\B30\win_b64\code\bin\CNEXT.exe"
        self.commands: list[str] = []
        if doc is not None:
            self.ActiveDocument = doc

    def StartCommand(self, name):
        self.commands.append(name)


def v5_part_doc():
    part = make_part("Part1")
    part.InWorkObject = SimpleNamespace(Name="PartBody")
    return SimpleNamespace(Name="Part1.CATPart", FullName="C:/w/Part1.CATPart",
                           Part=part, Selection=Selection())


def connection(app, platform):
    conn = CATIAConnection(platform=platform)
    conn.app = app
    conn.platform = platform
    return conn


# ── Screenshot ──


def test_screenshot_returns_image_and_verifies_file(tmp_path):
    app = V5App(v5_part_doc())
    tools = ExportTools(connection(app, PLATFORM_V5))
    target = tmp_path / "shots" / "a.jpg"
    result = tools.execute("catia_screenshot", {"file_path": str(target), "view": "front"})

    image, text = result
    assert isinstance(image, ImageContent) and image.mimeType == "image/jpeg"
    assert base64.b64decode(image.data) == JPEG_BYTES
    assert isinstance(text, TextContent)
    info = json.loads(text.text)
    assert info["path"] == str(target.absolute())
    assert info["view"] == "front" and info["bytes"] == len(JPEG_BYTES)
    assert app.viewer.Viewpoint3D.sight == [0.0, 1.0, 0.0]
    names = [c[0] for c in app.viewer.calls]
    assert names.index("Reframe") < names.index("CaptureToFile")
    assert ("CaptureToFile", 5, str(target.absolute())) in app.viewer.calls


def test_screenshot_default_path_in_captures_dir(state_home):
    app = V5App(v5_part_doc())
    result = ExportTools(connection(app, PLATFORM_V5)).execute(
        "catia_screenshot", {"return_image": False, "fit": False}
    )
    assert len(result) == 1
    info = json.loads(result[0].text)
    path = Path(info["path"])
    assert path.parent == state_home / "captures" and path.suffix == ".jpg"
    assert path.read_bytes() == JPEG_BYTES
    assert ("Reframe",) not in app.viewer.calls


def test_screenshot_fails_when_no_file_written(tmp_path):
    app = V5App(v5_part_doc())
    app.viewer.write = False
    with pytest.raises(CATIAToolError, match="wrote no new image"):
        ExportTools(connection(app, PLATFORM_V5)).execute(
            "catia_screenshot", {"file_path": str(tmp_path / "x.jpg")}
        )


def test_screenshot_fails_when_existing_file_not_rewritten(tmp_path):
    target = tmp_path / "old.jpg"
    target.write_bytes(b"old")
    app = V5App(v5_part_doc())
    app.viewer.write = False
    with pytest.raises(CATIAToolError):
        ExportTools(connection(app, PLATFORM_V5)).execute(
            "catia_screenshot", {"file_path": str(target)}
        )


def test_screenshot_png_without_pillow_saves_jpg(tmp_path, monkeypatch):
    monkeypatch.setattr(export_module, "_pillow", lambda: None)
    app = V5App(v5_part_doc())
    result = ExportTools(connection(app, PLATFORM_V5)).execute(
        "catia_screenshot", {"file_path": str(tmp_path / "p.png")}
    )
    info = json.loads(result[-1].text)
    assert info["path"].endswith("p.jpg") and info["warnings"]


def test_screenshot_bmp_without_pillow_returns_jpeg_preview(tmp_path, monkeypatch):
    monkeypatch.setattr(export_module, "_pillow", lambda: None)
    app = V5App(v5_part_doc())
    result = ExportTools(connection(app, PLATFORM_V5)).execute(
        "catia_screenshot", {"file_path": str(tmp_path / "b.bmp")}
    )
    formats = [c[1] for c in app.viewer.calls if c[0] == "CaptureToFile"]
    assert formats == [4, 5]
    assert result[0].mimeType == "image/jpeg"
    assert json.loads(result[-1].text)["preview_path"].endswith("b.preview.jpg")


def test_screenshot_works_on_3dx(tmp_path):
    app = App3DX()
    app.open_editor(make_part("Shape1"), "Shape1 window")
    viewer = FakeViewer()
    app.ActiveWindow.ActiveViewer = viewer
    result = ExportTools(connection(app, PLATFORM_3DX)).execute(
        "catia_screenshot", {"file_path": str(tmp_path / "s.jpg"), "view": "iso_front_right_top"}
    )
    assert isinstance(result[0], ImageContent)


def test_screenshot_schema_has_no_width_height():
    tools = {t["name"]: t for t in ExportTools(CATIAConnection()).get_tool_definitions()}
    props = tools["catia_screenshot"]["inputSchema"]["properties"]
    assert "width" not in props and "height" not in props
    assert "required" not in tools["catia_screenshot"]["inputSchema"]
    for definition in tools.values():
        Tool(**definition)


# ── Views ──


def test_presets_count_and_orthogonality():
    assert len(VIEW_PRESETS) >= 15
    for name, (sight, up) in VIEW_PRESETS.items():
        assert math.isclose(math.hypot(*sight), 1.0, abs_tol=1e-9), name
        assert math.isclose(math.hypot(*up), 1.0, abs_tol=1e-9), name
        assert abs(sum(a * b for a, b in zip(sight, up))) < 1e-9, name


def test_presets_are_z_up():
    assert VIEW_PRESETS["front"] == ((0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    assert VIEW_PRESETS["top"] == ((0.0, 0.0, -1.0), (0.0, 1.0, 0.0))
    sight, up = VIEW_PRESETS["isometric"]
    assert all(c < 0 for c in sight) and up[2] > 0
    sight, up = VIEW_PRESETS["iso_front_right_top"]
    # Camera at (+X, -Y, +Z) looks toward the origin.
    assert sight[0] < 0 and sight[1] > 0 and sight[2] < 0 and up[2] > 0


def test_explicit_vectors_are_orthogonalized():
    label, _sight, up = resolve_view(None, [1, 0, 0], [0.5, 0, 1])
    assert label == "custom" and up == (0.0, 0.0, 1.0)
    with pytest.raises(InvalidArgument):
        resolve_view(None, [0, 0, 1], [0, 0, 2])
    with pytest.raises(InvalidArgument):
        resolve_view("sideways", None, None)
    assert orthogonal_up((0, 0, -1)) == pytest.approx((0, 1, 0))


def test_set_view_applies_vectors():
    app = V5App(v5_part_doc())
    out = ExportTools(connection(app, PLATFORM_V5)).execute(
        "catia_set_view", {"view": "left", "fit": False}
    )
    assert "left" in out
    assert app.viewer.Viewpoint3D.sight == [1.0, 0.0, 0.0]
    assert app.viewer.Viewpoint3D.up == [0.0, 0.0, 1.0]
    assert ("Reframe",) not in app.viewer.calls


# ── Status ──


def test_status_v5():
    app = V5App(v5_part_doc())
    app.ActiveDocument.Selection.Add(SimpleNamespace(Value=SimpleNamespace(Name="Pad.1")))
    result = SessionTools(connection(app, PLATFORM_V5)).execute("catia_status", {})
    data = result["data"]
    assert result["ok"] and result["code"] == "OK"
    assert data["connected"] and data["platform"] == PLATFORM_V5
    assert data["release"] == 30 and data["service_pack"] == 4
    assert data["active"]["kind"] == "part" and data["active"]["name"] == "Part1.CATPart"
    assert data["in_work_object"] == "PartBody"
    assert data["selection_count"] == 1 and data["windows_count"] == 1


def test_status_v5_without_document_reports_nulls():
    app = V5App(None)
    del app.SystemConfiguration
    result = SessionTools(connection(app, PLATFORM_V5)).execute("catia_status", {})
    data = result["data"]
    assert result["ok"] and data["active"] is None
    assert data["version"] is None and data["selection_count"] is None


def test_status_3dx():
    app = App3DX()
    part = make_part("Shape1")
    part.InWorkObject = SimpleNamespace(Name="PartBody")
    app.open_editor(part, "Shape1 window")
    result = SessionTools(connection(app, PLATFORM_3DX)).execute("catia_status", {})
    data = result["data"]
    assert data["active"] == {"title": "Shape1 window", "name": "Shape1", "kind": "part"}
    assert data["in_work_object"] == "PartBody"
    assert data["selection_count"] == 0 and data["editors_count"] == 1


def test_status_not_connected(monkeypatch):
    conn = CATIAConnection(platform="v5")

    def refuse():
        raise CATIAToolError("CATIA is not running", code="CATIA_NOT_RUNNING")

    monkeypatch.setattr(conn, "ensure_connected", refuse)
    result = SessionTools(conn).execute("catia_status", {})
    assert result["ok"] and result["data"]["connected"] is False


# ── Selection ──


def test_get_selection_lists_items():
    app = App3DX()
    editor = app.open_editor(make_part("Shape1"), "w")
    face = SimpleNamespace(
        Value=SimpleNamespace(Name="Face.1"),
        Type="Face",
        Reference=SimpleNamespace(Name="RSur:(Face:...)", DisplayName="Pad.1\\Face.1"),
        LeafProduct=SimpleNamespace(Name="Shape1.1"),
    )
    editor.Selection.Add(face)
    editor.Selection.Add(SimpleNamespace(Value=SimpleNamespace(Name="Pad.1")))
    result = SessionTools(connection(app, PLATFORM_3DX)).execute(
        "catia_get_selection", {"max_items": 1}
    )
    data = result["data"]
    assert data["count"] == 2 and data["truncated"] is True
    assert data["items"] == [{
        "index": 1, "name": "Face.1", "type": "Face", "reference": "RSur:(Face:...)",
        "reference_display_name": "Pad.1\\Face.1", "leaf_product": "Shape1.1",
    }]


# ── Undo / redo ──


def test_undo_issues_start_command_n_times():
    app = V5App(v5_part_doc())
    tools = SessionTools(connection(app, PLATFORM_V5))
    result = tools.execute("catia_undo", {"steps": 3})
    assert app.commands == ["Undo"] * 3
    assert result["data"] == {"command": "Undo", "issued": 3, "verified": False}
    assert result["effects"]["modified"] is None
    tools.execute("catia_redo", {})
    assert app.commands[-1] == "Redo" and len(app.commands) == 4
    with pytest.raises(InvalidArgument):
        tools.execute("catia_undo", {"steps": 21})


def test_tool_definitions_build_mcp_tools_with_annotations():
    for definition in SessionTools(CATIAConnection()).get_tool_definitions():
        tool = Tool(**definition)
        assert tool.annotations is not None and tool.annotations.title


# ── API docs ──


def write_fake_docs(root: Path) -> Path:
    html_dir = root / "html"
    (html_dir / "interfaces").mkdir(parents=True)
    (html_dir / "interfaces" / "interface_ShapeFactory_12345.htm").write_text(
        "<html><head><title>ShapeFactory (Object)</title><script>var x=1;</script></head>"
        "<body><h1>ShapeFactory</h1><p>Func AddNewPad(Sketch iSketch, double iHeight) "
        "As Pad</p><p>Creates a new Pad&nbsp;feature.</p></body></html>",
        encoding="utf-8",
    )
    (html_dir / "interfaces" / "interface_Pad_1.htm").write_text(
        "<html><head><title>Pad (Object)</title></head><body>Property FirstLimit"
        "<!-- AddNewPad hidden --></body></html>",
        encoding="cp1252",
    )
    (html_dir / "AddNewPad overview.html").write_text(
        "<title>AddNewPad</title><p>See ShapeFactory.AddNewPad.</p>", encoding="utf-8"
    )
    (html_dir / "style.css").write_text("body{}", encoding="utf-8")
    return html_dir


def test_build_index_and_search(tmp_path):
    index = apidocs.build_index(write_fake_docs(tmp_path))
    pages = {e["page"]: e for e in index}
    assert len(index) == 3
    shape = pages["interfaces/interface_ShapeFactory_12345.htm"]
    assert shape["title"] == "ShapeFactory (Object)"
    assert "var x" not in shape["text"] and "Pad feature." in shape["text"]

    results = apidocs.search(index, "addnewpad", 10)
    assert [r["page"] for r in results] == [
        "AddNewPad overview.html",
        "interfaces/interface_ShapeFactory_12345.htm",
    ]
    assert "AddNewPad" in results[1]["snippet"]
    assert apidocs.search(index, "FirstLimit")[0]["title"] == "Pad (Object)"
    with pytest.raises(InvalidArgument):
        apidocs.search(index, "(unclosed")


def test_page_text_and_resolve(tmp_path):
    html_dir = write_fake_docs(tmp_path)
    index = apidocs.build_index(html_dir)
    page = apidocs.resolve_page(index, "interface_ShapeFactory_12345")
    assert page == "interfaces/interface_ShapeFactory_12345.htm"
    assert apidocs.resolve_page(index, "pad (object)") == "interfaces/interface_Pad_1.htm"
    assert "AddNewPad(Sketch iSketch" in apidocs.page_text(html_dir, page)
    with pytest.raises(ObjectNotFound):
        apidocs.page_text(html_dir, "../../etc/passwd")
    with pytest.raises(ObjectNotFound):
        apidocs.resolve_page(index, "Nothing")


def test_find_chm_near_executable(tmp_path):
    bin_dir = tmp_path / "B428" / "win_b64" / "code" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "DSYAutomation.chm").write_bytes(b"chm")
    exe = bin_dir / "3DEXPERIENCE.exe"
    assert apidocs.find_chm(str(exe)) == bin_dir / "DSYAutomation.chm"

    other = tmp_path / "V5" / "win_b64" / "code" / "bin"
    other.mkdir(parents=True)
    docs = tmp_path / "V5" / "doc"
    docs.mkdir()
    (docs / "CAAV5Automation.chm").write_bytes(b"chm")
    assert apidocs.find_chm(str(other / "CNEXT.exe")) == docs / "CAAV5Automation.chm"
    assert apidocs.find_chm(str(tmp_path / "none" / "x.exe")) is None


def test_extract_chm_runs_hh_once(tmp_path, monkeypatch):
    chm = tmp_path / "DSYAutomation.chm"
    chm.write_bytes(b"chm")
    monkeypatch.setattr(apidocs.shutil, "which", lambda name: "C:/Windows/hh.exe" if name == "hh.exe" else None)
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        dest = Path(command[2])
        (dest / "a.htm").write_text("<title>A</title>AddNewPad", encoding="utf-8")
        return SimpleNamespace(returncode=1)  # hh.exe often reports 1 on success

    dest = tmp_path / "out"
    assert apidocs.extract_chm(chm, dest, fake_run) == dest
    assert calls == [["C:/Windows/hh.exe", "-decompile", str(dest), str(chm)]]
    apidocs.extract_chm(chm, dest, fake_run)
    assert len(calls) == 1


def test_extract_chm_reports_failure(tmp_path, monkeypatch):
    chm = tmp_path / "DSYAutomation.chm"
    chm.write_bytes(b"chm")
    monkeypatch.setattr(apidocs.shutil, "which", lambda name: "hh.exe" if name == "hh.exe" else None)
    with pytest.raises(CATIAToolError, match="no HTML pages"):
        apidocs.extract_chm(chm, tmp_path / "out", lambda *a, **k: None)


def test_api_search_and_doc_tools(tmp_path, monkeypatch, state_home):
    install = tmp_path / "B428" / "win_b64" / "code" / "bin"
    install.mkdir(parents=True)
    chm = install / "DSYAutomation.chm"
    chm.write_bytes(b"chm-bytes")
    monkeypatch.setattr(apidocs.shutil, "which", lambda name: "hh.exe" if name == "hh.exe" else None)

    def fake_run(command, **kwargs):
        dest = Path(command[2])
        write_fake_docs(dest)
        return SimpleNamespace(returncode=0)

    app = App3DX()
    app.FullName = str(install / "3DEXPERIENCE.exe")
    tools = SessionTools(connection(app, PLATFORM_3DX))
    tools.docs.runner = fake_run
    result = tools.execute("catia_api_search", {"query": "AddNewPad", "max_results": 1})
    assert result["ok"] and len(result["data"]["results"]) == 1
    assert result["data"]["source"] == str(chm)
    cache_dir = state_home / "apidocs" / f"DSYAutomation-{len(b'chm-bytes')}"
    assert (cache_dir / apidocs._INDEX_FILE).is_file()

    doc = tools.execute("catia_api_doc", {"page": "ShapeFactory (Object)"})
    assert "Func AddNewPad" in doc["data"]["text"]

    # A fresh session without CATIA falls back to the cached extraction.
    offline = SessionTools(CATIAConnection(platform="v5"))
    monkeypatch.setattr(offline.conn, "ensure_connected", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    again = offline.execute("catia_api_search", {"query": "FirstLimit"})
    assert again["data"]["results"][0]["title"] == "Pad (Object)"


def test_schemas_are_valid_json_schema():
    from jsonschema import Draft202012Validator

    conn = CATIAConnection()
    for module in (ExportTools(conn), SessionTools(conn)):
        for definition in module.get_tool_definitions():
            Draft202012Validator.check_schema(definition["inputSchema"])


# ── Review fixes ──


class _FakeImage:
    """Minimal PIL.Image stand-in: open() returns a context-managed image."""

    def __init__(self, path) -> None:
        self.path = str(path)
        self.size = (800, 600)

    @classmethod
    def open(cls, path):
        return cls(path)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def convert(self, mode):
        return self

    def thumbnail(self, size):
        self.size = (min(self.size[0], size[0]), min(self.size[1], size[1]))

    def save(self, target, format=None, **kwargs):
        data = b"\x89PNG-fake" if format == "PNG" else JPEG_BYTES
        if isinstance(target, (str, Path)):
            Path(target).write_bytes(data)
        else:
            target.write(data)


def test_screenshot_png_with_pillow_keeps_users_bmp(tmp_path, monkeypatch):
    monkeypatch.setattr(export_module, "_pillow", lambda: _FakeImage)
    users_bmp = tmp_path / "p.bmp"
    users_bmp.write_bytes(b"user's own bitmap")
    app = V5App(v5_part_doc())
    result = ExportTools(connection(app, PLATFORM_V5)).execute(
        "catia_screenshot", {"file_path": str(tmp_path / "p.png"), "max_size": 256}
    )
    assert users_bmp.read_bytes() == b"user's own bitmap"
    assert (tmp_path / "p.png").read_bytes().startswith(b"\x89PNG")
    assert not list(tmp_path.glob("*.capture.bmp"))
    capture = [c for c in app.viewer.calls if c[0] == "CaptureToFile"]
    assert capture[0][1] == 4 and capture[0][2].endswith(".capture.bmp")
    assert result[0].mimeType == "image/png"
    assert json.loads(result[-1].text)["returned_image"]["size"] == [256, 256]


def test_screenshot_rejects_non_integer_max_size(tmp_path):
    app = V5App(v5_part_doc())
    tools = ExportTools(connection(app, PLATFORM_V5))
    for bad in ("big", 10, 5000):
        with pytest.raises(InvalidArgument):
            tools.execute("catia_screenshot", {"file_path": str(tmp_path / "a.jpg"), "max_size": bad})
    assert not [c for c in app.viewer.calls if c[0] == "CaptureToFile"]


def test_fit_all_without_viewer_is_typed_error():
    app = V5App(v5_part_doc())
    app.ActiveWindow = SimpleNamespace(Caption="Tree only")
    with pytest.raises(CATIAToolError, match="no active 3D viewer"):
        ExportTools(connection(app, PLATFORM_V5)).execute("catia_fit_all", {})


def test_status_never_launches_catia(monkeypatch):
    conn = CATIAConnection(platform="v5", launch=True)
    seen = []

    def fake_ensure():
        seen.append(conn.launch)
        raise RuntimeError("CATIA is not running")

    monkeypatch.setattr(conn, "ensure_connected", fake_ensure)
    result = SessionTools(conn).execute("catia_status", {})
    assert result["data"]["connected"] is False
    assert seen == [False] and conn.launch is True


def test_undo_does_not_reframe_and_validates_steps():
    app = V5App(v5_part_doc())
    tools = SessionTools(connection(app, PLATFORM_V5))
    tools.execute("catia_undo", {"steps": 2})
    assert ("Reframe",) not in app.viewer.calls
    for bad in ("two", 0):
        with pytest.raises(InvalidArgument):
            tools.execute("catia_redo", {"steps": bad})
    with pytest.raises(InvalidArgument):
        tools.execute("catia_get_selection", {"max_items": "all"})


def test_long_interface_page_is_searchable_and_chunked(tmp_path):
    html_dir = tmp_path / "html"
    html_dir.mkdir()
    filler = "".join(f"<p>Func AddNewThing{i}(Reference iRef) As HybridShape</p>" for i in range(3000))
    (html_dir / "HybridShapeFactory.htm").write_text(
        f"<title>HybridShapeFactory (Object)</title>{filler}<p>Func AddNewOffset(Reference iElem)</p>",
        encoding="utf-8",
    )
    index = apidocs.build_index(html_dir)
    # The member sits far past the first few thousand characters.
    assert apidocs.search(index, "AddNewOffset")[0]["title"] == "HybridShapeFactory (Object)"

    text, total, next_offset = apidocs.page_chunk(html_dir, "HybridShapeFactory.htm")
    assert len(text) == apidocs.PAGE_TEXT_CHARS and next_offset == apidocs.PAGE_TEXT_CHARS
    chunks = [text]
    while next_offset is not None:
        text, total, next_offset = apidocs.page_chunk(html_dir, "HybridShapeFactory.htm", next_offset)
        chunks.append(text)
    assert len("".join(chunks)) == total and "AddNewOffset" in chunks[-1]
