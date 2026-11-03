"""PLM tools: 3DEXPERIENCE database search/open, PLM attributes, product tree.

Database search and open, and PLM attributes, exist only on 3DEXPERIENCE.
The product tree works on both platforms (V5 Products recursion,
3DEXPERIENCE occurrence recursion).
"""

from __future__ import annotations

import json
import time
from typing import Any

from catia_mcp.automation import com_name, iter_items, name_exists, safe_get
from catia_mcp.connection import PLATFORM_3DX, CATIAConnection
from catia_mcp.errors import InvalidArgument, UnsupportedCapability
from catia_mcp.plm import STANDARD_ATTRIBUTES, PLMSession, custom_type_of
from catia_mcp.results import operation_result

# The Enterprise Item Number (EIN) lives in V_PartNumber on the 3D Part's root
# VPMReference (field-proven on R428 SP6, 2026-11-02 field report: the title
# block link to "VPMReference" / "V_PartNumber" showed the EIN the user
# assigned in the UI).
PART_NUMBER_ATTRIBUTE = "V_PartNumber"

# UNVERIFIED: English UI names of the 3DEXPERIENCE command that assigns an
# Enterprise Item Number, tried with Application.StartCommand. No automation
# API for EIN assignment is known; the 2026-11-02 field report says the user
# had to assign it in the UI. scripts/probe_3dx.py searches the API docs for
# it; pass `command` to catia_assign_part_number with the name the UI shows.
EIN_COMMAND_CANDIDATES = (
    "Assign Enterprise Item Number",
    "Enterprise Item Number",
    "Assign EIN",
)
# How long catia_assign_part_number watches V_PartNumber after StartCommand.
EIN_POLL_S = 3.0
EIN_POLL_STEP_S = 0.5
_sleep = time.sleep

_TARGETS = ("auto", "part", "shape")
_TARGET_HELP = (
    "Which PLM object: 'auto' (default: the root 3D Part / Physical Product "
    "reference when the active editor has one, else the 3D Shape / drawing), "
    "'part' (the root reference: title, V_PartNumber / EIN, revision), or "
    "'shape' (the 3D Shape representation)"
)

_TYPE_HELP = (
    "Database type: 'VPMReference' (Physical Product / 3D Part, default), "
    "'3DShape', 'Drawing', or another type name of your data model"
)


