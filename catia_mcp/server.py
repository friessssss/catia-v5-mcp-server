"""MCP transport with one shared CATIA connection and one serial STA worker."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from jsonschema import ValidationError, validate
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ImageContent, TextContent, Tool

from catia_mcp import __version__
from catia_mcp import guard
from catia_mcp.com_thread import BUSY_HRESULTS, hresult, init_worker
from catia_mcp.connection import CATIAConnection, normalize_platform
from catia_mcp.errors import CATIAToolError
from catia_mcp.paths import build_commit, log_path
from catia_mcp.results import operation_result
from catia_mcp.tools.assembly import AssemblyTools
from catia_mcp.tools.diagnostics import DiagnosticsTools
from catia_mcp.tools.document import DocumentTools
from catia_mcp.tools.drawing import DraftingTools
from catia_mcp.tools.drawing_annotate import DrawingAnnotateTools
from catia_mcp.tools.drawing_read import DrawingReadTools
from catia_mcp.tools.export import ExportTools
from catia_mcp.tools.gsd import GSDTools
from catia_mcp.tools.introspection import IntrospectionTools
from catia_mcp.tools.knowledge import KnowledgeTools
from catia_mcp.tools.measurement import MeasurementTools
from catia_mcp.tools.part_design import PartDesignTools
from catia_mcp.tools.plm import PLMTools
from catia_mcp.tools.session import SessionTools
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

Shared database safety (3DEXPERIENCE): every modifying tool is refused with
WRITE_PROTECTED when the active object belongs to another user or is
Frozen/Released/Obsolete; only pass allow_protected_write=true when the user
explicitly asked to change that object. Before writes, read
catia_status.write_target and pass its id as target_id: the call is then
refused (TARGET_MISMATCH) if another window is in front. Never save
(catia_save_document publishes to the shared space) unless the user asked.

Drawings: read one whole with catia_drawing_read (views with axis origins,
notes, tables, title block, dimensions with frames) before editing. Place
annotations with the drawing tools in SHEET millimetres (they convert to the
view's axis-origin coordinates); basic dimensions via
catia_drawing_set_dimension_frame(frame="basic"). Generated view edges cannot
be dimensioned by automation: ask the user to place those dimensions in the
UI, then format them.

When no dedicated tool fits, use catia_run_python: it runs Python in the
server with the live COM objects (catia, part, editor, ...) and returns
printed output and a `result` value. Look members up first with
catia_inspect_api (live type library) or catia_api_search (the
installation's automation reference). catia_undo reverts mistakes.
"""


_SCRIPT_TOOLS = frozenset({"catia_run_python", "catia_run_script"})
_CREATES_CONTENT = frozenset({"catia_new_part", "catia_new_product", "catia_new_drawing"})

# Tools that manage the connection themselves: status and the API docs must
# answer while CATIA is not running.
_NO_AUTO_CONNECT = frozenset(
    {"catia_connect", "catia_disconnect", "catia_status", "catia_api_search", "catia_api_doc"}
)

_DOCUMENT_SWITCH = frozenset(
    {
        "catia_new_part",
        "catia_new_product",
        "catia_new_drawing",
        "catia_open_document",
        "catia_open_from_database",
        "catia_close_document",
        "catia_activate_document",
    }
)

_PY_ATTRIBUTE_ERROR = re.compile(
    r"^'(NoneType|str|int|float|bool|list|dict|tuple|bytes)' object has no attribute"
)

# Listed with CATIA_MCP_TOOLSET=core: the Fusion-style surface. The rest
# stays callable by name.
CORE_TOOLS = frozenset(
    {
        "catia_status",
        "catia_screenshot",
        "catia_set_view",
        "catia_run_python",
        "catia_inspect_api",
        "catia_api_search",
        "catia_api_doc",
        "catia_undo",
        "catia_redo",
        "catia_get_selection",
        "catia_drawing_read",
        "catia_list_documents",
        "catia_open_document",
        "catia_save_document",
        "catia_close_document",
        "catia_new_part",
        "catia_new_product",
        "catia_activate_document",
        "catia_get_active_document_info",
        "catia_list_features",
        "catia_get_inertia",
        "catia_measure_distance",
        "catia_measure_element",
        "catia_diagnose",
        "catia_open_from_database",
        "catia_search_database",
    }
)

