"""Session tools: cheap status, selection, undo/redo and API docs search.

These give an agent the orientation loop the Fusion MCP offers: look at
what CATIA is showing (catia_status, catia_get_selection), step back
(catia_undo / catia_redo) and read the installed automation reference
(catia_api_search / catia_api_doc) before writing calls.

catia_status never raises for a missing piece: anything CATIA cannot report
(no document, no part, SystemConfiguration absent) comes back as null.
Undo/redo go through Application.StartCommand with the English command
names; that is not field-proven on 3DEXPERIENCE R428 and localized UIs may
use other names, so these tools report what was issued, never that the
model changed.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path, PurePath
from typing import Any

from catia_mcp import apidocs
from catia_mcp.automation import name_exists
from catia_mcp.connection import CATIAConnection
from catia_mcp.errors import InvalidArgument, UnsupportedCapability
from catia_mcp.introspect import type_name
from catia_mcp.results import operation_result

_MAX_STEPS = 20
_V5_KINDS = {
    ".catpart": "part",
    ".catproduct": "product",
    ".catdrawing": "drawing",
    ".catprocess": "process",
    ".catanalysis": "analysis",
    ".catmaterial": "material",
}

_READ_ONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}


def _try(getter: Callable[[], Any]) -> Any:
    """Value of getter(), or None when CATIA cannot provide it."""
    try:
        return getter()
    except Exception:  # noqa: BLE001 -- status reports missing pieces as null
        return None


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def _int_arg(arguments: dict[str, Any], name: str, default: int, lo: int, hi: int) -> int:
    value = arguments.get(name)
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as e:
        raise InvalidArgument(f"{name} must be an integer, got {value!r}.") from e
    if not lo <= number <= hi:
        raise InvalidArgument(f"{name} must be between {lo} and {hi}.")
    return number


class SessionTools:
    """Status, selection, undo/redo and installed API documentation."""

    def __init__(self, connection: CATIAConnection) -> None:
        self.conn = connection
        self.docs = apidocs.ApiDocs()

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "catia_status",
                "description": (
                    "Cheap snapshot of the CATIA session: connected or not, platform "
                    "(V5 / 3DEXPERIENCE), release, the active document or editor and "
                    "its kind (part/product/drawing), the in-work object, selection "
                    "count and open windows. Call it first and whenever you are unsure "
                    "what CATIA is showing. Missing pieces are null; never fails."
                ),
                "inputSchema": {"type": "object", "properties": {}},
                "annotations": {"title": "CATIA session status", **_READ_ONLY},
            },
            {
                "name": "catia_get_selection",
                "description": (
                    "List what the user has selected in CATIA: each item's name, "
                    "automation type, reference and leaf product. Use it to act on "
                    "\"this face / this feature\" the user picked by hand."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "max_items": {
                            "type": "integer",
                            "description": "Maximum items to list (default 100)",
                            "default": 100,
                            "minimum": 1,
                            "maximum": 1000,
                        },
                    },
                },
                "annotations": {"title": "Get the CATIA selection", **_READ_ONLY},
            },
            {
                "name": "catia_undo",
                "description": (
                    "Undo the last CATIA operation(s) by issuing CATIA's Undo command "
                    "'steps' times. The result is NOT verified: confirm with "
                    "catia_screenshot or a read tool afterwards."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "steps": {
                            "type": "integer",
                            "description": "Number of undo steps (1-20, default 1)",
                            "default": 1,
                            "minimum": 1,
                            "maximum": _MAX_STEPS,
                        },
                    },
                },
                "annotations": {
                    "title": "Undo in CATIA",
                    "readOnlyHint": False,
                    "destructiveHint": True,
                    "idempotentHint": False,
                    "openWorldHint": False,
                },
            },
            {
                "name": "catia_redo",
                "description": (
                    "Redo operation(s) undone with catia_undo by issuing CATIA's Redo "
                    "command 'steps' times. The result is NOT verified: confirm with "
                    "catia_screenshot or a read tool afterwards."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "steps": {
                            "type": "integer",
                            "description": "Number of redo steps (1-20, default 1)",
                            "default": 1,
                            "minimum": 1,
                            "maximum": _MAX_STEPS,
                        },
                    },
                },
                "annotations": {
                    "title": "Redo in CATIA",
                    "readOnlyHint": False,
                    "destructiveHint": True,
                    "idempotentHint": False,
                    "openWorldHint": False,
                },
            },
            {
                "name": "catia_api_search",
                "description": (
                    "Search the automation API reference shipped with the user's CATIA "
                    "(DSYAutomation.chm / V5Automation.chm) by regex over page titles "
                    "and text, case-insensitive. Returns page names and snippets; read "
                    "one with catia_api_doc. Use it before writing catia_run_python or "
                    "VBScript against an unfamiliar object. First call extracts and "
                    "indexes the CHM (can take a minute); later calls are instant."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Regex, e.g. 'AddNewPad' or 'HybridShapeFactory.*Offset'",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum pages to return (default 20)",
                            "default": 20,
                            "minimum": 1,
                            "maximum": 200,
                        },
                    },
                    "required": ["query"],
                },
                "annotations": {"title": "Search the CATIA API reference", **_READ_ONLY},
            },
            {
                "name": "catia_api_doc",
                "description": (
                    "Read one page of the installed CATIA automation reference as plain "
                    "text (members, signatures, parameters). 'page' is a page path or "
                    "file name from catia_api_search, or an exact page title. Long "
                    "pages (e.g. HybridShapeFactory) come back in chunks: pass the "
                    "returned next_offset as 'offset' to read on."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "page": {
                            "type": "string",
                            "description": "Page path, file name or title from catia_api_search",
                        },
                        "offset": {
                            "type": "integer",
                            "description": "Character offset to start from (default 0)",
                            "default": 0,
                            "minimum": 0,
                        },
                    },
                    "required": ["page"],
                },
                "annotations": {"title": "Read a CATIA API reference page", **_READ_ONLY},
            },
        ]

    def execute(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        match tool_name:
            case "catia_status":
                return self._status()
            case "catia_get_selection":
                return self._selection(_int_arg(arguments, "max_items", 100, 1, 1000))
            case "catia_undo":
                return self._history(
                    "catia_undo", "Undo", _int_arg(arguments, "steps", 1, 1, _MAX_STEPS)
                )
            case "catia_redo":
                return self._history(
                    "catia_redo", "Redo", _int_arg(arguments, "steps", 1, 1, _MAX_STEPS)
                )
            case "catia_api_search":
                return self._api_search(
                    arguments["query"], _int_arg(arguments, "max_results", 20, 1, 200)
                )
            case "catia_api_doc":
                return self._api_doc(
                    arguments["page"], _int_arg(arguments, "offset", 0, 0, 10**9)
                )
            case _:
                raise ValueError(f"Unknown session tool: {tool_name}")

    def _attach(self) -> None:
        """ensure_connected without ever launching CATIA: a status or docs
        lookup must not start a CATIA session even when CATIA_MCP_LAUNCH=1.
        All tool calls run serialized on one worker, so the temporary flag
        cannot leak into a concurrent call."""
        launch = self.conn.launch
        self.conn.launch = False
        try:
            self.conn.ensure_connected()
        finally:
            self.conn.launch = launch

    # ── Status ──

    def _status(self) -> dict[str, Any]:
        try:
            self._attach()
        except Exception as e:  # noqa: BLE001 -- "not connected" is a status, not a failure
            return operation_result(
                tool="catia_status",
                ok=True,
                code="OK",
                message=f"Not connected to CATIA: {e}",
                data={"connected": False, "reason": str(e), "platform": None},
                modified=False,
            )
        conn = self.conn
        app = conn.app
        config = _try(lambda: app.SystemConfiguration)
        data: dict[str, Any] = {
            "connected": True,
            "platform": conn.platform,
            "platform_label": conn.platform_label,
            "caption": _text(_try(lambda: app.Caption)),
            "version": _try(lambda: config.Version) if config is not None else None,
            "release": _try(lambda: config.Release) if config is not None else None,
            "service_pack": _try(lambda: config.ServicePack) if config is not None else None,
            "windows_count": _try(lambda: app.Windows.Count),
            "active_window": _text(_try(lambda: app.ActiveWindow.Caption)),
        }
        if conn.is_3dx:
            obj = _try(conn.active_object)
            data["active"] = None if obj is None else {
                "title": _text(_try(lambda: app.ActiveWindow.Caption)),
                "name": _text(_try(lambda: obj.Name)),
                "kind": conn.classify(obj),
            }
            data["editors_count"] = _try(lambda: app.Editors.Count)
        else:
            doc = _try(lambda: app.ActiveDocument)
            data["active"] = None if doc is None else {
                "title": _text(_try(lambda: doc.Name)),
                "name": _text(_try(lambda: doc.Name)),
                "kind": self._v5_kind(doc),
                "path": _text(_try(lambda: doc.FullName)),
            }
            data["documents_count"] = _try(lambda: app.Documents.Count)
        part = _try(conn.get_active_part) if data["active"] else None
        data["in_work_object"] = _text(_try(lambda: part.InWorkObject.Name)) if part else None
        data["selection_count"] = _try(lambda: conn.hso.Count) if data["active"] else None
        if data["active"]:
            # What a write lands on, as the write guard sees it: pass "id" as
            # target_id to write tools; owner/state show whether it is protected.
            from catia_mcp import guard

            target = _try(lambda: guard.active_target(conn))
            if target:
                data["write_target"] = {**target, "user": guard.current_user()}
        active = data["active"]
        summary = (
            f"{active['kind']} '{active['name']}'" if active else "no active document"
        )
        return operation_result(
            tool="catia_status",
            ok=True,
            code="OK",
            message=f"Connected to {conn.platform_label}; {summary}.",
            data=data,
            modified=False,
        )

    @staticmethod
    def _v5_kind(doc: Any) -> str:
        name = _try(lambda: str(doc.Name)) or ""
        kind = _V5_KINDS.get(PurePath(name).suffix.lower())
        if kind:
            return kind
        # Unsaved or renamed documents: classify by member (name check only).
        for member, label in (("Part", "part"), ("Product", "product"), ("Sheets", "drawing")):
            if name_exists(doc, member):
                return label
        return "other"

    # ── Selection ──

    def _selection(self, max_items: int) -> dict[str, Any]:
        selection = self.conn.hso
        count = int(selection.Count)
        items: list[dict[str, Any]] = []
        for index in range(1, min(count, max_items) + 1):
            element = selection.Item(index)
            value = _try(lambda e=element: e.Value)
            reference = _try(lambda e=element: e.Reference)
            leaf = _try(lambda e=element: e.LeafProduct)
            items.append({
                "index": index,
                "name": _text(_try(lambda v=value: v.Name)),
                # Live COM type first; SelectedElement.Type is the V5 fallback.
                "type": (type_name(value) if value is not None else None)
                or _text(_try(lambda e=element: e.Type)),
                "reference": _text(_try(lambda r=reference: r.Name)),
                "reference_display_name": _text(_try(lambda r=reference: r.DisplayName)),
                "leaf_product": _text(_try(lambda p=leaf: p.Name)),
            })
        message = f"{count} item(s) selected"
        if count > max_items:
            message += f"; listed the first {max_items}"
        return operation_result(
            tool="catia_get_selection",
            ok=True,
            code="OK",
            message=message + ".",
            data={"count": count, "items": items, "truncated": count > max_items},
            modified=False,
        )

    # ── Undo / redo ──

    def _history(self, tool: str, command: str, steps: int) -> dict[str, Any]:
        self.conn.ensure_connected()
        app = self.conn.app
        if not name_exists(app, "StartCommand"):
            raise UnsupportedCapability(
                "This CATIA session exposes no Application.StartCommand, so undo/redo "
                "cannot be issued. Use Ctrl+Z / Ctrl+Y in CATIA."
            )
        # StartCommand takes the command's English UI name. Not field-proven on
        # 3DEXPERIENCE R428; localized UIs may need another name, and whether
        # back-to-back StartCommand calls each run (rather than coalescing)
        # needs live validation with steps > 1. No refresh_display here: it
        # Reframes, which would also change the user's camera.
        issued = 0
        for _ in range(steps):
            app.StartCommand(command)
            issued += 1
        return operation_result(
            tool=tool,
            ok=True,
            code="OK",
            message=(
                f"Issued CATIA '{command}' {issued} time(s). Not verified: check the "
                "model with catia_screenshot or a read tool."
            ),
            data={"command": command, "issued": issued, "verified": False},
            modified=None,
            warnings=[(
                "StartCommand uses English command names; a localized CATIA UI may "
                "ignore them silently."
            )],
        )

    # ── API docs ──

    def _app_full_name(self) -> str | None:
        if self.docs.index is not None:
            return None  # already indexed; no COM round trip needed
        try:
            self._attach()
        except Exception:  # noqa: BLE001 -- a cached index works without CATIA
            return None
        return _text(_try(lambda: self.conn.app.FullName))

    def _type_libraries(self) -> list[Any]:
        """Type libraries of this CATIA: the .tlb files beside the executable
        (every framework) plus those of the reachable live objects."""
        from catia_mcp.introspect import _type_libraries

        libs: list[Any] = []
        names: set[str] = set()

        def add(lib: Any) -> None:
            try:
                name = str(lib.GetDocumentation(-1)[0])
            except Exception:  # noqa: BLE001
                return
            if name not in names:
                names.add(name)
                libs.append(lib)

        full_name = _text(_try(lambda: self.conn.app.FullName))
        try:
            import pythoncom
        except ImportError:
            pythoncom = None  # not Windows: live objects only
        if full_name and pythoncom is not None:
            for tlb in sorted(Path(full_name).parent.glob("*.tlb")):
                try:
                    add(pythoncom.LoadTypeLib(str(tlb)))
                except Exception:  # noqa: BLE001 -- not every .tlb loads
                    continue
        objects: list[Any] = [self.conn.app]
        for getter in (
            lambda: self.conn.active_document,
            lambda: self.conn.get_active_part(),
            lambda: self.conn.get_active_part().ShapeFactory,
            lambda: self.conn.get_active_part().HybridShapeFactory,
        ):
            obj = _try(getter)
            if obj is not None:
                objects.append(obj)
        for lib in _type_libraries(objects):
            add(lib)
        return libs

    def _ensure_docs(self) -> tuple[Any, list[dict[str, str]]]:
        return self.docs.ensure(self._app_full_name(), self._type_libraries)

    def _api_search(self, query: str, max_results: int) -> dict[str, Any]:
        _, index = self._ensure_docs()
        results = apidocs.search(index, query, max_results)
        return operation_result(
            tool="catia_api_search",
            ok=True,
            code="OK",
            message=f"{len(results)} page(s) match '{query}'.",
            data={"results": results, **self.docs.describe()},
            modified=False,
        )

    def _api_doc(self, page: str, offset: int = 0) -> dict[str, Any]:
        self._ensure_docs()
        resolved, title, text, total, next_offset = self.docs.chunk(page, offset)
        message = f"API reference page '{title}'."
        if next_offset is not None:
            message += f" More text follows: call again with offset={next_offset}."
        return operation_result(
            tool="catia_api_doc",
            ok=True,
            code="OK",
            message=message,
            data={
                "page": resolved,
                "title": title,
                "text": text,
                "offset": offset,
                "total_chars": total,
                "next_offset": next_offset,
                **self.docs.describe(),
            },
            modified=False,
        )
