"""Write guard: protect other people's and released content.

Field report 2026-11-02 (R428 SP6): in a shared 3DSpace the server writes to
whatever editor is in front, `catia_activate_document` matches partial
captions, and saving publishes to the shared collaborative space; safety
"depended on Claude checking which window was active before each delete,
not on the MCP". Before any modifying tool the server now reads the active
object's PLM owner and maturity and refuses when:

- the object belongs to another user (owner != this user), or
- its maturity is Frozen, Released or Obsolete,

unless the call passes `allow_protected_write: true`. A write tool may also
pass `target_id` (PLM_ExternalID or exact title, from catia_status or
catia_list_documents): the call is refused when the active editor shows
something else.

Content created in this session and not yet saved has no owner or maturity
yet; it counts as the user's own. When the attributes cannot be read at all
(V5 files, an unexpected object), only the target_id check applies.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from catia_mcp.automation import resolves, safe_get
from catia_mcp.errors import CATIAToolError

logger = logging.getLogger("catia_mcp.guard")

GUARD_ARGS = ("target_id", "allow_protected_write")

# Tools that write but never into existing content: they create or open
# content, or only change the view. Every other non-read-only tool is guarded.
EXEMPT = frozenset(
    {
        "catia_connect",
        "catia_disconnect",
        "catia_new_part",
        "catia_new_product",
        "catia_new_drawing",
        "catia_open_document",
        "catia_open_from_database",
        "catia_activate_document",
        "catia_close_document",  # guarded by the server when save=true
        "catia_export",  # writes a file, never the model or the database
        "catia_set_view",
        "catia_fit_all",
        "catia_screenshot",
    }
)

PROTECTED_STATES = {"frozen", "released", "obsolete"}

GUARD_SCHEMA = {
    "target_id": {
        "type": "string",
        "description": (
            "Optional safety check: PLM_ExternalID or exact title of the document "
            "this call must modify (from catia_status / catia_list_documents). The "
            "call is refused if the active CATIA window shows something else."
        ),
    },
    "allow_protected_write": {
        "type": "boolean",
        "description": (
            "Override the write guard for content owned by another user or in a "
            "Frozen/Released/Obsolete state. Only with the user's explicit consent."
        ),
    },
}


class WriteProtected(CATIAToolError):
    code = "WRITE_PROTECTED"


class TargetMismatch(CATIAToolError):
    code = "TARGET_MISMATCH"


def _login_file() -> Any:
    from catia_mcp.paths import state_dir

    return state_dir() / "plm_login.txt"


def learn_login(owner: str | None) -> None:
    """Remember the 3DEXPERIENCE login from the owner of content this server
    just created: it can differ from the Windows user name."""
    if not owner:
        return
    try:
        _login_file().write_text(owner.strip(), encoding="utf-8")
    except OSError:
        pass


def current_user() -> str:
    """The 3DEXPERIENCE login to compare owners against: CATIA_MCP_USER, else
    the login learned from content this server created, else the Windows
    user name (they match on the field laptop)."""
    explicit = os.environ.get("CATIA_MCP_USER")
    if explicit:
        return explicit.strip()
    try:
        learned = _login_file().read_text(encoding="utf-8").strip()
    except OSError:
        learned = ""
    return (learned or os.environ.get("USERNAME") or os.environ.get("USER") or "").strip()


def same_user(owner: str, user: str) -> bool:
    """Owner vs login, tolerant of DOMAIN\\user, user@domain and case."""

    def core(name: str) -> str:
        name = name.strip().lower().rsplit("\\", 1)[-1].split("@", 1)[0]
        return re.sub(r"[^a-z0-9]", "", name)

    return core(owner) == core(user)


def _attr(entity: Any, name: str) -> str | None:
    try:
        value = entity.GetAttributeValue(name)
    except Exception:  # noqa: BLE001 -- absent on unsaved / non-PLM objects
        return None
    text = str(value).strip() if value is not None else ""
    return text or None


def _plm_candidates(conn: Any) -> tuple[Any, list[Any]]:
    """(active editor object, PLM entities that may carry its metadata, the
    product root reference first)."""
    editor = conn.active_editor
    obj = safe_get(editor, "ActiveObject")
    candidates: list[Any] = []
    for service in ("PLMProductService", "PLMProductContext"):
        try:
            # Field-proven root path (report 2026-11-02): the 3D Part / product
            # reference that carries the owner, maturity and EIN.
            ref = editor.GetService(service).RootOccurrence.ReferenceRootOccurrenceOf
        except Exception:  # noqa: BLE001 -- 3D Shape and drawing editors have none
            ref = None
        if ref is not None:
            candidates.append(ref)
            break
    if obj is not None:
        if resolves(obj, "ReferenceRootOccurrenceOf"):
            ref = safe_get(obj, "ReferenceRootOccurrenceOf")
            if ref is not None:
                candidates.append(ref)
        candidates.append(obj)
        parent = safe_get(obj, "Parent")
        if parent is not None:
            candidates.append(parent)
    return obj, candidates


def active_target(conn: Any, entity: Any = None) -> dict[str, Any]:
    """What a write would land on: title, PLM id, owner and maturity. A tool
    that writes into another editor than the active one (a drawing found by
    title) passes that object's PLM `entity`."""
    if entity is not None:
        info = {
            "id": _attr(entity, "PLM_ExternalID"),
            "title": _attr(entity, "V_Name") or str(safe_get(entity, "Name", "") or "") or None,
            "owner": _attr(entity, "owner"),
            "state": _attr(entity, "current"),
        }
        return info
    if not conn.is_3dx:
        doc = safe_get(conn.app, "ActiveDocument")
        return {
            "title": str(safe_get(doc, "Name", "") or "") or None,
            "id": str(safe_get(doc, "FullName", "") or "") or None,
            "owner": None,
            "state": None,
        }
    obj, candidates = _plm_candidates(conn)
    info: dict[str, Any] = {"title": None, "id": None, "owner": None, "state": None}
    for candidate in candidates:
        values = {
            "id": _attr(candidate, "PLM_ExternalID"),
            "title": _attr(candidate, "V_Name"),
            "owner": _attr(candidate, "owner"),
            "state": _attr(candidate, "current"),
        }
        for key, value in values.items():
            if info[key] is None and value is not None:
                info[key] = value
        if info["id"] and info["owner"] and info["state"]:
            break
    if info["title"] is None:
        info["title"] = str(safe_get(obj, "Name", "") or "") or None
    return info


