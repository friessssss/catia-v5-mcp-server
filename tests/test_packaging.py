"""Packaging and background-service plumbing, offline.

Covers what `catia-mcp install` writes (Claude Desktop config entries and
their discovery), logging without a console (pythonw.exe), the CLI command
parsing, packaging metadata, and the stdio proxy relaying to a real HTTP
server end to end. Nothing here touches COM or the Windows registry.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import socket
import sys
from pathlib import Path

import httpx
import pytest
import uvicorn

from catia_mcp import __version__, paths, service
from catia_mcp.http_transport import build_app
from catia_mcp.proxy import Proxy
from catia_mcp.server import CATIAMCPServer, parse_args

try:
    import tomllib
except ImportError:  # Python 3.10
    tomllib = None

ROOT = Path(__file__).resolve().parent.parent
needs_tomllib = pytest.mark.skipif(tomllib is None, reason="tomllib needs Python 3.11+")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    # Logs, PID file and locks go to a throw-away state directory.
    monkeypatch.setenv("CATIA_MCP_HOME", str(tmp_path / "state"))


# ── Packaging metadata ──


@needs_tomllib
def test_versions_and_entry_points_agree():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert meta["version"] == __version__ == "0.3.0"
    assert meta["scripts"]["catia-mcp"] == "catia_mcp.server:main"
    assert meta["gui-scripts"]["catia-mcpw"] == "catia_mcp.server:main"
    assert meta["optional-dependencies"]["images"] == ["Pillow>=10"]
    assert "pywin32>=311; sys_platform == 'win32'" in meta["dependencies"]


@needs_tomllib
def test_requirements_txt_matches_pyproject():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    lines = [
        line.strip()
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert lines == meta["dependencies"]


def test_installer_scripts_exist_and_name_the_package():
    ps1 = (ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
    assert "uv tool install" in ps1 or "'tool', 'install'" in ps1
    assert "catia-v5-mcp-server[images]" in ps1 or "$PackageName[images]" in ps1
    # Live R428 laptop: native output leaked into Install-Uv's return value
    # and PowerShell tried to run the uv banner as the uv.exe path.
    assert "& $Exe @Arguments | Out-Host" in ps1
    assert "Set-StrictMode" in ps1 and "$ErrorActionPreference = 'Stop'" in ps1
    assert "[switch]$Uninstall" in ps1
    # Windows PowerShell 5.1 reads BOM-less scripts as ANSI: keep it ASCII.
    assert ps1.isascii()
    cmd = (ROOT / "scripts" / "install.cmd").read_bytes()
    assert b"-ExecutionPolicy Bypass" in cmd and b"%*" in cmd
    assert b"\r\n" in cmd  # cmd.exe batch files want CRLF
    # cmd.exe has no /dev/null: redirecting there fails, `&&` skips the
    # pause and a double-clicked window closes before the error can be read.
    assert b"/dev/null" not in cmd and b">NUL" in cmd.upper()
    # The uv tool's python.exe is a launcher with the real interpreter as a
    # child; only a tree kill releases the environment for the upgrade.
    assert "/T /F" in ps1


# ── Claude Desktop configuration ──


def test_proxy_entry_uses_this_interpreter_and_only_adds_nondefault_port():
    entry = service.proxy_entry()
    assert entry == {"command": sys.executable, "args": ["-m", "catia_mcp", "proxy"]}
    other = service.proxy_entry(9000)
    assert other["args"] == ["-m", "catia_mcp", "proxy", "--port", "9000"]
    assert Path(other["command"]).is_absolute()


def test_update_desktop_config_adds_and_removes_keeping_other_servers(tmp_path):
    path = tmp_path / "claude_desktop_config.json"
    original = {
        "mcpServers": {"fusion": {"command": "fusion-mcp"}},
        "globalShortcut": "Ctrl+Space",
    }
    path.write_text(json.dumps(original), encoding="utf-8")

    message = service.update_desktop_config(path, service.proxy_entry())
    assert "configured in" in message
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["mcpServers"]["fusion"] == {"command": "fusion-mcp"}
    assert data["mcpServers"]["catia"] == service.proxy_entry()
    assert data["globalShortcut"] == "Ctrl+Space"
    backup = path.with_suffix(".json.bak")
    assert json.loads(backup.read_text(encoding="utf-8")) == original

    message = service.update_desktop_config(path, None)
    assert "removed from" in message
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "catia" not in data["mcpServers"]
    assert data["mcpServers"]["fusion"] == {"command": "fusion-mcp"}
    assert "no 'catia' entry" in service.update_desktop_config(path, None)


def test_update_desktop_config_creates_missing_and_refuses_invalid_json(tmp_path):
    fresh = tmp_path / "new" / "claude_desktop_config.json"
    fresh.parent.mkdir()
    service.update_desktop_config(fresh, service.proxy_entry())
    assert json.loads(fresh.read_text(encoding="utf-8"))["mcpServers"]["catia"]
    assert not fresh.with_suffix(".json.bak").exists()

    broken = tmp_path / "broken.json"
    broken.write_text("{ not json", encoding="utf-8")
    assert "not valid JSON" in service.update_desktop_config(broken, service.proxy_entry())
    assert broken.read_text(encoding="utf-8") == "{ not json"


def test_claude_desktop_configs_finds_classic_and_msix_installs(tmp_path, monkeypatch):
    appdata = tmp_path / "Roaming"
    local = tmp_path / "Local"
    (appdata / "Claude").mkdir(parents=True)
    msix = local / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude"
    msix.mkdir(parents=True)
    # A package without a Claude config folder is ignored.
    (local / "Packages" / "Claude_other" / "LocalCache").mkdir(parents=True)
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(sys, "platform", "win32")

    found = service.claude_desktop_configs()
    assert found == [
        appdata / "Claude" / "claude_desktop_config.json",
        msix / "claude_desktop_config.json",
    ]


def test_claude_desktop_configs_always_include_appdata_on_windows(tmp_path, monkeypatch):
    # Live laptop: Claude Desktop installed, but no folder matched, so the
    # entry was never written. The classic path is now always configured.
    appdata = tmp_path / "a"
    local = tmp_path / "b"
    store = local / "Packages" / "AnthropicPBC.Claude_abc123" / "LocalCache" / "Roaming" / "Claude"
    store.mkdir(parents=True)
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(sys, "platform", "win32")
    found = service.claude_desktop_configs()
    assert found == [
        appdata / "Claude" / "claude_desktop_config.json",
        store / "claude_desktop_config.json",
    ]
    service.update_desktop_config(found[0], service.proxy_entry())
    assert "catia" in found[0].read_text(encoding="utf-8")


def test_claude_desktop_configs_empty_off_windows_without_claude(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "a"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "b"))
    monkeypatch.setattr(sys, "platform", "linux")
    assert service.claude_desktop_configs() == []


# ── Logging under pythonw.exe ──


def test_configure_logging_survives_missing_stdio(monkeypatch):
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    try:
        path = paths.configure_logging(console=True)
        # Stray prints (uvicorn, pywin32) must not crash a windowless process.
        assert sys.stdout is not None and sys.stderr is not None
        print("stray print")
        sys.stderr.write("stray stderr\n")
        logging.getLogger("catia_mcp.test").info("logged without a console")
        for handler in root.handlers:
            handler.flush()
        assert path == paths.log_path()
        assert "logged without a console" in path.read_text(encoding="utf-8")
        assert not any(
            isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
            for h in root.handlers
        )
    finally:
        replaced = sys.stdout
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)
        if replaced is not None and replaced is not sys.__stdout__:
            replaced.close()


# ── Command line ──


@pytest.mark.parametrize("command", ["serve", "proxy", "install", "uninstall", "start", "stop", "status"])
def test_parse_args_accepts_service_commands(command):
    args = parse_args([command])
    assert args.command == command
    assert args.port == 8765 and args.host == "127.0.0.1"


def test_parse_args_install_options_and_stdio_default(monkeypatch):
    monkeypatch.delenv("CATIA_MCP_ALLOW_SCRIPTS", raising=False)
    args = parse_args(["install", "--port", "9001", "--no-clients"])
    assert (args.command, args.port, args.no_clients) == ("install", 9001, True)
    plain = parse_args([])
    assert plain.command is None and plain.http is False
    assert plain.allow_scripts is True  # stdio: scripts on by default


def test_parse_args_script_policy(monkeypatch):
    monkeypatch.delenv("CATIA_MCP_ALLOW_SCRIPTS", raising=False)
    assert parse_args(["serve"]).allow_scripts is True  # loopback
    assert parse_args(["serve", "--host", "0.0.0.0"]).allow_scripts is False
    assert parse_args(["serve", "--host", "0.0.0.0", "--allow-scripts"]).allow_scripts is True
    assert parse_args(["serve", "--no-scripts"]).allow_scripts is False
    monkeypatch.setenv("CATIA_MCP_ALLOW_SCRIPTS", "0")
    assert parse_args([]).allow_scripts is False


def test_parse_args_rejects_unknown_command():
    with pytest.raises(SystemExit):
        parse_args(["launch-rockets"])


# ── Proxy and /health end to end ──


async def _with_http_server(body, with_status: bool = True):
    mcp_server = CATIAMCPServer()
    port = free_port()
    app = build_app(
        mcp_server.server, "127.0.0.1", port, None, mcp_server.status if with_status else None
    )
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        assert server.started, "uvicorn did not start"
        await body(port, mcp_server)
    finally:
        server.should_exit = True
        await task
        await mcp_server.close()


def test_health_reports_the_service():
    async def body(port, _):
        async with httpx.AsyncClient() as client:
            response = await client.get(f"http://127.0.0.1:{port}/health")
        assert response.status_code == 200
        data = response.json()
        assert data["service"] == "catia-mcp"
        assert data["version"] == __version__
        assert data["catia"]["connected"] is False
        # service.health() is what `catia-mcp status` and the proxy use.
        found = await asyncio.to_thread(service.health, port)
        assert found is not None and found["service"] == "catia-mcp"

    asyncio.run(_with_http_server(body))


def test_service_health_ignores_a_foreign_server():
    async def body(port, _):
        # /health without the status callback answers {"status": "ok"} only:
        # not recognised as the CATIA MCP service.
        assert await asyncio.to_thread(service.health, port) is None

    asyncio.run(_with_http_server(body, with_status=False))
    assert service.health(free_port(), timeout=0.5) is None


def test_proxy_relays_initialize_and_tools_list(monkeypatch):
    raw = io.BytesIO()
    out = io.TextIOWrapper(raw, encoding="utf-8")
    monkeypatch.setattr(sys, "stdout", out)

    async def body(port, _):
        proxy = Proxy(port)
        try:
            await proxy.handle(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {},
                            "clientInfo": {"name": "test", "version": "0"},
                        },
                    }
                )
            )
            # Notifications get no reply line.
            await proxy.handle(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))
            await proxy.handle(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))
            await proxy.handle("not json at all")
        finally:
            await proxy._client.aclose()

    asyncio.run(_with_http_server(body))
    replies = [json.loads(line) for line in raw.getvalue().decode("utf-8").splitlines() if line.strip()]
    assert [r["id"] for r in replies] == [1, 2]
    init, listing = replies
    assert init["result"]["serverInfo"]["name"] == "catia-mcp"
    names = {tool["name"] for tool in listing["result"]["tools"]}
    assert {"catia_screenshot", "catia_run_python", "catia_set_view"} <= names
    assert len(names) > 100


def test_proxy_reports_unreachable_service_as_jsonrpc_error(monkeypatch):
    raw = io.BytesIO()
    out = io.TextIOWrapper(raw, encoding="utf-8")
    monkeypatch.setattr(sys, "stdout", out)

    def no_service(port, wait=20.0):
        raise RuntimeError(f"The CATIA MCP service did not start on port {port}")

    monkeypatch.setattr("catia_mcp.proxy.ensure_running", no_service)

    async def body():
        proxy = Proxy(free_port())
        try:
            await proxy.handle(json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/list"}))
            await proxy.handle(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        finally:
            await proxy._client.aclose()

    asyncio.run(body())
    replies = [json.loads(line) for line in raw.getvalue().decode("utf-8").splitlines() if line.strip()]
    assert len(replies) == 1
    assert replies[0]["id"] == 7
    assert "unreachable" in replies[0]["error"]["message"]


def test_ensure_running_reports_a_startup_crash_with_its_output(monkeypatch, tmp_path):
    import subprocess as sp

    from catia_mcp import service

    monkeypatch.setenv("CATIA_MCP_HOME", str(tmp_path))
    monkeypatch.setattr(service, "health", lambda *a, **k: None)

    def crashing(port):
        service.start_log_path().write_text("ImportError: DLL load failed: pywintypes\n")
        return sp.Popen(["python3", "-c", "raise SystemExit(3)"])

    monkeypatch.setattr(service, "start_detached", crashing)
    with pytest.raises(RuntimeError) as error:
        service.ensure_running(8765, wait=10)
    assert "exit code 3" in str(error.value) and "DLL load failed" in str(error.value)


def test_install_still_configures_clients_when_the_service_is_slow(monkeypatch, tmp_path):
    from catia_mcp import service

    monkeypatch.setattr(service, "set_autostart", lambda *a: "autostart")
    monkeypatch.setattr(service, "health", lambda *a, **k: None)

    def slow(port):
        raise RuntimeError("did not answer")

    monkeypatch.setattr(service, "ensure_running", slow)
    config = tmp_path / "claude_desktop_config.json"
    monkeypatch.setattr(service, "claude_desktop_configs", lambda: [config])
    monkeypatch.setattr(service, "configure_claude_code", lambda port: "claude code")
    lines = service.install(8765)
    assert any(line.startswith("SERVICE NOT RUNNING") for line in lines)
    assert config.exists()