class PLMTools:
    """3DEXPERIENCE PLM database tools plus a cross-platform product tree."""

    def __init__(self, connection: CATIAConnection) -> None:
        self.conn = connection
        self.plm = PLMSession(connection)

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "catia_search_database",
                "description": (
                    "3DEXPERIENCE only: search the 3DSpace database by title (V_Name) "
                    "or another attribute; '*' works as a wildcard. Returns title, "
                    "name (PLM_ExternalID), revision, maturity, owner. Read-only."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Value to match, e.g. 'Bracket*'"},
                        "type": {"type": "string", "description": _TYPE_HELP},
                        "attribute": {
                            "type": "string",
                            "description": "Attribute to match (default 'V_Name' = title; 'PLM_ExternalID' = name)",
                        },
                        "max_results": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 200,
                            "description": "Maximum rows returned (default 25)",
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "catia_open_from_database",
                "description": (
                    "3DEXPERIENCE only: open an object from the 3DSpace database in a new "
                    "editor (search + PLMOpen). Refuses to guess when several objects "
                    "match: pass 'revision' or the 1-based 'index' from "
                    "catia_search_database."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Title (V_Name) or value of 'attribute'"},
                        "type": {"type": "string", "description": _TYPE_HELP},
                        "attribute": {
                            "type": "string",
                            "description": "Attribute to match (default 'V_Name')",
                        },
                        "revision": {"type": "string", "description": "Optional revision to pick, e.g. 'A.1'"},
                        "index": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "1-based result index when several objects match",
                        },
                    },
                    "required": ["title"],
                },
            },
            {
                "name": "catia_get_plm_attributes",
                "description": (
                    "3DEXPERIENCE only: read PLM attributes (title V_Name, name "
                    "PLM_ExternalID, part number / EIN V_PartNumber, revision, maturity "
                    "'current', description, owner, ...) of the active editor's content. "
                    "In a 3D Part editor 'auto' reads the 3D Part itself (root reference), "
                    "not its inner 3D Shape. Reports which object was read."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "names": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Attribute names to read (default: the standard set)",
                        },
                        "target": {"type": "string", "enum": list(_TARGETS), "description": _TARGET_HELP},
                    },
                },
                "annotations": {
                    "title": "Read PLM attributes",
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
            },
            {
                "name": "catia_set_plm_attributes",
                "description": (
                    "3DEXPERIENCE only: set PLM attributes (e.g. V_Name title, "
                    "V_description, custom attributes) of the active editor's content "
                    "('target' as in catia_get_plm_attributes). Values are read back. "
                    "Changes stay in session until catia_save_document; the data model "
                    "and your role decide which attributes are writable. V_PartNumber "
                    "(the Enterprise Item Number) is usually assigned by the site's "
                    "numbering service: if CATIA refuses it or the read-back is "
                    "unchanged, this fails with UNSUPPORTED_CAPABILITY."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "attributes": {
                            "type": "object",
                            "additionalProperties": {"type": ["string", "number", "boolean"]},
                            "description": "Attribute name -> value, e.g. {\"V_description\": \"Rev B bracket\"}",
                        },
                        "target": {"type": "string", "enum": list(_TARGETS), "description": _TARGET_HELP},
                    },
                    "required": ["attributes"],
                },
            },
            {
                "name": "catia_assign_part_number",
                "description": (
                    "3DEXPERIENCE only: give the active 3D Part (or Physical Product) a "
                    "part number / Enterprise Item Number (V_PartNumber on its root "
                    "reference). With part_number: writes it and verifies by read-back "
                    "(UNSUPPORTED_CAPABILITY when the site forbids writing it). Without: "
                    "UNVERIFIED, starts the UI command that assigns an EIN "
                    "(Application.StartCommand) and reports whether a number appeared; "
                    "if CATIA opens a dialog, the user must complete it, then read the "
                    "number with catia_get_plm_attributes target='part'. An existing "
                    "number is never replaced unless part_number is given."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "part_number": {
                            "type": "string",
                            "description": "Number to write; omit to run the EIN assignment command",
                        },
                        "command": {
                            "type": "string",
                            "description": (
                                "Exact English UI command name to start instead of the "
                                "built-in guesses (" + ", ".join(EIN_COMMAND_CANDIDATES) + ")"
                            ),
                        },
                    },
                },
                "annotations": {
                    "title": "Assign part number / EIN",
                    "readOnlyHint": False,
                    "destructiveHint": False,
                    "idempotentHint": False,
                    "openWorldHint": True,
                },
            },
            {
                "name": "catia_get_product_tree",
                "description": (
                    "Return the product structure of the active product as a tree: V5 "
                    "components (name, part number), 3DEXPERIENCE occurrences (instance "
                    "name, reference title, revision, leaf or sub-assembly)."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "max_depth": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 50,
                            "description": "Maximum depth (default 10)",
                        },
                    },
                },
            },
        ]

    def execute(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        match tool_name:
            case "catia_search_database":
                return self._search(arguments)
            case "catia_open_from_database":
                return self._open(arguments)
            case "catia_get_plm_attributes":
                return self._get_attributes(arguments.get("names"), arguments.get("target") or "auto")
            case "catia_set_plm_attributes":
                return self._set_attributes(arguments["attributes"], arguments.get("target") or "auto")
            case "catia_assign_part_number":
                return self._assign_part_number(arguments.get("part_number"), arguments.get("command"))
            case "catia_get_product_tree":
                return self._product_tree(int(arguments.get("max_depth", 10)))
            case _:
                raise ValueError(f"Unknown PLM tool: {tool_name}")

    def _search(self, args: dict[str, Any]) -> str:
        self.conn.ensure_connected()
        self.conn.require_platform(PLATFORM_3DX, "catia_search_database")
        base_type = args.get("type") or "VPMReference"
        attribute = args.get("attribute") or "V_Name"
        limit = int(args.get("max_results", 25))
        entities = self.plm.search(args["query"], base_type, attribute)
        rows = [self.plm.describe(e, i) for i, e in enumerate(entities[:limit], start=1)]
        return json.dumps(
            {
                "query": args["query"],
                "type": base_type,
                "attribute": attribute,
                "total": len(entities),
                "returned": len(rows),
                "results": rows,
            },
            indent=2,
            ensure_ascii=False,
        )

    def _open(self, args: dict[str, Any]) -> str:
        self.conn.ensure_connected()
        self.conn.require_platform(PLATFORM_3DX, "catia_open_from_database")
        base_type = args.get("type") or "VPMReference"
        attribute = args.get("attribute") or "V_Name"
        extra = {"revision": args["revision"]} if args.get("revision") else None
        entities = self.plm.search(args["title"], base_type, attribute, extra)
        description = f"{base_type} with {attribute}='{args['title']}'" + (
            f" revision {args['revision']}" if args.get("revision") else ""
        )
        entity = self.plm.pick_one(entities, description, args.get("index"))
        summary = self.plm.describe(entity)
        self.plm.open_entity(entity)
        obj = self.conn.active_object()
        return (
            f"Opened {self.conn.classify(obj)} '{self.plm.title_of(obj)}' "
            f"({', '.join(f'{k}={v}' for k, v in summary.items())}) in a new editor."
        )

    # ── Attribute target ──

    def _target(self, target: str) -> tuple[Any, dict[str, Any]]:
        """(PLM object, description) for target 'auto' | 'part' | 'shape'."""
        if target not in _TARGETS:
            raise InvalidArgument(f"target must be one of {', '.join(_TARGETS)}; got {target!r}.")
        active = self.conn.active_object()
        is_drawing = self.conn.classify(active) == "drawing"
        if target in ("auto", "part") and not is_drawing:
            # Field-proven on R428 SP6 (2026-11-02 field report):
            # ActiveEditor.GetService("PLMProductService").RootOccurrence
            # .ReferenceRootOccurrenceOf is the 3D Part itself.
            reference = self.plm.root_reference()
            if reference is not None:
                level = "3D Part" if self.conn.part_from_reference(reference) is not None else "Physical Product"
                return reference, self._describe_target(reference, f"{level} (root reference)")
        if target == "part":
            raise UnsupportedCapability(
                "The active editor has no root 3D Part / Physical Product reference (it "
                "shows a standalone 3D Shape or a drawing), so there is no part number to "
                "read or write. Activate the 3D Part's editor with catia_activate_document."
            )
        if target == "shape":
            part = self.conn.get_active_part()
            rep = safe_get(part, "Parent")
            if rep is None or not name_exists(rep, "GetAttributeValue"):
                raise UnsupportedCapability("Could not reach the 3D Shape representation of the active part.")
            return rep, self._describe_target(rep, "3D Shape (representation)")
        plm_object = self.plm.plm_object_of_active()
        level = "Drawing (representation)" if is_drawing else "3D Shape (representation)"
        if self.plm.reference_of(active) is not None:
            level = "Physical Product (reference)"
        return plm_object, self._describe_target(plm_object, level)

    def _describe_target(self, obj: Any, level: str) -> dict[str, Any]:
        info: dict[str, Any] = {"level": level, "title": self.plm.label_of(obj)}
        custom = custom_type_of(obj)
        if custom:
            info["type"] = custom
        else:
            from catia_mcp.introspect import type_name

            interface = type_name(obj)
            if interface:
                info["type"] = interface
        return info

    def _get_attributes(self, names: list[str] | None, target: str = "auto") -> str:
        self.conn.ensure_connected()
        self.conn.require_platform(PLATFORM_3DX, "catia_get_plm_attributes")
        plm_object, read_from = self._target(target)
        wanted = list(names) if names else list(STANDARD_ATTRIBUTES)
        values = self.plm.attributes(plm_object, wanted)
        missing = [n for n in wanted if n not in values]
        result: dict[str, Any] = {"read_from": read_from, "attributes": values}
        if missing:
            result["unavailable"] = missing
        if PART_NUMBER_ATTRIBUTE in missing and read_from["level"].startswith("3D Part"):
            result["note"] = (
                "V_PartNumber is empty: no Enterprise Item Number is assigned yet "
                "(catia_assign_part_number, or 'Assign Enterprise Item Number' in the UI)."
            )
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    def _set_attributes(self, attributes: dict[str, Any], target: str = "auto") -> str:
        self.conn.ensure_connected()
        self.conn.require_platform(PLATFORM_3DX, "catia_set_plm_attributes")
        if not attributes:
            raise InvalidArgument("attributes must name at least one attribute.")
        plm_object, written_to = self._target(target)
        applied, failed, differs = {}, {}, {}
        part_number_error: str | None = None
        for name, value in attributes.items():
            before = self.plm.attribute(plm_object, name)
            try:
                plm_object.SetAttributeValue(name, value)
            except Exception as e:
                failed[name] = str(e)
                if name == PART_NUMBER_ATTRIBUTE:
                    part_number_error = f"CATIA refused the write ({e})"
                continue
            after = self.plm.attribute(plm_object, name)
            applied[name] = after
            if _text(after) != _text(value):
                differs[name] = {"requested": value, "read_back": after}
                if name == PART_NUMBER_ATTRIBUTE and _text(after) == _text(before):
                    part_number_error = f"the read-back value is unchanged ({before!r})"
        if part_number_error is not None:
            applied.pop(PART_NUMBER_ATTRIBUTE, None)
            # Every name left in `applied` was accepted by SetAttributeValue,
            # including those whose read-back differs: the model may have changed.
            others = list(applied)
            raise UnsupportedCapability(
                f"V_PartNumber could not be set on {written_to['title']}: {part_number_error}. "
                "Enterprise Item Numbers are usually assigned by the site's numbering "
                "service: ask the user to run 'Assign Enterprise Item Number' on the 3D "
                "Part in the CATIA UI, then read it with catia_get_plm_attributes "
                "target='part'."
                + (f" Other attributes were written: {', '.join(others)}." if others else ""),
                modified=bool(others),
            )
        if not applied:
            raise RuntimeError(
                "No attribute could be set: "
                + "; ".join(f"{k}: {v}" for k, v in failed.items())
                + ". The attribute may be read-only for your role, or not part of this type."
            )
        result: dict[str, Any] = {"written_to": written_to, "applied": applied, "saved": False}
        if failed:
            result["failed"] = failed
        if differs:
            result["read_back_differs"] = differs
        result["note"] = "Changes are in session only until catia_save_document."
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    # ── Part number / EIN ──

    def _assign_part_number(self, part_number: str | None, command: str | None) -> dict[str, Any]:
        self.conn.ensure_connected()
        self.conn.require_platform(PLATFORM_3DX, "catia_assign_part_number")
        reference, target = self._target("part")
        current = self.plm.attribute(reference, PART_NUMBER_ATTRIBUTE)
        if part_number is not None:
            if not str(part_number).strip():
                raise InvalidArgument("part_number must not be empty; omit it to run the EIN command.")
            self._set_attributes({PART_NUMBER_ATTRIBUTE: str(part_number).strip()}, "part")
            value = self.plm.attribute(reference, PART_NUMBER_ATTRIBUTE)
            changed = _text(value) != _text(current)
            return operation_result(
                tool="catia_assign_part_number",
                ok=True,
                code="OK",
                message=(
                    f"V_PartNumber of {target['title']} is now '{value}' (read back; was "
                    f"{current!r}). In session only until catia_save_document."
                ),
                data={"part": target, "part_number": value, "previous": current, "method": "SetAttributeValue"},
                modified=changed,
            )
        if current:
            return operation_result(
                tool="catia_assign_part_number",
                ok=True,
                code="OK",
                message=f"{target['title']} already has V_PartNumber '{current}'; nothing was changed.",
                data={"part": target, "part_number": current, "method": None},
                modified=False,
            )
        app = self.conn.app
        if not name_exists(app, "StartCommand"):
            raise UnsupportedCapability(
                "This CATIA session exposes no Application.StartCommand, and no automation "
                "API assigns an Enterprise Item Number. Ask the user to run 'Assign "
                "Enterprise Item Number' on the 3D Part in the CATIA UI."
            )
        # UNVERIFIED on R428: whether any of these English command names exists,
        # and whether StartCommand raises for an unknown one (V5 ignores unknown
        # names silently). The first name StartCommand accepts is the only one
        # issued, so a dialog it opens is never stacked with another command.
        names = [command.strip()] if command and command.strip() else list(EIN_COMMAND_CANDIDATES)
        issued: str | None = None
        errors: dict[str, str] = {}
        for name in names:
            try:
                app.StartCommand(name)
            except Exception as e:
                errors[name] = str(e)
                continue
            issued = name
            break
        if issued is None:
            raise UnsupportedCapability(
                "No EIN assignment command could be started ("
                + "; ".join(f"'{k}': {v}" for k, v in errors.items())
                + "). Ask the user to run 'Assign Enterprise Item Number' on the 3D Part in "
                "the CATIA UI, or pass the exact command name as 'command'.",
            )
        value = None
        waited = 0.0
        while True:
            value = self.plm.attribute(reference, PART_NUMBER_ATTRIBUTE)
            if value or waited >= EIN_POLL_S:
                break
            _sleep(EIN_POLL_STEP_S)
            waited += EIN_POLL_STEP_S
        data = {"part": target, "command": issued, "tried": names, "part_number": value, "verified": bool(value)}
        if errors:
            data["command_errors"] = errors
        if value:
            return operation_result(
                tool="catia_assign_part_number",
                ok=True,
                code="OK",
                message=(
                    f"Started '{issued}'; {target['title']} now has V_PartNumber '{value}' "
                    "(read back). In session only until catia_save_document."
                ),
                data=data,
                modified=True,
            )
        return operation_result(
            tool="catia_assign_part_number",
            ok=True,
            code="PENDING_USER_ACTION",
            message=(
                f"Started CATIA command '{issued}' (UNVERIFIED command name), but no "
                f"V_PartNumber appeared within {EIN_POLL_S:g} s. If a dialog opened in "
                "CATIA, ask the user to complete it, then read the number with "
                "catia_get_plm_attributes target='part'. If nothing opened, the command "
                "name is wrong for this release: ask the user to run 'Assign Enterprise "
                "Item Number' in the UI (or pass its exact name as 'command')."
            ),
            data=data,
            modified=None,
            warnings=["StartCommand uses English command names; a localized UI may ignore them silently."],
        )

    def _product_tree(self, max_depth: int) -> str:
        self.conn.ensure_connected()
        if self.conn.is_3dx:
            root = self.conn.root_occurrence()
            reference = self.plm.reference_of(root)
            tree = {
                "title": self.plm.attribute(reference, "V_Name") if reference is not None else None,
                "children": self._occurrence_children(root, 1, max_depth),
            }
            return json.dumps(tree, indent=2, ensure_ascii=False, default=str)
        product = self.conn.get_active_product()
        tree = {
            "name": com_name(product),
            "part_number": str(safe_get(product, "PartNumber", "")),
            "children": self._product_children(product, 1, max_depth),
        }
        return json.dumps(tree, indent=2, ensure_ascii=False)

    def _occurrence_children(self, occurrence: Any, depth: int, max_depth: int) -> list[dict[str, Any]]:
        rows = []
        for child in self.plm.child_occurrences(occurrence):
            instance = safe_get(child, "InstanceOccurrenceOf")
            reference = safe_get(instance, "ReferenceInstanceOf") if instance is not None else None
            row: dict[str, Any] = {"instance": self.plm.occurrence_name(child)}
            if reference is not None:
                row.update(self.plm.attributes(reference, ("V_Name", "PLM_ExternalID", "revision", "current")))
            grandchildren = self.plm.child_occurrences(child)
            row["kind"] = "sub-assembly" if grandchildren else "part"
            if grandchildren and depth < max_depth:
                row["children"] = self._occurrence_children(child, depth + 1, max_depth)
            elif grandchildren:
                row["truncated"] = True
            rows.append(row)
        return rows

    def _product_children(self, product: Any, depth: int, max_depth: int) -> list[dict[str, Any]]:
        rows = []
        try:
            children = list(iter_items(product.Products))
        except Exception:
            return rows
        for child in children:
            row: dict[str, Any] = {
                "name": com_name(child),
                "part_number": str(safe_get(child, "PartNumber", "")),
            }
            try:
                count = int(child.Products.Count)
            except Exception:
                count = 0
            if count and depth < max_depth:
                row["children"] = self._product_children(child, depth + 1, max_depth)
            elif count:
                row["truncated"] = True
            rows.append(row)
        return rows


def _text(value: Any) -> str:
    """Comparable text of an attribute value (None reads as empty)."""
    return "" if value is None else str(value).strip()