def _norm(value: str | None) -> str:
    return re.sub(r"[\s_-]", "", (value or "").lower())


def check(
    conn: Any,
    tool: str,
    target_id: str | None,
    override: bool,
    target: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Raise when `tool` would modify a protected or unexpected object;
    return the target description otherwise (logged by the caller). Pass
    an already read `target` to avoid reading the attributes twice."""
    target = target if target is not None else active_target(conn)
    shown = target["title"] or target["id"] or "the active window"
    if target_id:
        wanted = target_id.strip().lower()
        if wanted not in {(target["id"] or "").lower(), (target["title"] or "").lower()}:
            raise TargetMismatch(
                f"{tool} was meant for '{target_id}', but the active CATIA window shows "
                f"'{shown}' (id {target['id'] or 'unknown'}). Nothing was changed. "
                "Activate the right window with catia_activate_document, then retry."
            )
    if override:
        return target
    owner = target["owner"]
    user = current_user()
    if owner and user and not same_user(owner, user):
        raise WriteProtected(
            f"'{shown}' is owned by {owner}, not {user}; {tool} would modify someone "
            "else's content in the shared database, so nothing was changed. Make a copy "
            "or ask the owner. Pass allow_protected_write=true only if the user "
            "explicitly asked to change it (set CATIA_MCP_USER if your 3DEXPERIENCE "
            "login differs from your Windows user name)."
        )
    if _norm(target["state"]) in PROTECTED_STATES:
        raise WriteProtected(
            f"'{shown}' is {target['state']}; {tool} would modify released or frozen "
            "content, so nothing was changed. Create a new revision first, or pass "
            "allow_protected_write=true only if the user explicitly asked for it."
        )
    return target


# catia_run_python audit: calls that delete, publish or move content.
_RISKY_CALL = re.compile(
    r"\.\s*(Delete|Remove|Save|SaveAs|PLMPropagate|Propagate|Paste|Close|Cut)\s*\(",
    re.IGNORECASE,
)


def risky_calls(code: str) -> list[str]:
    """Distinct risky member calls a script makes (static scan, for the log
    and the result's warnings)."""
    seen: list[str] = []
    for match in _RISKY_CALL.finditer(code):
        name = match.group(1)
        if name not in seen:
            seen.append(name)
    return seen
