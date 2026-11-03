"""Offline contract tests for the 3DEXPERIENCE / document-level fixes.

Covered: PLMCreate refusals raise at once and are cached per connection,
CATIA_MCP_DEFAULT_PLM_TYPE, DisplayFileAlerts around PLM and V5 file calls,
catia_add_new_part failing fast, inertia COG unit conversion and matrix
precision, Measurer session reset, drawing view aliases/roles, V5
save/open/close guards and diagnostics paths. In-memory doubles only: these
prove orchestration, not live CATIA behaviour.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from catia_mcp.connection import PLATFORM_3DX, PLATFORM_V5, CATIAConnection
from catia_mcp.measure import Measurer, inertia_cog_to_mm, significant
from catia_mcp.plm import PLMCreateRefused, PLMSession, catia_refused, refusal_summary
from catia_mcp.server import CATIAMCPServer
from catia_mcp.tools.diagnostics import DiagnosticsTools
from catia_mcp.tools.drawing import DraftingTools
from catia_mcp.tools.measurement import MeasurementTools
from tests.fakes import App3DX, Coll, NewService, PLMObject

# Text of the live R428 SP6 refusal (smoke_report_before_fixes.json).
FIELD_REFUSAL = (
    "(-2147352567, 'Exception occurred.', (0, None, 'ExecuteScript(Evaluate, "
    "MemoryMacro, catia_mcp_obj)\\nExecution error\\n(The scripting engine for "
    "CATScript has reported the following error:\\n\\nSource: CATIAPLMNewService\\n"
    "Description: The method PLMCreate failed\\nLine: 3\\nColumn: 4\\n)\\n', None, 0, "
    "-2147467259), None)"
)


def run(coro):
    return asyncio.run(coro)


def server_on(app, platform=PLATFORM_3DX) -> CATIAMCPServer:
    server = CATIAMCPServer()
    server.connection.app = app
    server.connection.platform = platform
    return server


async def calls(server: CATIAMCPServer, *steps):
    """Run several tool calls on one server (one connection), then close."""
    try:
        return [await server.call_tool(name, args or {}) for name, args in steps]
    finally:
        await server.close()


def call(server, name, args=None):
    return run(calls(server, (name, args)))[0]


def text(result) -> str:
    return result.content[0].text


def connected(app, platform=PLATFORM_3DX) -> CATIAConnection:
    conn = CATIAConnection(platform=platform)
    conn.app = app
    conn.platform = platform
    return conn


class FieldRefusingService(NewService):
    """PLMNewService refusing some types with the live R428 error text, and
    failing the test if the direct (non-Evaluate) retry is attempted."""

    def __init__(self, app, refuse=("3DPart",)):
        super().__init__(app)
        self.refuse_types = refuse
        self.attempts: list[str] = []
        self.direct_calls = 0

    def _out_PLMCreate(self, plm_type):
        self.attempts.append(plm_type)
        if plm_type in self.refuse_types:
            raise RuntimeError(FIELD_REFUSAL)
        return super()._out_PLMCreate(plm_type)

    def PLMCreate(self, plm_type):
        self.direct_calls += 1
        raise AssertionError("direct PLMCreate retry after a CATIA refusal")


# ── 1. PLMCreate refusals ──


def test_field_refusal_text_is_recognised_and_summarised():
    error = RuntimeError(FIELD_REFUSAL)
    assert catia_refused(error)
    assert refusal_summary(error) == "The method PLMCreate failed"
    assert not catia_refused(RuntimeError("Type mismatch: 'o.PLMCreate'"))


def test_refused_3d_part_falls_back_at_once_and_is_cached_for_the_session():
    app = App3DX()
    service = FieldRefusingService(app)
    app.session_services["PLMNewService"] = service
    first, second = run(
        calls(
            server_on(app),
            ("catia_new_part", {"name": "A"}),
            ("catia_new_part", {"name": "B"}),
        )
    )
    assert not first.isError and not second.isError, (text(first), text(second))
    assert service.direct_calls == 0
    # The second 'auto' call goes straight to 3DShape.
    assert service.attempts == ["3DPart", "3DShape", "3DShape"]
    assert "3DShape 'A'" in text(first) and "PLMCreate failed" in text(first)
    assert "earlier on this machine" in text(second) and "3DShape 'B'" in text(second)


def test_explicit_3d_part_is_still_attempted_and_refused_untouched():
    app = App3DX()
    service = FieldRefusingService(app)
    app.session_services["PLMNewService"] = service
    first, explicit = run(
        calls(
            server_on(app),
            ("catia_new_part", {}),
            ("catia_new_part", {"plm_type": "3DPart"}),
        )
    )
    assert not first.isError
    assert explicit.structuredContent["code"] == "UNSUPPORTED_CAPABILITY"
    assert "refused" in explicit.structuredContent["message"]
    assert explicit.structuredContent["effects"]["modified"] is False
    assert service.attempts == ["3DPart", "3DShape", "3DPart"]


def test_evaluate_machinery_failure_still_retries_the_direct_call():
    app = App3DX()

    class BrokenDetour(NewService):
        direct = 0

        def _out_PLMCreate(self, plm_type):
            raise RuntimeError("Microsoft VBScript runtime error: Type mismatch")

        def PLMCreate(self, plm_type):
            BrokenDetour.direct += 1
            return NewService._out_PLMCreate(self, plm_type)

    app.session_services["PLMNewService"] = BrokenDetour(app)
    result = call(server_on(app), "catia_new_part", {"plm_type": "3DShape"})
    assert not result.isError, text(result)
    assert BrokenDetour.direct == 1


def test_default_plm_type_env_skips_the_3d_part_attempt(monkeypatch):
    monkeypatch.setenv("CATIA_MCP_DEFAULT_PLM_TYPE", "3DShape")
    app = App3DX()
    service = FieldRefusingService(app)
    app.session_services["PLMNewService"] = service
    result = call(server_on(app), "catia_new_part", {"name": "Plate"})
    assert not result.isError, text(result)
    assert service.attempts == ["3DShape"]
    assert "CATIA_MCP_DEFAULT_PLM_TYPE=3DShape" in text(result)


def test_invalid_default_plm_type_env_is_refused_before_creating(monkeypatch):
    monkeypatch.setenv("CATIA_MCP_DEFAULT_PLM_TYPE", "Drawing")
    app = App3DX()
    service = FieldRefusingService(app)
    app.session_services["PLMNewService"] = service
    result = call(server_on(app), "catia_new_part")
    assert result.structuredContent["code"] == "INVALID_ARGUMENT"
    assert service.attempts == []


def test_refusal_survives_reconnect_and_restart_but_expires(monkeypatch):
    # Live R428: every refused 3D Part attempt shows a licence dialog, so a
    # refusal is remembered on disk, across CATIA reconnects and service
    # restarts, until it expires (a newly granted role is then tried again).
    import catia_mcp.plm as plm_module

    app = App3DX()
    app.session_services["PLMNewService"] = FieldRefusingService(app)
    conn = connected(app)
    plm = PLMSession(conn)
    with pytest.raises(PLMCreateRefused):
        plm.create("3DPart")
    conn.app = App3DX()  # reconnect: new Application proxy
    assert plm.refused("3DPart") == "The method PLMCreate failed"
    fresh = PLMSession(connected(App3DX()))  # new server process, same machine
    assert fresh.refused("3DPart") == "The method PLMCreate failed"
    import time

    later = time.time() + plm_module.REFUSAL_TTL_S + 1
    monkeypatch.setattr(time, "time", lambda: later)
    assert PLMSession(connected(App3DX())).refused("3DPart") is None


def test_execution_error_from_the_script_engine_counts_as_refusal():
    from catia_mcp.plm import catia_refused

    live = Exception(
        -2147352567, "Exception occurred.",
        (0, None, "ExecuteScript(Evaluate, MemoryMacro, catia_mcp_obj)\nExecution error\n"
         "(The scripting engine for CATScript has reported the following error:\n\n"
         "Description: Unspecified error\nLine: 3\n)", None, 0, -2147467259), None,
    )
    assert catia_refused(live)
    assert not catia_refused(Exception("Execution error: Syntax error on line 2"))


def test_display_file_alerts_are_off_during_plm_create_and_restored():
    app = App3DX()
    app.DisplayFileAlerts = True
    seen = []

    class Watching(NewService):
        def _out_PLMCreate(self, plm_type):
            seen.append(app.DisplayFileAlerts)
            return super()._out_PLMCreate(plm_type)

    app.session_services["PLMNewService"] = Watching(app)
    PLMSession(connected(app)).create("3DShape")
    assert seen == [False] and app.DisplayFileAlerts is True


# ── 2. catia_add_new_part ──


def _assembly(app):
    root = SimpleNamespace(
        ReferenceRootOccurrenceOf=lambda: SimpleNamespace(RepInstances=Coll()),
        Occurrences=Coll(),
    )
    app.open_editor(root, "Assembly", {"PLMProductService": SimpleNamespace(RootOccurrence=root)})
    return root


def test_add_new_part_refused_is_unsupported_and_quotes_the_refusal():
    app = App3DX()
    service = FieldRefusingService(app)
    app.session_services["PLMNewService"] = service
    _assembly(app)
    live, cached = run(
        calls(server_on(app), ("catia_add_new_part", {}), ("catia_add_new_part", {}))
    )
    for result in (live, cached):
        assert result.structuredContent["code"] == "UNSUPPORTED_CAPABILITY"
        assert "PLMCreate failed" in result.structuredContent["message"]
        assert result.structuredContent["effects"]["modified"] is False
    assert "earlier" in cached.structuredContent["message"]
    assert service.attempts == ["3DPart"]  # the cached call did not ask CATIA again


# ── 3. Inertia / measurer ──


def test_inertia_cog_scale_is_cross_checked_against_the_measurable_cog():
    assert inertia_cog_to_mm([0.0, 0.0, 0.02], [0.0, 0.0, 20.0])[0] == [0.0, 0.0, 20.0]
    mm, basis = inertia_cog_to_mm([0.0, 0.0, 20.0], [0.0, 0.0, 20.1])
    assert mm == [0.0, 0.0, 20.0] and "raw mm" in basis
    assumed, basis = inertia_cog_to_mm([0.01, 0.0, 0.02])
    assert assumed == pytest.approx([10.0, 0.0, 20.0]) and "assumed m" in basis
    _, basis = inertia_cog_to_mm([5.0, 0.0, 0.0], [0.0, 0.0, 20.0])
    assert "NOT agree" in basis


def test_significant_keeps_small_inertia_moments():
    assert significant(0.000812345678) == 0.000812346
    assert significant(1234567.0) == 1234570.0


def _inertia_tools(monkeypatch, cog_fails: bool):
    conn = connected(App3DX())
    tools = MeasurementTools(conn)
    monkeypatch.setattr(conn, "get_active_part", lambda: "part")
    monkeypatch.setattr(conn, "get_active_part_body", lambda: "body")
    measure = tools.measure
    monkeypatch.setattr(measure, "volume_mm3", lambda p, b: 240000.0)
    monkeypatch.setattr(measure, "area_mm2", lambda p, b: 24800.0)

    def cog(p, b):
        if cog_fails:
            raise RuntimeError("Object doesn't support this property or method: 'o.GetCOG'")
        return [50.0, 30.0, 20.0]

    monkeypatch.setattr(measure, "cog_mm", cog)
    matrix = [0.000812345678, 0, 0, 0, 0.0018, 0, 0, 0, 0.00213456789]
    monkeypatch.setattr(
        measure,
        "inertia",
        lambda p, b: {"mass_kg": 1.8648, "matrix": matrix, "cog_raw": [0.05, 0.03, 0.02]},
    )
    return tools


def test_get_inertia_reports_cog_in_mm_and_precise_matrix(monkeypatch):
    result = json.loads(_inertia_tools(monkeypatch, cog_fails=False)._get_inertia())
    assert result["inertia_cog_mm"] == {"x": 50.0, "y": 30.0, "z": 20.0}
    assert "agrees" in result["inertia_cog_unit_basis"]
    assert result["inertia_matrix_kg_m2"][0][0] == 0.000812346
    assert result["inertia_matrix_kg_m2"][2][2] == 0.00213457
    assert "inertia_cog_raw" not in result


def test_get_inertia_falls_back_to_the_inertia_cog_when_the_measurable_fails(monkeypatch):
    result = json.loads(_inertia_tools(monkeypatch, cog_fails=True)._get_inertia())
    assert result["center_of_gravity_mm"] == {"x": 50.0, "y": 30.0, "z": 20.0}
    assert result["center_of_gravity_source"].startswith("InertiaService")
    assert "GetCOG" in result["center_of_gravity_measurable_error"]


def test_reset_session_state_clears_measurer_caches():
    tools = MeasurementTools(connected(App3DX()))
    measurer: Measurer = tools.measure
    measurer._codes["volume"] = 7
    measurer._enum = {"CAAMeasurableVolume": 7}
    measurer._enum_read = True
    tools.reset_session_state()
    assert measurer._codes == {} and measurer._enum is None and measurer._enum_read is False


# ── 4. Drawing view aliases ──


def _drawing_tools(view_names):
    app = App3DX()
    views = Coll([SimpleNamespace(Name=n, x=0.0, y=0.0, Scale=1.0) for n in view_names])
    sheet = SimpleNamespace(Name="Sheet.1", Views=views)
    sheets = Coll([sheet])
    sheets.ActiveSheet = sheet
    app.open_editor(SimpleNamespace(Sheets=sheets, Parent=PLMObject(V_Name="DWG")), "Drawing")
    return DraftingTools(connected(app)), views


def test_main_and_background_aliases_resolve_by_index():
    tools, views = _drawing_tools(["View.1", "View.2", "Front view"])
    assert tools._find_view("Main") is views.Item(1)
    assert tools._find_view("main  view") is views.Item(1)
    assert tools._find_view("BACKGROUND") is views.Item(2)
    assert tools._find_view_any("Background View") is views.Item(2)
    assert tools._find_view("Front view") is views.Item(3)


def test_a_view_really_named_like_an_alias_wins_over_the_alias():
    tools, views = _drawing_tools(["View.1", "View.2", "main"])
    assert tools._find_view("main") is views.Item(3)


def test_list_views_reports_roles():
    tools, _ = _drawing_tools(["View.1", "View.2", "Front view"])
    sheet = json.loads(tools._list_views())["sheets"][0]
    assert [v["role"] for v in sheet["views"]] == ["main", "background", "user"]


# ── 5. V5 document file handling ──


class V5Doc:
    def __init__(self, app, writes: bool = True):
        self.app = app
        self.Name = "Part1.CATPart"
        self.writes = writes
        self.alerts_seen: list = []
        self.closed = False

    def SaveAs(self, path):
        self.alerts_seen.append(self.app.DisplayFileAlerts)
        if self.writes:
            with open(path, "w") as handle:
                handle.write("x")

    def Save(self):
        self.alerts_seen.append(self.app.DisplayFileAlerts)

    def Close(self):
        self.alerts_seen.append(self.app.DisplayFileAlerts)
        self.closed = True


def _v5(writes=True):
    app = SimpleNamespace(Caption="CATIA V5", DisplayFileAlerts=True)
    app.ActiveDocument = V5Doc(app, writes)
    app.Documents = SimpleNamespace(Open=lambda path: pytest.fail("Documents.Open called"))
    return app


def test_v5_save_as_turns_alerts_off_and_checks_the_file(tmp_path):
    app = _v5()
    target = tmp_path / "out.CATPart"
    result = call(server_on(app, PLATFORM_V5), "catia_save_document", {"file_path": str(target)})
    assert not result.isError, text(result)
    assert app.ActiveDocument.alerts_seen == [False] and app.DisplayFileAlerts is True
    assert target.exists()


def test_v5_save_as_without_a_written_file_fails_as_possibly_modified(tmp_path):
    app = _v5(writes=False)
    result = call(
        server_on(app, PLATFORM_V5), "catia_save_document", {"file_path": str(tmp_path / "x.CATPart")}
    )
    assert result.structuredContent["code"] == "OBJECT_NOT_FOUND"
    assert result.structuredContent["effects"]["modified"] is None


def test_v5_close_and_save_run_with_alerts_off():
    app = _v5()
    result = call(server_on(app, PLATFORM_V5), "catia_close_document", {"save": True})
    assert not result.isError, text(result)
    assert app.ActiveDocument.alerts_seen == [False, False] and app.ActiveDocument.closed
    assert app.DisplayFileAlerts is True


def test_open_missing_file_is_object_not_found_before_any_com_call(tmp_path):
    app = _v5()
    result = call(
        server_on(app, PLATFORM_V5), "catia_open_document", {"file_path": str(tmp_path / "nope.CATPart")}
    )
    assert result.structuredContent["code"] == "OBJECT_NOT_FOUND"
    assert result.structuredContent["effects"]["modified"] is False


def test_connect_description_says_it_attaches_and_is_optional():
    server = CATIAMCPServer()
    definition = next(
        d for d in server.document_tools.get_tool_definitions() if d["name"] == "catia_connect"
    )
    assert "CATIA_MCP_LAUNCH=1" in definition["description"]
    assert "Must be called" not in definition["description"]


# ── 7. Diagnostics ──


def test_diagnostics_reports_log_path_and_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CATIA_MCP_HOME", str(tmp_path))
    info = DiagnosticsTools(connected(App3DX()))._python_info(App3DX())
    assert info["state_dir"] == str(tmp_path)
    assert info["log_path"].startswith(str(tmp_path))
    assert "pywin32_build" in info


# ── Review follow-ups ──


def test_successful_explicit_create_clears_the_cached_refusal():
    app = App3DX()
    service = FieldRefusingService(app, refuse=("3DShape",))
    app.session_services["PLMNewService"] = service
    plm = PLMSession(connected(app))
    with pytest.raises(PLMCreateRefused):
        plm.create("3DShape")
    assert plm.refused("3DShape") is not None
    service.refuse_types = ()  # role granted meanwhile
    plm.create("3DShape")
    assert plm.refused("3DShape") is None


def test_relative_paths_are_refused_for_open_and_save_as():
    for name, args in (
        ("catia_open_document", {"file_path": "part.CATPart"}),
        ("catia_save_document", {"file_path": "out.CATPart"}),
    ):
        app = _v5()
        result = call(server_on(app, PLATFORM_V5), name, args)
        assert result.structuredContent["code"] == "INVALID_ARGUMENT", name
        assert app.ActiveDocument.alerts_seen == []


def test_v5_save_as_over_an_existing_file_reports_overwrite_or_unconfirmed(tmp_path):
    target = tmp_path / "out.CATPart"
    target.write_text("old")
    import os

    os.utime(target, ns=(1_000_000_000, 1_000_000_000))
    result = call(server_on(_v5(), PLATFORM_V5), "catia_save_document", {"file_path": str(target)})
    assert "overwritten" in text(result)
    os.utime(target, ns=(1_000_000_000, 1_000_000_000))
    result = call(
        server_on(_v5(writes=False), PLATFORM_V5), "catia_save_document", {"file_path": str(target)}
    )
    assert not result.isError and "could not be confirmed" in text(result)


def test_malformed_inertia_cog_keeps_the_matrix(monkeypatch):
    tools = _inertia_tools(monkeypatch, cog_fails=False)
    monkeypatch.setattr(
        tools.measure, "inertia", lambda p, b: {"mass_kg": 1.0, "matrix": [1e-4] * 9, "cog_raw": ["x"]}
    )
    result = json.loads(tools._get_inertia())
    assert result["inertia_matrix_kg_m2"][0][0] == 0.0001
    assert result["inertia_cog_mm"].startswith("unavailable")


def test_refused_physical_product_is_not_retried():
    app = App3DX()
    service = FieldRefusingService(app, refuse=("VPMReference",))
    app.session_services["PLMNewService"] = service
    first = call(server_on(app), "catia_new_product", {})
    assert first.isError
    attempts = list(service.attempts)
    second = call(server_on(app), "catia_new_product", {})
    assert second.structuredContent["code"] == "UNSUPPORTED_CAPABILITY"
    assert "not retried" in second.structuredContent["message"]
    assert service.attempts == attempts == ["VPMReference"]  # not asked again
