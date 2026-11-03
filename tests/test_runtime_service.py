"""Background-service runtime: busy guard, timeouts, health, reattach,
COM error classification and tool annotations (offline, no CATIA)."""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from catia_mcp.connection import CATIAConnection
from catia_mcp.server import CATIAMCPServer, default_annotations


class ComError(Exception):
    """Shape of pywintypes.com_error: the HRESULT is args[0]."""


def run(coro):
    return asyncio.run(coro)


def test_call_timeout_reports_catia_timeout_and_busy_guard_fails_fast(monkeypatch):
    release = threading.Event()

    async def body():
        server = CATIAMCPServer()
        server.call_timeout = 0.2
        server.busy_grace = 0.0
        monkeypatch.setattr(server.connection, "ensure_connected", lambda: None)

        def execute(name, arguments):
            release.wait(5)
            return "done"

        monkeypatch.setattr(server.document_tools, "execute", execute)
        try:
            first = await server.call_tool("catia_list_documents", {})
            assert first.structuredContent["code"] == "CATIA_TIMEOUT"
            assert server.status()["busy"]["tool"] == "catia_list_documents"
            second = await server.call_tool("catia_list_documents", {})
            assert second.structuredContent["code"] == "CATIA_BUSY"
            assert second.structuredContent["effects"]["modified"] is False
            assert second.structuredContent["data"]["busy_tool"] == "catia_list_documents"
        finally:
            release.set()
            await server.close()
        assert server.status()["busy"] is None

    run(body())


def test_status_never_touches_com_and_reports_service_fields():
    server = CATIAMCPServer()
    status = server.status()
    assert status["service"] == "catia-mcp"
    assert status["catia"]["connected"] is False
    assert {"pid", "version", "uptime_s", "calls", "busy", "commit", "log", "scripts"} <= set(status)
    run(server.close())


def test_lost_catia_drops_connection_and_resets_module_state(monkeypatch):
    async def body():
        server = CATIAMCPServer()
        conn = server.connection
        conn.app = SimpleNamespace(Caption="CATIA")
        resets = []
        monkeypatch.setattr(
            server.sketcher_tools, "reset_session_state", lambda: resets.append("sketch"), raising=False
        )

        def dies(name, arguments):
            raise ComError(-2147023174, "The RPC server is unavailable.", None, None)

        monkeypatch.setattr(server.document_tools, "execute", dies)
        try:
            result = await server.call_tool("catia_list_documents", {})
            assert result.structuredContent["code"] == "CATIA_NOT_RUNNING"
            assert conn.app is None and conn.generation == 1

            # CATIA comes back: the next call re-attaches and modules drop
            # proxies cached from the dead process.
            def reattach():
                if conn.app is None:
                    conn.app = SimpleNamespace(Caption="CATIA")
                    conn.generation += 1

            monkeypatch.setattr(conn, "ensure_connected", reattach)
            monkeypatch.setattr(server.document_tools, "execute", lambda n, a: "ok")
            again = await server.call_tool("catia_list_documents", {})
            assert again.content[0].text == "ok"
            assert resets == ["sketch"]
        finally:
            await server.close()

    run(body())


def test_rejected_call_is_catia_busy_and_keeps_the_connection(monkeypatch):
    async def body():
        server = CATIAMCPServer()
        server.connection.app = SimpleNamespace(Caption="CATIA")
        monkeypatch.setattr(server.connection, "ensure_connected", lambda: None)

        def rejected(name, arguments):
            raise ComError(-2147418111, "Call was rejected by callee.", None, None)

        monkeypatch.setattr(server.document_tools, "execute", rejected)
        try:
            result = await server.call_tool("catia_list_documents", {})
            assert result.structuredContent["code"] == "CATIA_BUSY"
            assert server.connection.app is not None
        finally:
            await server.close()

    run(body())


def test_busy_liveness_probe_keeps_proxy():
    conn = CATIAConnection()

    class Busy:
        @property
        def Caption(self):
            raise ComError(-2147417846, "retry later", None, None)

    conn.app = Busy()
    assert conn.is_connected is True and conn.app is not None

    class Dead:
        @property
        def Caption(self):
            raise ComError(-2147023174, "unavailable", None, None)

    conn.app = Dead()
    assert conn.is_connected is False and conn.app is None and conn.generation == 1


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (AttributeError("'NoneType' object has no attribute 'Name'"), "INTERNAL_ERROR"),
        (AttributeError("<unknown>.AddNewPad"), "UNSUPPORTED_CAPABILITY"),
        (ValueError("radius must be positive"), "INVALID_ARGUMENT"),
        (RuntimeError("Exception occurred."), "COM_ERROR"),
    ],
)
def test_exception_classification(monkeypatch, error, code):
    async def body():
        server = CATIAMCPServer()
        monkeypatch.setattr(server.connection, "ensure_connected", lambda: None)

        def boom(name, arguments):
            raise error

        monkeypatch.setattr(server.document_tools, "execute", boom)
        try:
            result = await server.call_tool("catia_list_documents", {})
            assert result.structuredContent["code"] == code
        finally:
            await server.close()

    run(body())


