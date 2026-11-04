"""Write guard (field report 2026-11-02): never modify another user's or
released content, verify the intended target, audit script deletes."""

import asyncio
from types import SimpleNamespace

import pytest

from catia_mcp import guard
from catia_mcp.server import CATIAMCPServer


class Entity:
    def __init__(self, **attrs):
        self.attrs = attrs

    def GetAttributeValue(self, name):
        if name not in self.attrs:
            raise RuntimeError("attribute not found")
        return self.attrs[name]


class Editor:
    def __init__(self, reference, active):
        self.reference = reference
        self.ActiveObject = active

    def GetService(self, name):
        root = SimpleNamespace(ReferenceRootOccurrenceOf=self.reference)
        return SimpleNamespace(RootOccurrence=root)


def conn_showing(**attrs):
    reference = Entity(**attrs)
    shape = SimpleNamespace(Name="Part1", Parent=None)
    return SimpleNamespace(is_3dx=True, active_editor=Editor(reference, shape), app=object())


@pytest.fixture(autouse=True)
def as_jane(monkeypatch):
    monkeypatch.setenv("CATIA_MCP_USER", "jane.doe")


def test_own_in_work_content_is_writable():
    conn = conn_showing(PLM_ExternalID="prd-1", V_Name="test part A",
                        owner="jane.doe", current="IN_WORK")
    assert guard.check(conn, "catia_pad", None, False)["id"] == "prd-1"


def test_someone_elses_template_is_protected():
    conn = conn_showing(PLM_ExternalID="dwg-9", V_Name="COMPANY DWG TEMPLATE_V1.0",
                        owner="other.user", current="IN_WORK")
    with pytest.raises(guard.WriteProtected, match="owned by other.user"):
        guard.check(conn, "catia_drawing_add_text", None, False)
    assert guard.check(conn, "catia_drawing_add_text", None, True)["owner"] == "other.user"


@pytest.mark.parametrize("state", ["FROZEN", "Released", "obsolete"])
def test_frozen_or_released_content_is_protected(state):
    conn = conn_showing(PLM_ExternalID="prd-100200", V_Name="100200 BRACKET",
                        owner="jane.doe", current=state)
    with pytest.raises(guard.WriteProtected, match=state):
        guard.check(conn, "catia_save_document", None, False)


def test_unsaved_session_content_without_metadata_is_writable():
    conn = conn_showing()
    assert guard.check(conn, "catia_pad", None, False)["owner"] is None


def test_target_id_must_match_the_active_window():
    conn = conn_showing(PLM_ExternalID="prd-1", V_Name="test part A",
                        owner="jane.doe", current="IN_WORK")
    assert guard.check(conn, "catia_pad", "PRD-1", False)
    assert guard.check(conn, "catia_pad", "test part A", False)
    with pytest.raises(guard.TargetMismatch, match="active CATIA window shows 'test part A'"):
        guard.check(conn, "catia_pad", "Drawing00043936", False)


def test_risky_script_calls_are_detected():
    code = "sel = editor().Selection\nsel.Add(x)\nsel.Delete()\ndoc.Save()\nsel.Paste ()"
    assert guard.risky_calls(code) == ["Delete", "Save", "Paste"]
    assert guard.risky_calls("print(part().Name)") == []


def run(coro):
    return asyncio.run(coro)


def test_server_refuses_protected_writes_before_touching_the_model(monkeypatch):
    async def body():
        server = CATIAMCPServer(allow_scripts=True)
        server.connection.app = SimpleNamespace(Caption="CATIA")
        server.connection.platform = "3dx"
        monkeypatch.setattr(server.connection, "ensure_connected", lambda: None)
        protected = {"title": "COMPANY DWG TEMPLATE", "id": "dwg-9",
                     "owner": "other.user", "state": "IN_WORK"}
        monkeypatch.setattr(guard, "active_target", lambda conn, entity=None: protected)
        ran = []
        monkeypatch.setattr(server.part_design_tools, "execute", lambda n, a: ran.append(a) or "ok")
        try:
            refused = await server.call_tool("catia_pad", {"height": 10})
            assert refused.structuredContent["code"] == "WRITE_PROTECTED"
            assert refused.structuredContent["effects"]["modified"] is False
            assert ran == []
            allowed = await server.call_tool(
                "catia_pad", {"height": 10, "allow_protected_write": True}
            )
            assert not allowed.isError and ran == [{"height": 10}]  # guard args stripped

            mismatch = await server.call_tool("catia_pad", {"height": 10, "target_id": "prd-1"})
            assert mismatch.structuredContent["code"] == "TARGET_MISMATCH"

            # Reads are never guarded.
            monkeypatch.setattr(server.part_design_tools, "execute", lambda n, a: "[]")
            assert not (await server.call_tool("catia_list_features", {})).isError
        finally:
            await server.close()

    run(body())


