"""MCP transport with one shared CATIA connection and one serial STA worker."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from jsonschema import ValidationError, validate
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ImageContent, TextContent, Tool

from catia_mcp import __version__
from catia_mcp.connection import CATIAConnection, normalize_platform
from catia_mcp.errors import CATIAToolError
from catia_mcp.results import operation_result
from catia_mcp.tools.assembly import AssemblyTools
from catia_mcp.tools.diagnostics import DiagnosticsTools
from catia_mcp.tools.document import DocumentTools
from catia_mcp.tools.drawing import DraftingTools
from catia_mcp.tools.export import ExportTools
from catia_mcp.tools.gsd import GSDTools
from catia_mcp.tools.introspection import IntrospectionTools
from catia_mcp.tools.knowledge import KnowledgeTools
from catia_mcp.tools.measurement import MeasurementTools
from catia_mcp.tools.part_design import PartDesignTools
from catia_mcp.tools.plm import PLMTools
from catia_mcp.tools.sketcher import SketcherTools

logger = logging.getLogger("catia_mcp")


def structured_result(data: dict[str, Any]) -> CallToolResult:
    return CallToolResult(
        content=[
            TextContent(
                type="text", text=json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2)
            )
        ],
        structuredContent=data,
        isError=data.get("ok") is not True,
    )


def failure(
    code: str,
    message: str,
    stage: str,
    modified: bool | None,
    tool: str,
    data: dict[str, Any] | None = None,
) -> CallToolResult:
    return structured_result(
        operation_result(
            tool=tool,
            ok=False,
            code=code,
            message=message,
            data={"failure_stage": stage, **(data or {})},
            modified=modified,
        )
    )


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


INSTRUCTIONS = """\
Drives the user's running CATIA (V5 or 3DEXPERIENCE, detected on connect)
through COM. Calls are serialized; CATIA must be running (and logged in, on
3DEXPERIENCE) - the server attaches by itself, no connect call is needed.

Work in small verified steps: create or open content, act, then check with
catia_get_model_state (tree, bodies, volume, errors) or catia_screenshot,
which returns the picture of the CATIA view so you can see the result.
Failures come back as JSON with a stable `code` (INVALID_ARGUMENT,
NO_ACTIVE_DOCUMENT, WRONG_DOCUMENT_TYPE, OBJECT_NOT_FOUND,
UNSUPPORTED_CAPABILITY, FEATURE_NO_EFFECT, CATIA_BUSY, CATIA_TIMEOUT,
CATIA_NOT_RUNNING, COM_ERROR) and `effects.modified` says whether the model
may have changed.