def test_tools_carry_annotations_and_core_toolset_filters(monkeypatch):
    server = CATIAMCPServer()
    tools = server.list_tools()
    assert all(tool.annotations is not None for tool in tools)
    by_name = {tool.name: tool for tool in tools}
    assert by_name["catia_list_documents"].annotations.readOnlyHint is True
    assert by_name["catia_close_document"].annotations.destructiveHint is True
    assert default_annotations("catia_pad")["readOnlyHint"] is False

    server.toolset = "core"
    core = {tool.name for tool in server.list_tools()}
    assert "catia_run_python" in core and "catia_gsd_sweep" not in core
    run(server.close())


def test_run_python_returns_output_result_and_traceback():
    async def body():
        server = CATIAMCPServer(allow_scripts=True)
        server.connection.app = SimpleNamespace(Caption="CATIA R428")
        server.connection.platform = "3dx"
        try:
            ok = await server.call_tool(
                "catia_run_python", {"code": "print(catia.Caption)\nsession['n'] = 2\n6 * 7"}
            )
            assert not ok.isError
            assert ok.structuredContent["data"]["result"] == 42
            assert "CATIA R428" in ok.structuredContent["data"]["stdout"]
            kept = await server.call_tool("catia_run_python", {"code": "result = session['n']"})
            assert kept.structuredContent["data"]["result"] == 2
            bad = await server.call_tool("catia_run_python", {"code": "print('before')\n1/0"})
            assert bad.structuredContent["code"] == "SCRIPT_ERROR"
            assert "ZeroDivisionError" in bad.structuredContent["message"]
            assert "before" in bad.structuredContent["message"]
        finally:
            await server.close()

        denied_server = CATIAMCPServer(allow_scripts=False)
        denied_server.connection.app = SimpleNamespace(Caption="CATIA")
        try:
            denied = await denied_server.call_tool("catia_run_python", {"code": "1"})
            assert denied.structuredContent["code"] == "PERMISSION_DENIED"
        finally:
            await denied_server.close()

    run(body())


def test_close_does_not_wait_for_a_stuck_worker(monkeypatch):
    release = threading.Event()

    async def body():
        server = CATIAMCPServer()
        server.call_timeout = 0.1
        monkeypatch.setattr(server.connection, "ensure_connected", lambda: None)
        monkeypatch.setattr(server.document_tools, "execute", lambda n, a: release.wait(10))
        await server.call_tool("catia_list_documents", {})
        loop = asyncio.get_running_loop()
        start = loop.time()
        await server.close()
        assert loop.time() - start < 7
        assert server.worker_stuck

    try:
        run(body())
    finally:
        release.set()


def test_status_answers_while_catia_is_not_running(monkeypatch):
    async def body():
        server = CATIAMCPServer()

        def not_running():
            raise RuntimeError("CATIA is not running")

        monkeypatch.setattr(server.connection, "ensure_connected", not_running)
        try:
            result = await server.call_tool("catia_status", {})
            assert not result.isError
            assert result.structuredContent["data"]["connected"] is False
        finally:
            await server.close()

    run(body())


def test_proxy_writes_utf8_bytes_even_on_an_ansi_console(monkeypatch):
    import io

    from catia_mcp.proxy import Proxy

    raw = io.BytesIO()
    # A Windows pipe: cp1252 text layer over a byte buffer.
    monkeypatch.setattr("sys.stdout", io.TextIOWrapper(raw, encoding="cp1252"))

    async def body():
        proxy = Proxy(8765)
        await proxy._write({"jsonrpc": "2.0", "id": 1, "result": {"d": "surface ∩ plane, 90°"}})
        await proxy._client.aclose()

    run(body())
    assert raw.getvalue().decode("utf-8").strip().endswith('"surface ∩ plane, 90°"}}')


def test_proxy_answers_even_when_relaying_fails(monkeypatch):
    import io
    import json

    from catia_mcp.proxy import Proxy

    raw = io.BytesIO()
    monkeypatch.setattr("sys.stdout", io.TextIOWrapper(raw, encoding="utf-8"))

    async def body():
        proxy = Proxy(8765)

        async def broken(message, text):
            raise RuntimeError("boom")

        proxy._relay = broken
        await proxy.handle('{"jsonrpc":"2.0","id":7,"method":"tools/list"}')
        await proxy._client.aclose()

    run(body())
    reply = json.loads(raw.getvalue().decode("utf-8"))
    assert reply["id"] == 7 and "boom" in reply["error"]["message"]


def test_stop_never_kills_a_stale_pid(monkeypatch, tmp_path):
    from catia_mcp import service

    monkeypatch.setenv("CATIA_MCP_HOME", str(tmp_path))
    service.pid_file().write_text('{"pid": 4242, "port": 8765}', encoding="utf-8")
    monkeypatch.setattr(service, "health", lambda *a, **k: None)
    killed = []
    monkeypatch.setattr(service.subprocess, "run", lambda *a, **k: killed.append(a))
    monkeypatch.setattr(service.os, "kill", lambda *a: killed.append(a))
    assert "not running" in service.stop(8765)
    assert killed == [] and not service.pid_file().exists()


def test_desktop_config_with_bom_is_updated(tmp_path):
    import json

    from catia_mcp import service

    path = tmp_path / "claude_desktop_config.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"mcpServers": {"other": {"command": "x"}}}).encode())
    service.update_desktop_config(path, {"command": "py", "args": ["-m", "catia_mcp", "proxy"]})
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data["mcpServers"]) == {"other", "catia"}