def test_run_python_is_guarded_and_audited(monkeypatch):
    async def body():
        server = CATIAMCPServer(allow_scripts=True)
        server.connection.app = SimpleNamespace(Caption="CATIA")
        server.connection.platform = "3dx"
        own = {"title": "test part A", "id": "prd-1",
               "owner": "jane.doe", "state": "IN_WORK"}
        monkeypatch.setattr(guard, "active_target", lambda conn, entity=None: own)
        try:
            result = await server.call_tool(
                "catia_run_python",
                {"code": "class S:\n    def Delete(self): pass\nS().Delete()\nresult = 1"},
            )
            assert not result.isError
            warnings = result.structuredContent["warnings"]
            assert any("Delete" in w and "test part A" in w for w in warnings)
        finally:
            await server.close()

    run(body())


def test_write_tools_advertise_guard_arguments():
    server = CATIAMCPServer()
    tools = {tool.name: tool for tool in server.list_tools()}
    assert "target_id" in tools["catia_pad"].inputSchema["properties"]
    assert "allow_protected_write" in tools["catia_run_python"].inputSchema["properties"]
    assert "target_id" not in tools["catia_list_features"].inputSchema["properties"]
    assert "target_id" not in tools["catia_new_part"].inputSchema["properties"]
    run(server.close())


def test_save_preview_saves_nothing_and_names_the_target(monkeypatch):
    async def body():
        server = CATIAMCPServer()
        server.connection.app = SimpleNamespace(Caption="CATIA")
        server.connection.platform = "3dx"
        monkeypatch.setattr(server.connection, "ensure_connected", lambda: None)
        own = {"title": "test part A", "id": "prd-XX000000000-00000001",
               "owner": "jane.doe", "state": "IN_WORK"}
        monkeypatch.setattr(guard, "active_target", lambda conn, entity=None: own)
        tools = server.document_tools
        monkeypatch.setattr(server.connection, "active_object", lambda: object())
        monkeypatch.setattr(tools.plm, "title_of", lambda obj: "test part A")
        saved = []
        monkeypatch.setattr(tools.plm, "propagate", lambda: saved.append(True))
        try:
            preview = await server.call_tool("catia_save_document", {"preview": True})
            text = preview.content[0].text
            assert "nothing saved" in text and "prd-XX000000000-00000001" in text
            assert saved == []
            done = await server.call_tool("catia_save_document", {})
            assert saved == [True] and "owner jane.doe" in done.content[0].text
        finally:
            await server.close()

    run(body())


def test_saving_someone_elses_template_is_refused(monkeypatch):
    async def body():
        server = CATIAMCPServer()
        server.connection.app = SimpleNamespace(Caption="CATIA")
        server.connection.platform = "3dx"
        monkeypatch.setattr(server.connection, "ensure_connected", lambda: None)
        template = {"title": "COMPANY DWG TEMPLATE_V1.0", "id": "dwg-9",
                    "owner": "other.user", "state": "IN_WORK"}
        monkeypatch.setattr(guard, "active_target", lambda conn, entity=None: template)
        saved = []
        monkeypatch.setattr(server.document_tools.plm, "propagate", lambda: saved.append(True))
        try:
            refused = await server.call_tool("catia_save_document", {})
            assert refused.structuredContent["code"] == "WRITE_PROTECTED" and saved == []
        finally:
            await server.close()

    run(body())


def test_guard_checks_the_drawing_a_tool_really_writes(monkeypatch):
    """apply_template writes the target drawing while the (protected)
    template is in front: the guard must judge the target, not the window."""
    async def body():
        server = CATIAMCPServer()
        server.connection.app = SimpleNamespace(Caption="CATIA")
        server.connection.platform = "3dx"
        monkeypatch.setattr(server.connection, "ensure_connected", lambda: None)
        own_drawing = Entity(PLM_ExternalID="dwg-own", V_Name="Drawing00043936",
                             owner="jane.doe", current="IN_WORK")
        monkeypatch.setattr(server.drafting_tools, "write_target", lambda n, a: own_drawing)
        ran = []
        monkeypatch.setattr(server.drafting_tools, "execute", lambda n, a: ran.append(n) or "ok")
        try:
            result = await server.call_tool(
                "catia_drawing_apply_template",
                {"template": "COMPANY DWG TEMPLATE_V1.0", "target_id": "dwg-own"},
            )
            assert not result.isError and ran == ["catia_drawing_apply_template"]
        finally:
            await server.close()

    run(body())