When no dedicated tool fits, use catia_run_python: it runs Python in the
server with the live COM objects (catia, part, editor, ...) and returns
printed output and a `result` value. Look members up first with
catia_inspect_api (live type library) or catia_api_search (the
installation's automation reference). catia_undo reverts mistakes.
"""


def _preview(arguments: dict[str, Any]) -> str:
    text = repr(arguments)
    return text if len(text) <= 500 else text[:500] + "..."


def _dialog_hint() -> str:
    try:
        from catia_mcp.winutil import catia_dialog_titles

        titles = catia_dialog_titles()
    except Exception:  # noqa: BLE001 -- hint only
        return ""
    if not titles:
        return ""
    return " Open CATIA dialog(s): " + "; ".join(repr(t) for t in titles) + "."


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def unsupported_message(name: str, error: Exception, is_3dx: bool) -> str:
    message = (
        f"UNSUPPORTED_CAPABILITY in {name}: the required CATIA automation API "
        f"did not resolve ({error}). The API may be absent from this CATIA release, "
        "or a stale pywin32 gen_py cache may be blocking resolution. Clearing caches "
        "cannot add an absent API. For a stale cache, close CATIA and the server, "
        "delete the %TEMP%\\gen_py folder, restart and retry once. A missing "
        "workbench licence can also lock methods. Run catia_diagnose for details."
    )
    if is_3dx:
        message += (
            " On 3DEXPERIENCE, member names can differ from V5: catia_inspect_api lists "
            "what this installation exposes, and DSYAutomation.chm (win_b64\\code\\bin) "
            "documents it. Your role/licence also gates some services."
        )
    return message


class CATIAMCPServer:
    def __init__(self, platform: str | None = None, allow_scripts: bool | None = None) -> None:
        self.server = Server("catia-mcp", version=__version__, instructions=INSTRUCTIONS)
        self.connection = CATIAConnection(platform)
        if allow_scripts is None:
            allow_scripts = _env_flag("CATIA_MCP_ALLOW_SCRIPTS")
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="catia-sta")
        self._started = time.monotonic()
        self._busy: tuple[str, float] | None = None
        self._calls = 0
        self._catia_state: dict[str, Any] = {"connected": False, "platform": None, "caption": None}
        # A call still running after call_timeout is reported to the client;
        # while the worker stays stuck past busy_grace, new calls fail fast.
        self.call_timeout = _env_float("CATIA_MCP_CALL_TIMEOUT", 600.0)
        self.busy_grace = _env_float("CATIA_MCP_BUSY_GRACE", 30.0)
        self.document_tools = DocumentTools(self.connection)
        self.sketcher_tools = SketcherTools(self.connection)
        self.part_design_tools = PartDesignTools(self.connection)
        self.gsd_tools = GSDTools(self.connection)
        self.diagnostics_tools = DiagnosticsTools(self.connection)
        self.drafting_tools = DraftingTools(self.connection)
        self.knowledge_tools = KnowledgeTools(self.connection)
        self.assembly_tools = AssemblyTools(self.connection)
        self.measurement_tools = MeasurementTools(self.connection)
        self.export_tools = ExportTools(self.connection)
        self.plm_tools = PLMTools(self.connection)
        self.introspection_tools = IntrospectionTools(self.connection, allow_scripts)
        self._tool_modules = [
            self.document_tools,
            self.sketcher_tools,
            self.part_design_tools,
            self.gsd_tools,
            self.diagnostics_tools,
            self.drafting_tools,
            self.knowledge_tools,
            self.assembly_tools,
            self.measurement_tools,
            self.export_tools,
            self.plm_tools,
            self.introspection_tools,
        ]
        self._tool_router: dict[str, Any] = {}
        self._schemas: dict[str, dict] = {}
        for module in self._tool_modules:
            for definition in module.get_tool_definitions():
                name = definition["name"]
                if name in self._tool_router:
                    raise ValueError(f"Duplicate tool: {name}")
                self._tool_router[name] = module
                self._schemas[name] = definition["inputSchema"]
        self._setup_handlers()

    def _setup_handlers(self) -> None:
        @self.server.list_tools()
        async def handle_list_tools() -> list[Tool]:
            return [
                Tool(**definition)
                for module in self._tool_modules
                for definition in module.get_tool_definitions()
            ]

        @self.server.call_tool(validate_input=False)
        async def handle_call_tool(name: str, arguments: dict[str, Any] | None) -> CallToolResult:
            return await self.call_tool(name, arguments or {})

    # ── Runtime state (read by /health and the busy guard) ──

    def status(self) -> dict[str, Any]:
        """Health payload. Never touches COM: it reads what the worker last
        recorded, so it answers even while CATIA is blocked."""
        busy = None
        if self._busy is not None:
            tool, since = self._busy
            busy = {"tool": tool, "seconds": round(time.monotonic() - since, 1)}
        return {
            "service": "catia-mcp",
            "status": "ok",
            "version": __version__,
            "pid": os.getpid(),
            "uptime_s": round(time.monotonic() - self._started, 1),
            "tools": len(self._tool_router),
            "calls": self._calls,
            "busy": busy,
            "catia": dict(self._catia_state),
        }

    def _record_connection(self) -> None:
        conn = self.connection
        connected = conn.app is not None
        self._catia_state = {
            "connected": connected,
            "platform": conn.platform_label if connected else None,
            "caption": conn._get_version() if connected else None,
        }

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
        if self._busy is not None:
            tool, since = self._busy
            waited = time.monotonic() - since
            if waited > self.busy_grace:
                # The worker is still inside an earlier call (typically a CATIA
                # modal dialog). Queuing more work behind it would hang the
                # client too, so answer now.
                return failure(
                    "CATIA_BUSY",
                    f"CATIA has been busy with {tool} for {waited:.0f} s, so this call was "
                    "not started. CATIA is probably showing a dialog or still computing: "
                    "look at the CATIA window, close any dialog, then retry."
                    + _dialog_hint(),
                    "queue",
                    False,
                    name,
                    {"busy_tool": tool, "busy_seconds": round(waited, 1)},
                )
        future = asyncio.get_running_loop().run_in_executor(
            self._executor, self._execute_tool, name, arguments
        )
        try:
            return await asyncio.wait_for(asyncio.shield(future), self.call_timeout)
        except asyncio.TimeoutError:
            future.add_done_callback(
                lambda completed: completed.exception() if not completed.cancelled() else None
            )
            return failure(
                "CATIA_TIMEOUT",
                f"{name} did not finish within {self.call_timeout:.0f} s. It is still running "
                "inside CATIA and may yet complete; CATIA may be waiting on a dialog. Check "
                "the CATIA window, then inspect the model before repeating a write."
                + _dialog_hint(),
                "execute",
                None,
                name,
            )
        except asyncio.CancelledError:
            future.add_done_callback(
                lambda completed: completed.exception() if not completed.cancelled() else None
            )
            raise

    def _execute_tool(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
        self._busy = (name, time.monotonic())
        self._calls += 1
        try:
            return self._run_tool(name, arguments)
        finally:
            self._busy = None
            try:
                self._record_connection()
            except Exception:  # noqa: BLE001 -- status only
                pass

    def _run_tool(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
        module = self._tool_router.get(name)
        if module is None:
            return failure("UNKNOWN_TOOL", f"Unknown tool: {name}", "validate", False, name)
        try:
            validate(arguments, self._schemas[name])
        except ValidationError as error:
            return failure("INVALID_ARGUMENT", error.message, "validate", False, name)
        logger.info("Tool call: %s(%s)", name, _preview(arguments))
        stage = "connect"
        try:
            if name not in {"catia_connect", "catia_disconnect"}:
                self.connection.ensure_connected()
            stage = "connect" if name == "catia_connect" else "execute"
            result = module.execute(name, arguments)
            if isinstance(result, CallToolResult):
                return result
            if isinstance(result, dict):
                logger.info("Tool result: %s %s", name, result.get("code"))
                return structured_result(result)
            if isinstance(result, list):
                return CallToolResult(content=result)
            logger.info("Tool result: %s", result[:200])
            return CallToolResult(content=[TextContent(type="text", text=result)])
        except CATIAToolError as error:
            logger.warning("Tool failed: %s: %s %s", name, error.code, error)
            return failure(error.code, str(error), stage, error.modified, name)
        except Exception as error:
            logger.exception("Tool failed: %s: %s", name, error)  # noqa: TRY401 -- log preview includes the error message
            if self.connection.lost(error):
                self.connection.app = None
                self.connection.platform = None
                return failure(
                    "CATIA_NOT_RUNNING",
                    "CATIA closed or stopped responding during the call. Start CATIA "
                    "(log in on 3DEXPERIENCE) and retry; the server reattaches by itself.",
                    stage,
                    None,
                    name,
                )
            code = "UNSUPPORTED_CAPABILITY" if isinstance(error, AttributeError) else "COM_ERROR"
            message = str(error)
            if code == "UNSUPPORTED_CAPABILITY":
                message = unsupported_message(name, error, self.connection.is_3dx)
            return failure(code, message, stage, False if stage == "connect" else None, name)

    async def close(self) -> None:
        try:
            await asyncio.get_running_loop().run_in_executor(
                self._executor, self.connection.disconnect
            )
        finally:
            self._executor.shutdown(wait=True)

    async def run(self) -> None:
        logger.info("Starting CATIA MCP Server: %d tools", len(self._tool_router))
        try:
            async with stdio_server() as (read_stream, write_stream):
                await self.server.run(
                    read_stream, write_stream, self.server.create_initialization_options()
                )
        finally:
            await self.close()


    async def run_http(self, host: str, port: int, token: str | None) -> None:
        from catia_mcp.http_transport import serve

        logger.info("Starting CATIA MCP Server (HTTP): %d tools", len(self._tool_router))
        try:
            await serve(self.server, host, port, token, self.status)
        finally:
            await self.close()


COMMANDS = ("serve", "proxy", "install", "uninstall", "start", "stop", "status")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    from catia_mcp.http_transport import DEFAULT_HOST, DEFAULT_PORT

    parser = argparse.ArgumentParser(
        prog="catia-mcp",
        description=(
            "CATIA MCP server for CATIA V5 and CATIA 3DEXPERIENCE. Without a command it "
            "speaks MCP over stdio. Commands: install (background service that starts "
            "at logon + Claude client setup), serve (run the HTTP service in the "
            "foreground), proxy (stdio bridge to the service, used by Claude Desktop), "
            "start, stop, status, uninstall."
        ),
    )
    parser.add_argument("command", nargs="?", choices=COMMANDS, help="Optional command")
    parser.add_argument(
        "--http",
        action="store_true",
        help="Serve over Streamable HTTP at /mcp instead of stdio (same as 'serve')",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="Bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port (default 8765)")
    parser.add_argument(
        "--token",
        default=os.environ.get("CATIA_MCP_TOKEN"),
        help="Bearer token required from clients (default: CATIA_MCP_TOKEN env var). "
        "Mandatory when --host is not a loopback address.",
    )
    parser.add_argument(
        "--new-token", action="store_true", help="Print a fresh random token and exit"
    )
    parser.add_argument(
        "--platform",
        default=os.environ.get("CATIA_MCP_PLATFORM", "auto"),
        help="CATIA platform: auto (default, detected on connect), v5 or 3dx "
        "(default: CATIA_MCP_PLATFORM env var)",
    )
    scripts = parser.add_mutually_exclusive_group()
    scripts.add_argument(
        "--allow-scripts",
        action="store_true",
        default=None,
        help="Enable the script tools (catia_run_python, catia_run_script) even when "
        "listening beyond this machine. They are on by default for stdio and loopback "
        "(env: CATIA_MCP_ALLOW_SCRIPTS=1/0).",
    )
    scripts.add_argument(
        "--no-scripts",
        dest="allow_scripts",
        action="store_false",
        help="Disable the script tools.",
    )
    parser.add_argument(
        "--no-clients",
        action="store_true",
        help="install: do not configure Claude Desktop / Claude Code",
    )
    args = parser.parse_args(argv)
    try:
        normalize_platform(args.platform)
    except ValueError as error:
        parser.error(str(error))
    if args.allow_scripts is None:
        env = os.environ.get("CATIA_MCP_ALLOW_SCRIPTS")
        if env is not None and env.strip():
            args.allow_scripts = _env_flag("CATIA_MCP_ALLOW_SCRIPTS")
        else:
            from catia_mcp.http_transport import is_loopback

            # Like Fusion's MCP: full scripting for local agents; a server
            # reachable from the network needs an explicit opt-in.
            args.allow_scripts = not (args.http or args.command == "serve") or is_loopback(
                args.host
            )
    return args


def main(argv: list[str] | None = None) -> None:
    from catia_mcp.paths import configure_logging

    args = parse_args(argv)
    if args.new_token:
        from catia_mcp.http_transport import new_token

        print(new_token())
        return
    command = "serve" if args.http and args.command is None else args.command
    if command in ("install", "uninstall", "start", "stop", "status"):
        from catia_mcp import service

        if command == "install":
            lines = service.install(args.port, configure_clients=not args.no_clients)
        elif command == "uninstall":
            lines = service.uninstall(args.port)
        elif command == "start":
            status = service.ensure_running(args.port)
            lines = [f"Service running at {service.mcp_url(args.port)} (PID {status.get('pid')})."]
        elif command == "stop":
            lines = [service.stop(args.port)]
        else:
            lines = service.status_lines(args.port)
        print("\n".join(lines))
        return
    if command == "proxy":
        from catia_mcp.proxy import run_proxy

        configure_logging(console=True)
        run_proxy(args.port, args.token)
        return
    if command == "serve":
        from catia_mcp import service
        from catia_mcp.http_transport import check_bind

        configure_logging(console=True)
        check_bind(args.host, args.token)
        running = service.health(args.port, token=args.token)
        if running is not None:
            logger.info("A CATIA MCP service already runs on port %d (PID %s)", args.port, running.get("pid"))
            print(f"Already running on port {args.port} (PID {running.get('pid')}).", file=sys.stderr)
            return
        server = CATIAMCPServer(args.platform, args.allow_scripts)
        service.write_pid_file(args.port)
        try:
            asyncio.run(server.run_http(args.host, args.port, args.token))
        finally:
            service.clear_pid_file()
        return
    configure_logging(console=True)
    asyncio.run(CATIAMCPServer(args.platform, args.allow_scripts).run())


if __name__ == "__main__":
    main()