_READ_ONLY = re.compile(
    r"^catia_(status|diagnose|inspect_api|api_search|api_doc|screenshot|fit_all|"
    r"(list|get|measure|search)_.*|.*_list_.*|.*_get_.*)$"
)
_DESTRUCTIVE = re.compile(
    r"^catia_(close_document|delete_.*|run_python|run_script|set_plm_attributes|"
    r"save_document|disconnect|.*_delete_.*)$"
)


def _script_reads_only(code: str) -> bool:
    import ast

    from catia_mcp.tools.introspection import _looks_read_only

    try:
        return _looks_read_only(ast.parse(code))
    except SyntaxError:
        return False


def is_guarded(definition: dict[str, Any]) -> bool:
    """Modifying tools that act on existing content go through the guard."""
    name = definition["name"]
    if name in guard.EXEMPT:
        return False
    hints = definition.get("annotations") or default_annotations(name)
    return not hints.get("readOnlyHint", False)


def _with_guard_args(definition: dict[str, Any]) -> dict[str, Any]:
    schema = dict(definition["inputSchema"])
    properties = dict(schema.get("properties") or {})
    for key, spec in guard.GUARD_SCHEMA.items():
        properties.setdefault(key, spec)
    schema["properties"] = properties
    return schema


def default_annotations(name: str) -> dict[str, Any]:
    """MCP tool hints derived from the tool's name (a definition may set its
    own). Reads change nothing; most writes add features without destroying
    existing ones."""
    if _READ_ONLY.match(name):
        return {"readOnlyHint": True, "openWorldHint": False}
    return {
        "readOnlyHint": False,
        "destructiveHint": bool(_DESTRUCTIVE.match(name)),
        "openWorldHint": False,
    }


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
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="catia-sta", initializer=init_worker
        )
        self.allow_scripts = bool(allow_scripts)
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
        self.drawing_read_tools = DrawingReadTools(self.connection)
        self.drawing_annotate_tools = DrawingAnnotateTools(self.connection)
        self.knowledge_tools = KnowledgeTools(self.connection)
        self.assembly_tools = AssemblyTools(self.connection)
        self.measurement_tools = MeasurementTools(self.connection)
        self.export_tools = ExportTools(self.connection)
        self.plm_tools = PLMTools(self.connection)
        self.introspection_tools = IntrospectionTools(self.connection, allow_scripts)
        self.session_tools = SessionTools(self.connection)
        self._tool_modules = [
            self.session_tools,
            self.document_tools,
            self.sketcher_tools,
            self.part_design_tools,
            self.gsd_tools,
            self.diagnostics_tools,
            self.drafting_tools,
            self.drawing_read_tools,
            self.drawing_annotate_tools,
            self.knowledge_tools,
            self.assembly_tools,
            self.measurement_tools,
            self.export_tools,
            self.plm_tools,
            self.introspection_tools,
        ]
        self._generation_seen = self.connection.generation
        self.toolset = os.environ.get("CATIA_MCP_TOOLSET", "full").strip().lower() or "full"
        self._tool_router: dict[str, Any] = {}
        self._schemas: dict[str, dict] = {}
        self._guarded: set[str] = set()
        for module in self._tool_modules:
            for definition in module.get_tool_definitions():
                name = definition["name"]
                if name in self._tool_router:
                    raise ValueError(f"Duplicate tool: {name}")
                self._tool_router[name] = module
                if is_guarded(definition):
                    self._guarded.add(name)
                self._schemas[name] = _with_guard_args(definition) if name in self._guarded else (
                    definition["inputSchema"]
                )
        self._setup_handlers()

    def _setup_handlers(self) -> None:
        @self.server.list_tools()
        async def handle_list_tools() -> list[Tool]:
            return self.list_tools()

        @self.server.call_tool(validate_input=False)
        async def handle_call_tool(name: str, arguments: dict[str, Any] | None) -> CallToolResult:
            return await self.call_tool(name, arguments or {})

    def list_tools(self) -> list[Tool]:
        """Every tool, annotated; CATIA_MCP_TOOLSET=core lists only the
        Fusion-style core (every tool stays callable by name)."""
        tools = []
        for module in self._tool_modules:
            for definition in module.get_tool_definitions():
                if self.toolset == "core" and definition["name"] not in CORE_TOOLS:
                    continue
                definition = dict(definition)
                definition.setdefault("annotations", default_annotations(definition["name"]))
                if definition["name"] in self._guarded:
                    definition["inputSchema"] = _with_guard_args(definition)
                tools.append(Tool(**definition))
        return tools

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
            "scripts": self.allow_scripts,
            "toolset": self.toolset,
            "commit": build_commit(),
            "log": str(log_path()),
        }

    def _record_connection(self) -> None:
        """Plain attribute reads only: a COM call here could block the
        worker after the busy marker is cleared."""
        conn = self.connection
        connected = conn.app is not None
        self._catia_state = {
            "connected": connected,
            "platform": conn.platform_label if connected else None,
            "caption": conn.caption if connected else None,
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
            try:
                self._record_connection()
            except Exception:  # noqa: BLE001 -- status only
                pass
            self._busy = None

    def _run_tool(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
        module = self._tool_router.get(name)
        if module is None:
            return failure("UNKNOWN_TOOL", f"Unknown tool: {name}", "validate", False, name)
        try:
            validate(arguments, self._schemas[name])
        except ValidationError as error:
            return failure("INVALID_ARGUMENT", error.message, "validate", False, name)
        logger.info("Tool call: %s(%s)", name, _preview(arguments))
        arguments = dict(arguments)
        target_id = arguments.pop("target_id", None)
        override = bool(arguments.pop("allow_protected_write", False))
        stage = "connect"
        try:
            if name not in _NO_AUTO_CONNECT:
                self.connection.ensure_connected()
            self._reset_stale_modules()
            if name in _DOCUMENT_SWITCH:
                # A sketch left open in the previous document must not be
                # closed or used as a profile in the next one.
                self.sketcher_tools.reset_session_state()
            stage = "connect" if name == "catia_connect" else "execute"
            target = None
            if self._needs_guard(name, arguments):
                target = self._guard(module, name, arguments, target_id, override)
            if name in _SCRIPT_TOOLS:
                self._log_script(name, arguments.get("code", ""), target)
            result = module.execute(name, arguments)
            if name in _SCRIPT_TOOLS and isinstance(result, dict):
                self._audit_script(arguments.get("code", ""), target, result)
            if name in _CREATES_CONTENT:
                self._learn_login()
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
                self.connection.drop()
                return failure(
                    "CATIA_NOT_RUNNING",
                    "CATIA closed or stopped responding during the call. Start CATIA "
                    "(log in on 3DEXPERIENCE) and retry; the server reattaches by itself.",
                    stage,
                    None,
                    name,
                )
            if hresult(error) in BUSY_HRESULTS:
                return failure(
                    "CATIA_BUSY",
                    "CATIA kept refusing the call because it is busy (a command, a "
                    "document load or a dialog). Finish or cancel it in CATIA, then retry."
                    + _dialog_hint(),
                    stage,
                    None,
                    name,
                )
            modified = False if stage == "connect" else None
            if isinstance(error, AttributeError) and _PY_ATTRIBUTE_ERROR.match(str(error)):
                # A plain Python object (often None where CATIA returned
                # nothing) lacks the member: not a missing CATIA API.
                return failure(
                    "INTERNAL_ERROR",
                    f"{error}. CATIA returned an unexpected empty or wrong-typed value, or "
                    "this is a server bug. Check the model state (catia_status) and the log.",
                    stage,
                    modified,
                    name,
                )
            if isinstance(error, AttributeError):
                message = unsupported_message(name, error, self.connection.is_3dx)
                return failure("UNSUPPORTED_CAPABILITY", message, stage, modified, name)
            if isinstance(error, ValueError):
                return failure("INVALID_ARGUMENT", str(error), stage, modified, name)
            return failure("COM_ERROR", str(error), stage, modified, name)

    def _needs_guard(self, name: str, arguments: dict[str, Any]) -> bool:
        """Guarded tools, plus the ones whose writing depends on arguments."""
        if name == "catia_save_document" and arguments.get("preview"):
            return False  # a preview writes nothing: let it describe protected content
        if name == "catia_close_document":
            return bool(arguments.get("save"))  # close-with-save publishes
        if name == "catia_run_python" and _script_reads_only(arguments.get("code", "")):
            # Reading a Released or someone else's drawing (pre-checks) is
            # safe; any assignment or modifying call keeps the guard on.
            return False
        if name == "catia_drawing_list_dimensions":
            # refresh (default) regenerates the views, which modifies them.
            return arguments.get("refresh", True) is not False
        return name in self._guarded

    def _guard(
        self,
        module: Any,
        name: str,
        arguments: dict[str, Any],
        target_id: str | None,
        override: bool,
    ) -> dict[str, Any] | None:
        """Write guard (catia_mcp/guard.py). A module may name the object a
        call really writes (write_target), e.g. a drawing found by title
        while another window is in front."""
        entity = None
        hook = getattr(module, "write_target", None)
        if hook is not None:
            # A typed failure here (no drawing to write) is the tool's own
            # failure too: report it before anything runs.
            entity = hook(name, arguments)
        try:
            target = guard.active_target(self.connection, entity)
        except Exception as error:  # noqa: BLE001 -- nothing open / readable to protect
            if target_id:
                raise guard.TargetMismatch(
                    f"{name} was meant for '{target_id}', but the object it would modify "
                    f"could not be identified ({error}). Nothing was changed."
                ) from error
            return None
        target = guard.check(self.connection, name, target_id, override, target)
        if override:
            logger.warning("Write guard overridden: %s on %s", name, target)
        return target

    def _learn_login(self) -> None:
        """New content is owned by the 3DEXPERIENCE login, which may differ
        from the Windows user name: learn it so the guard never blocks the
        user's own work."""
        if not self.connection.is_3dx:
            return
        try:
            guard.learn_login(guard.active_target(self.connection).get("owner"))
        except Exception:  # noqa: BLE001 -- best effort
            pass

    def _log_script(self, name: str, code: str, target: dict[str, Any] | None) -> None:
        """Logged before running, so a script that deletes and then fails is
        still on record."""
        calls = guard.risky_calls(code)
        if calls:
            where = (target or {}).get("title") or (target or {}).get("id") or "the active window"
            logger.warning("%s about to run %s with '%s' active", name, ", ".join(calls), where)

    def _audit_script(self, code: str, target: dict[str, Any] | None, result: dict) -> None:
        """Log and report deletes, saves and pastes a script performed."""
        calls = guard.risky_calls(code)
        if not calls:
            return
        where = (target or {}).get("title") or (target or {}).get("id") or "the active window"
        message = (
            f"Script called {', '.join(calls)} with '{where}' active: check that only "
            "the intended objects were affected."
        )
        logger.warning("catia_run_python audit: %s", message)
        result.setdefault("warnings", []).append(message)

    def _reset_stale_modules(self) -> None:
        """After CATIA was re-attached (restart, disconnect), drop the COM
        proxies modules cached from the previous process."""
        generation = self.connection.generation
        if generation == self._generation_seen:
            return
        self._generation_seen = generation
        for module in self._tool_modules:
            reset = getattr(module, "reset_session_state", None)
            if reset is None:
                continue
            try:
                reset()
            except Exception:  # noqa: BLE001 -- best effort
                logger.warning("reset_session_state failed for %s", type(module).__name__)

    async def close(self) -> None:
        """Release CATIA on the worker, but never wait on a worker stuck in
        CATIA (modal dialog): shutdown must stay bounded."""
        try:
            await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    self._executor, self.connection.disconnect
                ),
                timeout=5.0,
            )
        except asyncio.TimeoutError:
            logger.warning("CATIA worker still busy at shutdown; not waiting for it")
            self._executor.shutdown(wait=False, cancel_futures=True)
        else:
            self._executor.shutdown(wait=True)

    @property
    def worker_stuck(self) -> bool:
        return self._busy is not None

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


