"""One-shot read-only probe of a 3DEXPERIENCE session's PLM data model.

Answers what the 2026-11-02 R428 field report left open: which PLM type the
site's 3D Parts and Physical Products really have (customized types?),
whether PLMEntity.GetCustomType works on this release, what PLMNewService
exposes, and where the Enterprise Item Number (V_PartNumber) lives.

Before running: start CATIA, log in, and open a 3D Part you made in the UI
(and, if you can, a Physical Product). Nothing is created or saved unless you
pass --try-create.

Usage (Windows, PowerShell):
    & "$env:APPDATA\\uv\\tools\\catia-v5-mcp-server\\Scripts\\python.exe" probe_3dx.py
    ... probe_3dx.py --out C:\\temp\\probe_3dx.json
    ... probe_3dx.py --try-create            (tries PLMCreate once per discovered type)
    ... probe_3dx.py --try-create --type XYZ_Part   (also tries a type you name)

--try-create WARNING: every refused PLMCreate may show a modal licence
dialog in CATIA that you must click away. A type that works creates a new,
unsaved object in the session (close it without saving); it is remembered so
catia_new_part / catia_new_product use it next time. Refused types are
remembered too, so the server never asks CATIA for them again.

Send the JSON file (default: Desktop\\probe_3dx.json) back to the developer.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import traceback
from collections.abc import Callable
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

PROBE_ATTRIBUTES = (
    "V_Name",
    "PLM_ExternalID",
    "V_PartNumber",
    "V_usage",
    "V_discipline",
    "revision",
    "current",
)
MEMBER_FILTER = re.compile(r"Attribute|Type|Custom|Part", re.IGNORECASE)
API_QUERIES = ("PLMCreate", "GetCustomType", "PartNumber", "Enterprise", "EIN", "PLMNewService")


def attempt(fn: Callable[[], Any]) -> dict[str, Any]:
    """{'value': ...} or {'error': full error text}; never raises."""
    try:
        return {"value": fn()}
    except Exception as error:  # noqa: BLE001 -- a probe records every failure
        return {"error": f"{type(error).__name__}: {error}"}


def call_custom_type(obj: Any) -> dict[str, Any]:
    from catia_mcp.automation import get_member

    return attempt(lambda: get_member(obj, "GetCustomType"))


def read_attributes(obj: Any) -> dict[str, Any]:
    return {name: attempt(lambda name=name: obj.GetAttributeValue(name)) for name in PROBE_ATTRIBUTES}


def filtered_members(obj: Any) -> dict[str, Any]:
    from catia_mcp.introspect import describe_members

    result = attempt(lambda: describe_members(obj))
    if "value" in result:
        result["value"] = [m for m in result["value"] if MEMBER_FILTER.search(m["name"])]
    return result


def describe_plm_object(obj: Any) -> dict[str, Any]:
    from catia_mcp.introspect import type_name

    return {
        "type_name": type_name(obj),
        "GetCustomType": call_custom_type(obj),
        "attributes": read_attributes(obj),
        "members": filtered_members(obj),
    }


def probe_editor(conn: Any, plm: Any, index: int, editor: Any) -> dict[str, Any]:
    from catia_mcp.automation import safe_get
    from catia_mcp.introspect import type_name

    row: dict[str, Any] = {"index": index}
    obj = safe_get(editor, "ActiveObject")
    row["active_object"] = {
        "classified": conn.classify(obj),
        "type_name": type_name(obj) if obj is not None else None,
        "title": attempt(lambda: plm.title_of(obj)) if obj is not None else None,
    }
    root = plm.root_occurrence_of(editor)
    row["has_root_occurrence"] = root is not None
    reference = plm.root_reference(editor) if root is not None else None
    if reference is not None:
        row["title"] = plm.label_of(reference)
        row["root_reference"] = describe_plm_object(reference)
        part = conn.part_from_reference(reference)
        row["kind"] = "3D Part" if part is not None else "Physical Product"
        if part is not None:
            rep = safe_get(part, "Parent")
            if rep is not None:
                row["shape_representation"] = describe_plm_object(rep)
        children = []
        for child in plm.child_occurrences(root)[:10]:
            child_ref = plm.reference_of(child)
            if child_ref is None:
                continue
            children.append(
                {
                    "title": plm.label_of(child_ref),
                    "is_3d_part": conn.part_from_reference(child_ref) is not None,
                    "type_name": type_name(child_ref),
                    "GetCustomType": call_custom_type(child_ref),
                    "attributes": read_attributes(child_ref),
                }
            )
        if children:
            row["components"] = children
    elif obj is not None and conn.classify(obj) == "part":
        row["kind"] = "3D Shape (standalone)"
        rep = safe_get(obj, "Parent")
        if rep is not None:
            row["title"] = plm.label_of(rep)
            row["shape_representation"] = describe_plm_object(rep)
    elif obj is not None and conn.classify(obj) == "drawing":
        row["kind"] = "Drawing"
    return row


def probe_new_service(conn: Any) -> dict[str, Any]:
    from catia_mcp.introspect import describe_members, type_name

    service = attempt(lambda: conn.session_service("PLMNewService"))
    if "error" in service:
        return service
    obj = service["value"]
    result: dict[str, Any] = {"type_name": type_name(obj)}
    members = attempt(lambda: describe_members(obj))
    if "value" in members:
        result["members"] = [
            {
                "name": m["name"],
                "kind": m["kind"],
                "params": [p["name"] + (" (out)" if p.get("out") else "") for p in m.get("params", [])],
            }
            for m in members["value"]
        ]
    else:
        result["members"] = members
    return result


def api_searches(conn: Any) -> dict[str, Any]:
    from catia_mcp.tools.session import SessionTools

    tools = SessionTools(conn)
    found: dict[str, Any] = {}
    for query in API_QUERIES:
        outcome = attempt(lambda query=query: tools.execute("catia_api_search", {"query": query, "max_results": 5}))
        if "value" in outcome and isinstance(outcome["value"], dict):
            data = outcome["value"].get("data", {})
            found[query] = {
                "hits": [
                    {"page": r.get("page"), "title": r.get("title"), "snippet": r.get("snippet")}
                    for r in data.get("results", [])[:5]
                ],
                "source": data.get("source"),
            }
        else:
            found[query] = outcome
    return found


def try_create(conn: Any, plm: Any, discovery: dict[str, Any], extra: list[str]) -> list[dict[str, Any]]:
    from catia_mcp.automation import call_out_object
    from catia_mcp.plm import (
        PART_KIND,
        PRODUCT_KIND,
        persist_working_type,
        refusal_summary,
        remember_not_part_type,
    )

    plan: list[tuple[str, str]] = [(e["type"], PART_KIND) for e in discovery[PART_KIND]]
    plan += [(e["type"], PRODUCT_KIND) for e in discovery[PRODUCT_KIND]]
    plan += [(t, "explicit") for t in extra]
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for plm_type, kind in plan:
        if plm_type.lower() in seen:
            continue
        seen.add(plm_type.lower())
        print(f"  PLMCreate('{plm_type}') ... (click away any licence dialog)")
        row: dict[str, Any] = {"type": plm_type, "discovered_as": kind}
        before = plm.editor_count()
        try:
            service = conn.session_service("PLMNewService")
            call_out_object(conn.app, service, "PLMCreate", plm_type)
        except Exception as error:  # noqa: BLE001 -- record the full refusal text
            row["ok"] = False
            row["error"] = f"{type(error).__name__}: {error}"
            row["editors_before_after"] = [before, plm.editor_count()]
            plm.remember_refusal(plm_type, refusal_summary(error))
            rows.append(row)
            continue
        row["ok"] = True
        row["editors_before_after"] = [before, plm.editor_count()]
        made_part = attempt(lambda: conn.get_active_part() is not None)
        row["new_editor_has_3d_shape"] = made_part
        plm.forget_refusal(plm_type)
        if made_part.get("value") is True:
            persist_working_type(PART_KIND, plm_type)
            row["remembered_as"] = PART_KIND
        else:
            # Same bookkeeping as PLMSession.create_part: never offered for parts again.
            remember_not_part_type(plm_type)
            row["remembered_as"] = "not a part type"
            if kind == PRODUCT_KIND:
                persist_working_type(PRODUCT_KIND, plm_type)
                row["remembered_as"] = "product (not a part type)"
        rows.append(row)
    return rows


def default_out() -> str:
    home = os.path.expanduser("~")
    for desktop in (os.path.join(home, "Desktop"), os.path.join(home, "OneDrive", "Desktop")):
        if os.path.isdir(desktop):
            return os.path.join(desktop, "probe_3dx.json")
    return os.path.abspath("probe_3dx.json")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", default=default_out(), help="JSON output path (default: Desktop\\probe_3dx.json)")
    parser.add_argument(
        "--try-create",
        action="store_true",
        help="Try PLMCreate once per discovered custom type (each refusal may show a licence dialog)",
    )
    parser.add_argument("--type", action="append", default=[], help="Extra PLM type to try with --try-create")
    args = parser.parse_args()

    from catia_mcp.connection import CATIAConnection
    from catia_mcp.plm import (
        PLMSession,
        load_not_part_types,
        load_persisted_refusals,
        load_working_types,
    )

    report: dict[str, Any] = {"probe": "probe_3dx", "version": 1}
    conn = CATIAConnection(launch=False)
    try:
        report["connect"] = conn.connect()
        app = conn.app
        report["catia"] = {
            "caption": attempt(lambda: str(app.Caption)),
            "full_name": attempt(lambda: str(app.FullName)),
            "platform": conn.platform_label,
            "system_configuration": {
                name: attempt(lambda name=name: getattr(app.SystemConfiguration, name))
                for name in ("Version", "Release", "ServicePack", "ProductCount")
            },
        }
        if not conn.is_3dx:
            print("This CATIA is V5, not 3DEXPERIENCE: nothing to probe.")
        plm = PLMSession(conn)
        editors = plm.editors()
        report["editors"] = []
        for i, editor in enumerate(editors, start=1):
            try:
                report["editors"].append(probe_editor(conn, plm, i, editor))
            except Exception:  # noqa: BLE001
                report["editors"].append({"index": i, "error": traceback.format_exc()})
        discovery = attempt(plm.discover_types)
        report["discovery"] = discovery
        report["plm_new_service"] = probe_new_service(conn)
        report["api_search"] = api_searches(conn)
        report["cached_state_before"] = {
            "refusals": load_persisted_refusals(),
            "working_types": load_working_types(),
            "not_part_types": load_not_part_types(),
        }
        if args.try_create:
            print("--try-create: trying PLMCreate with each discovered type.")
            found = discovery.get("value") or {"part": [], "product": []}
            report["try_create"] = try_create(conn, plm, found, args.type)
            report["cached_state_after"] = {
                "refusals": load_persisted_refusals(),
                "working_types": load_working_types(),
            }
    except Exception:  # noqa: BLE001 -- always write what was collected
        report["fatal"] = traceback.format_exc()

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False, default=str)

    print()
    if "fatal" in report:
        print("Probe stopped early:", report["fatal"].strip().splitlines()[-1])
    for row in report.get("editors", []):
        ref = row.get("root_reference") or row.get("shape_representation") or {}
        custom = ref.get("GetCustomType", {})
        print(
            f"  editor {row.get('index')}: {row.get('kind', '?')} '{row.get('title', '?')}' "
            f"type_name={ref.get('type_name')} GetCustomType="
            f"{custom.get('value', custom.get('error', '-'))}"
        )
    found = (report.get("discovery") or {}).get("value") or {}
    for kind in ("part", "product", "shape"):
        types = [e["type"] for e in found.get(kind, [])]
        print(f"  discovered {kind} types: {', '.join(types) or 'none'}")
    if not report.get("editors"):
        print("  No editor is open: open a 3D Part you made in the UI and run again.")
    for row in report.get("try_create", []):
        print(f"  PLMCreate('{row['type']}'): {'OK' if row['ok'] else 'refused: ' + row['error'][:120]}")
    print(f"\nJSON written to: {args.out}")
    return 1 if "fatal" in report else 0


if __name__ == "__main__":
    sys.exit(main())
