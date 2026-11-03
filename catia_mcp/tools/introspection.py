"""API discovery tools: inspect the live automation API, run opt-in scripts.

CATIA automation signatures and enum values drift between releases, and the
3DEXPERIENCE API is documented only in each installation's
DSYAutomation.chm. catia_inspect_api reads the type information of live
objects (read-only), so an agent can find the exact member names,
parameters and enum values of the user's CATIA before calling them.

catia_run_script runs VBScript inside CATIA to bridge gaps no tool covers.
It is disabled unless the server was started with --allow-scripts (or
CATIA_MCP_ALLOW_SCRIPTS=1), because a script can do anything the user can.
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import math
import re
import traceback
from typing import Any

from catia_mcp.automation import (
    call_fill_array,
    call_out_object,
    call_out_values,
    evaluate,
    get_member,
)
from catia_mcp.connection import CATIAConnection
from catia_mcp.errors import CATIAToolError, InvalidArgument, ObjectNotFound, PermissionDenied
from catia_mcp.introspect import describe_members, find_enum, list_enums, type_name

# Getter-like methods allowed in an inspection path; no other call is made.
_ALLOWED_CALLS = {
    "item", "getitem", "getservice", "getsessionservice", "getworkbench",
    "gettechnologicalobject", "referencerootoccurrenceof", "referenceinstanceof",
    "instanceoccurrenceof",
}
_SEGMENT = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)(?:\((?:(-?\d+)|"([^"]*)")?\))?')
_ROOTS = ("app", "editor", "document", "part", "product", "selection", "window")
_MAX_OUTPUT = 40_000


def _parse_path(path: str) -> list[tuple[str, bool, Any]]:
    segments: list[tuple[str, bool, Any]] = []
    pos = 0
    text = path.strip()
    while pos < len(text):
        match = _SEGMENT.match(text, pos)
        if not match:
            raise InvalidArgument(f"Cannot parse inspection path at: {text[pos:]!r}")
        name, number, string = match.groups()
        called = match.group(0).endswith(")")
        arg = int(number) if number is not None else string
        segments.append((name, called, arg))
        pos = match.end()
        if pos < len(text):
            if text[pos] != ".":
                raise InvalidArgument(f"Expected '.' at: {text[pos:]!r}")
            pos += 1
    if not segments:
        raise InvalidArgument("Empty inspection path.")
    return segments


def _jsonable(value: Any, depth: int = 0) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)) and depth < 3:
        return [_jsonable(v, depth + 1) for v in value]
    if hasattr(value, "_oleobj_"):
        return f"<COM object {type_name(value) or 'unknown type'}>"
    return str(value)


def _clip(text: str) -> str:
    if len(text) <= _MAX_OUTPUT:
        return text
    return text[:_MAX_OUTPUT] + f"\n... ({len(text) - _MAX_OUTPUT} more characters cut)"


class IntrospectionTools:
    """Read-only API inspection and opt-in script execution."""

    def __init__(self, connection: CATIAConnection, allow_scripts: bool = False) -> None:
        self.conn = connection
        self.allow_scripts = allow_scripts
        # Survives between catia_run_python calls (like a notebook kernel).
        self._session: dict[str, Any] = {}

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "catia_inspect_api",
                "description": (
                    "Read-only discovery of the live CATIA automation API: resolve an "
                    "object path and list its properties and methods (with parameter "
                    "names and directions) from the installation's type library, and "
                    "read enum values. Use it when a tool reports "
                    "UNSUPPORTED_CAPABILITY or before scripting. Path roots: app, "
                    "editor (3DEXPERIENCE), document, part, product, selection, window; "
                    "segments are properties or the getters Item(n|\"name\"), "
                    "GetItem(\"X\"), GetService(\"X\"), GetSessionService(\"X\"), "
                    "GetWorkbench(\"X\"). Examples: 'part.ShapeFactory', "
                    "'editor.GetService(\"MeasurableService\")', "
                    "'app.ActiveEditor.ActiveObject'."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Object path, e.g. 'part.HybridShapeFactory'"},
                        "filter": {
                            "type": "string",
                            "description": "Case-insensitive substring filter on member names",
                        },
                        "enum": {
                            "type": "string",
                            "description": "Enum to read from the object's type library, e.g. 'CATMeasurableType'",
                        },
                        "list_enums": {
                            "type": "boolean",
                            "description": "List the enum names declared in the object's type library",
                        },
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "catia_run_python",
                "description": (
                    "Run Python inside the server against the live CATIA automation API "
                    "(the escape hatch for anything no dedicated tool covers - like "
                    "Fusion's script execution). Predefined names: catia (the "
                    "Application), conn (connection helpers), platform ('v5'/'3dx'), "
                    "part(), body(), document(), editor(), product(), selection() "
                    "(resolve lazily, raise if absent), win32com, math, json, and "
                    "session (a dict kept between calls). Methods that return values "
                    "through ByRef/array parameters read back as None or zeros under "
                    "pywin32; call them through CATIA's script engine instead: "
                    "fill_array(obj, 'GetCoordinates', 3, *inputs) -> [x, y, z], "
                    "out_values(obj, 'GetDirection', 3) -> scalar outs, "
                    "out_object(obj, 'GetLink', *inputs) -> object out, "
                    "vbscript(code, 'Fn', *args) for raw VBScript. Print to report, or set "
                    "`result` (or end with an expression) to return a value. COM "
                    "objects print as their type. Calls run on CATIA's thread, so keep "
                    "loops bounded; call part().Update() after changes and verify with a "
                    "measurement or catia_screenshot. Use catia_inspect_api / "
                    "catia_api_search to find member names. Example: "
                    "\"p = part(); print([b.Name for b in p.Bodies])\". Requires scripts "
                    "to be enabled (default for local use)."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string", "description": "Python source to run"},
                    },
                    "required": ["code"],
                },
            },
            {
                "name": "catia_run_script",
                "description": (
                    "Run a VBScript function inside CATIA through "
                    "SystemService.Evaluate and return its result. Disabled unless the "
                    "server was started with --allow-scripts (or "
                    "CATIA_MCP_ALLOW_SCRIPTS=1): a script runs with the user's full "
                    "rights in CATIA and on the machine. The global CATIA object is "
                    "available inside the script. Model changes are not verified."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "code": {
                            "type": "string",
                            "description": "VBScript source defining the function to call",
                        },
                        "function": {
                            "type": "string",
                            "description": "Name of the function to call (default 'CATMain')",
                        },
                        "args": {
                            "type": "array",
                            "items": {"type": ["string", "number", "boolean"]},
                            "description": "Arguments passed to the function",
                        },
                    },
                    "required": ["code"],
                },
            },
        ]

    def execute(self, tool_name: str, arguments: dict[str, Any]) -> str:
        match tool_name:
            case "catia_inspect_api":
                return self._inspect(arguments)
            case "catia_run_script":
                return self._run_script(arguments)
            case "catia_run_python":
                return self._run_python(arguments)
            case _:
                raise ValueError(f"Unknown introspection tool: {tool_name}")

    def _root(self, name: str) -> Any:
        key = name.lower()
        if key == "app":
            return self.conn.app
        if key == "editor":
            return self.conn.active_editor
        if key == "document":
            return self.conn.active_document
        if key == "part":
            return self.conn.get_active_part()
        if key == "product":
            return self.conn.get_active_product()
        if key == "selection":
            return self.conn.hso
        if key == "window":
            return self.conn.active_window
        raise InvalidArgument(f"Unknown path root '{name}'. Use one of: {', '.join(_ROOTS)}.")

    def _resolve(self, path: str) -> Any:
        segments = _parse_path(path)
        root_name, root_called, _ = segments[0]
        if root_called:
            raise InvalidArgument("The path root cannot be a call.")
        obj = self._root(root_name)
        walked = root_name
        for name, called, arg in segments[1:]:
            if called and name.lower() not in _ALLOWED_CALLS:
                raise InvalidArgument(
                    f"'{name}(...)' is not an allowed getter. Allowed calls: Item, GetItem, "
                    "GetService, GetSessionService, GetWorkbench, GetTechnologicalObject, "
                    "ReferenceRootOccurrenceOf, ReferenceInstanceOf, InstanceOccurrenceOf."
                )
            try:
                if called and arg is not None:
                    obj = getattr(obj, name)(arg)
                else:
                    obj = get_member(obj, name)
            except AttributeError:
                raise ObjectNotFound(f"'{walked}' has no member '{name}'.")
            except Exception as e:
                raise ObjectNotFound(f"Reading '{walked}.{name}' failed: {e}")
            walked += f".{name}"
        return obj

    def _inspect(self, args: dict[str, Any]) -> str:
        self.conn.ensure_connected()
        obj = self._resolve(args["path"])
        result: dict[str, Any] = {"path": args["path"], "platform": self.conn.platform_label}
        if not hasattr(obj, "_oleobj_"):
            result["value"] = _jsonable(obj)
            return json.dumps(result, indent=2, ensure_ascii=False)
        result["type"] = type_name(obj)
        if args.get("enum"):
            values = find_enum([obj, self.conn.app], args["enum"])
            result["enum"] = {args["enum"]: values} if values else (
                f"Enum '{args['enum']}' not found in this object's type library."
            )
        if args.get("list_enums"):
            result["enums"] = list_enums([obj])
        if not args.get("enum") or args.get("filter"):
            members = describe_members(obj)
            needle = (args.get("filter") or "").lower()
            if needle:
                members = [m for m in members if needle in m["name"].lower()]
            result["members"] = members
        return json.dumps(result, indent=2, ensure_ascii=False)

    def _require_scripts(self, tool: str) -> None:
        if not self.allow_scripts:
            raise PermissionDenied(
                f"{tool} is disabled on this server (it was started with --no-scripts, "
                "CATIA_MCP_ALLOW_SCRIPTS=0, or listens beyond this machine without "
                "--allow-scripts). Scripts run with the user's full rights in CATIA and "
                "on the machine."
            )

    def _namespace(self) -> dict[str, Any]:
        conn = self.conn

        def document() -> Any:
            return conn.active_document

        def editor() -> Any:
            return conn.active_editor

        app = conn.app

        def fill_array(obj: Any, method: str, count: int, *inputs: Any) -> list[float]:
            return call_fill_array(app, obj, method, count, *inputs)

        def out_values(obj: Any, method: str, count: int, *inputs: Any) -> list[Any]:
            return call_out_values(app, obj, method, count, *inputs)

        def out_object(obj: Any, method: str, *inputs: Any) -> Any:
            return call_out_object(app, obj, method, *inputs)

        def vbscript(code: str, function: str = "CATMain", *args: Any) -> Any:
            return evaluate(app, code, function, list(args))

        namespace: dict[str, Any] = {
            # ByRef/SAFEARRAY out-parameters come back empty through pywin32's
            # late binding (field report 2026-11-02: GetLink, GetCoordinates,
            # GetDirection); these run the call inside CATIA's script engine,
            # the route the measurement tools already rely on.
            "fill_array": fill_array,
            "out_values": out_values,
            "out_object": out_object,
            "vbscript": vbscript,
            "catia": conn.app,
            "app": conn.app,
            "conn": conn,
            "platform": conn.platform,
            "part": conn.get_active_part,
            "body": conn.get_active_part_body,
            "product": conn.get_active_product,
            "document": document,
            "editor": editor,
            "selection": lambda: conn.hso,
            "math": math,
            "json": json,
            "session": self._session,
            "__name__": "__catia__",
        }
        try:
            import win32com.client

            namespace["win32com"] = win32com
        except ImportError:
            pass
        return namespace

    def _run_python(self, args: dict[str, Any]) -> dict[str, Any]:
        self._require_scripts("catia_run_python")
        self.conn.ensure_connected()
        code = args["code"]
        try:
            tree = ast.parse(code, "<catia_run_python>", "exec")
        except SyntaxError as error:
            raise InvalidArgument(f"Python syntax error: {error}") from error
        tail = None
        if tree.body and isinstance(tree.body[-1], ast.Expr):
            tail = ast.Expression(tree.body.pop().value)
        namespace = self._namespace()
        out = io.StringIO()
        value: Any = None
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                exec(compile(tree, "<catia_run_python>", "exec"), namespace)  # noqa: S102 -- opt-in tool
                if tail is not None:
                    value = eval(compile(tail, "<catia_run_python>", "eval"), namespace)  # noqa: S307
            except Exception as error:  # noqa: BLE001 -- reported with traceback
                printed = _clip(out.getvalue())
                trace = traceback.format_exc(limit=-6)
                raise CATIAToolError(
                    f"{type(error).__name__}: {error}\n\nOutput before the error:\n"
                    f"{printed or '(none)'}\n\nTraceback:\n{_clip(trace)}",
                    code="SCRIPT_ERROR",
                    modified=None,
                ) from error
        if "result" in namespace:
            value = namespace["result"]
        from catia_mcp.results import operation_result

        return operation_result(
            tool="catia_run_python",
            ok=True,
            code="OK",
            message=_clip(out.getvalue()) or "(no output)",
            data={"result": _jsonable(value), "stdout": _clip(out.getvalue())},
            modified=None,
        )

    def _run_script(self, args: dict[str, Any]) -> str:
        self._require_scripts("catia_run_script")
        self.conn.ensure_connected()
        function = args.get("function") or "CATMain"
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", function):
            raise InvalidArgument(f"Invalid function name {function!r}.")
        result = evaluate(self.conn.app, args["code"], function, list(args.get("args") or []))
        return json.dumps({"function": function, "result": _jsonable(result)}, indent=2, ensure_ascii=False)