COMMANDS = ("serve", "proxy", "install", "uninstall", "update", "start", "stop", "status")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    from catia_mcp.http_transport import DEFAULT_HOST, DEFAULT_PORT

    parser = argparse.ArgumentParser(
        prog="catia-mcp",
        description=(
            "CATIA MCP server for CATIA V5 and CATIA 3DEXPERIENCE. Without a command it "
            "speaks MCP over stdio. Commands: install (background service that starts "
            "at logon + Claude client setup), serve (run the HTTP service in the "
            "foreground), proxy (stdio bridge to the service, used by Claude Desktop), "
            "update (install the newest version of the branch you installed; --check only "
            "compares), start, stop, status, uninstall."
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
        "--branch",
        default=None,
        help="update: branch to install (default: the one this copy came from)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="update: only report whether GitHub has a newer commit",
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
    if command in ("install", "uninstall", "update", "start", "stop", "status"):
        from catia_mcp import service

        if command == "install":
            lines = service.install(args.port, configure_clients=not args.no_clients)
        elif command == "uninstall":
            lines = service.uninstall(args.port)
        elif command == "update":
            lines = (
                service.update_check(args.branch)
                if args.check
                else service.update(args.branch, port=args.port)
            )
        elif command == "start":
            status = service.ensure_running(args.port)
            lines = [f"Service running at {service.mcp_url(args.port)} (PID {status.get('pid')})."]
        elif command == "stop":
            lines = [service.stop(args.port)]
        else:
            lines = service.status_lines(args.port)
        print("\n".join(lines))
        if any(line.startswith("SERVICE NOT RUNNING") for line in lines):
            sys.exit(1)
        return
    if command == "proxy":
        from catia_mcp.proxy import run_proxy

        # Its own file: a second process holding catia_mcp.log open would make
        # the service's log rotation fail on Windows.
        configure_logging(console=True, filename="proxy.log")
        run_proxy(args.port, args.token)
        return
    if command == "serve":
        from catia_mcp import service
        from catia_mcp.http_transport import check_bind

        configure_logging(console=True)
        check_bind(args.host, args.token)
        lock = service.acquire_instance_lock(args.port)
        running = service.health(args.port, token=args.token)
        if lock is None or running is not None:
            pid = running.get("pid") if running else "?"
            logger.info("A CATIA MCP service already runs on port %d (PID %s)", args.port, pid)
            print(f"Already running on port {args.port} (PID {pid}).", file=sys.stderr)
            return
        server = CATIAMCPServer(args.platform, args.allow_scripts)
        service.write_pid_file(args.port)
        try:
            asyncio.run(server.run_http(args.host, args.port, args.token))
        finally:
            service.clear_pid_file()
        if server.worker_stuck:
            # The worker thread is blocked inside CATIA and would keep the
            # interpreter from exiting; nothing else is left to clean up.
            logging.shutdown()
            os._exit(0)
        return
    configure_logging(console=True)
    asyncio.run(CATIAMCPServer(args.platform, args.allow_scripts).run())


if __name__ == "__main__":
    main()