def test_conditional_guards_close_save_and_dimension_refresh():
    server = CATIAMCPServer()
    assert not server._needs_guard("catia_close_document", {})
    assert server._needs_guard("catia_close_document", {"save": True})
    assert server._needs_guard("catia_drawing_list_dimensions", {})
    assert not server._needs_guard("catia_drawing_list_dimensions", {"refresh": False})
    assert not server._needs_guard("catia_export", {"file_path": "C:/x.stp"})
    assert not server._needs_guard("catia_save_document", {"preview": True})
    run(server.close())


def test_owner_matching_tolerates_domains_and_learns_the_login(monkeypatch):
    assert guard.same_user("JANE.DOE", "jane.doe")
    assert guard.same_user("CORP\\jane.doe", "jane.doe@example.com")
    assert not guard.same_user("other.user", "jane.doe")
    monkeypatch.delenv("CATIA_MCP_USER")
    monkeypatch.setenv("USERNAME", "jdoe")  # Windows name differs from the login
    guard.learn_login("jane.doe")
    assert guard.current_user() == "jane.doe"


@pytest.mark.parametrize(
    "target_id",
    ["prd-1", "PRD-1", "test part B", "test part B A:1", "old name A:2", "old name"],
)
def test_target_id_accepts_id_title_or_window_caption(target_id):
    # After a rename the window keeps its old caption plus "A:1" suffixes.
    target = {"id": "prd-1", "title": "test part B", "caption": "old name A:2",
              "owner": None, "state": None}
    assert guard.matches_target(target, target_id)
    assert not guard.matches_target(target, "prd-2")
    assert not guard.matches_target(target, "test part")


def test_activate_document_finds_a_window_by_plm_id(monkeypatch):
    from catia_mcp.tools.document import DocumentTools

    class Window:
        def __init__(self, caption, plm_id):
            self.Caption, self.plm_id = caption, plm_id

        def Activate(self):
            app.ActiveWindow = self

    class Windows(list):
        @property
        def Count(self):
            return len(self)

        def Item(self, i):
            return self[i - 1]

    old = Window("old name A:1", "prd-7")  # caption stale after a rename
    other = Window("Drawing1 A:1", "dwg-1")
    app = SimpleNamespace(Windows=Windows([other, old]), ActiveWindow=other)
    conn = SimpleNamespace(is_3dx=True, app=app, ensure_connected=lambda: None)
    monkeypatch.setattr(
        guard, "active_target",
        lambda c, entity=None: {"id": app.ActiveWindow.plm_id, "title": "new name",
                                "owner": None, "state": None, "caption": app.ActiveWindow.Caption},
    )
    tools = DocumentTools.__new__(DocumentTools)
    tools.conn = conn
    text = tools._activate_document("prd-7")
    assert app.ActiveWindow is old and "PLM id prd-7" in text
    with pytest.raises(Exception, match="by caption or PLM id"):
        tools._activate_document("prd-404")
    assert app.ActiveWindow is old  # restored to the window in front before the search


def test_read_only_script_may_read_a_released_drawing(monkeypatch):
    async def body():
        server = CATIAMCPServer(allow_scripts=True)
        server.connection.app = SimpleNamespace(Caption="CATIA R428")
        server.connection.platform = "3dx"
        released = {"title": "100200 BRACKET", "id": "dwg-5", "owner": "other.user",
                    "state": "RELEASED"}
        monkeypatch.setattr(guard, "active_target", lambda conn, entity=None: released)
        try:
            read = await server.call_tool("catia_run_python", {"code": "result = catia.Caption"})
            assert not read.isError and read.structuredContent["effects"]["modified"] is False
            write = await server.call_tool("catia_run_python", {"code": "catia.Caption = 'x'"})
            assert write.structuredContent["code"] == "WRITE_PROTECTED"
        finally:
            await server.close()

    run(body())
