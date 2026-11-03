"""3DEXPERIENCE PLM session helpers: create, open, save, search, attributes.

Recipes (field-documented for 3DEXPERIENCE automation):

- Create:  CATIA.GetSessionService("PLMNewService").PLMCreate type, oEditor
  with type "3DShape", "3DPart", "Drawing" or "VPMReference" (Physical
  Product). "3DPart" and "VPMReference" need an extra licence/role on many
  sites (macro reports: "Operation not authorized"); "3DShape" and
  "Drawing" are always available. The new content opens in a new editor
  that becomes CATIA.ActiveEditor.
- Open:    CATIA.GetSessionService("Search").DatabaseSearch (BaseType,
  AddEasyCriteria "V_Name", title), Search, .Results (PLMEntities), then
  GetSessionService("PLMOpenService").PLMOpen entity, oEditor.
- Save:    GetSessionService("PLMPropagateService").PLMPropagate saves the
  content of the ACTIVE editor to the 3DSpace database.
- Rename:  rep or reference .SetAttributeValue "V_Name", title.

PLMCreate and PLMOpen return the editor through an out-parameter, so they
run through the SystemService.Evaluate detour (automation.call_out_object).

Field evidence (R428 SP6, smoke_report_before_fixes.json): a site without the
3D Part role answers PLMCreate "3DPart" with a CATScript error whose source is
"CATIAPLMNewService" and description "The method PLMCreate failed". That is
CATIA refusing the call, not the Evaluate detour failing, so retrying through
the direct (pywin32 out-parameter) call only cost time: catia_new_part took
~55 s before falling back to a 3D Shape. Refusals are now raised at once and
remembered per connection, so later 'auto' creations go straight to 3D Shape.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from catia_mcp.automation import (
    call_out_object,
    get_member,
    iter_items,
    name_exists,
    resolves,
    safe_get,
)
from catia_mcp.connection import CATIAConnection
from catia_mcp.errors import (
    AmbiguousTarget,
    InvalidArgument,
    ObjectNotFound,
    UnsupportedCapability,
)

# Attributes read for search results and attribute reports. Not every type
# carries every attribute; missing ones are skipped.
STANDARD_ATTRIBUTES = (
    "V_Name",
    "PLM_ExternalID",
    "revision",
    "V_description",
    "current",
    "owner",
    "project",
    "organization",
    "originated",
    "modified",
    "V_discipline",
    "V_usage",
)

CREATE_TYPES = {
    "3dpart": "3DPart",
    "3dshape": "3DShape",
    "drawing": "Drawing",
    "vpmreference": "VPMReference",
    "product": "VPMReference",
    "physical_product": "VPMReference",
}


# Environment variable naming the PLM type 'auto' part creation starts with.
# "3DShape" skips the 3D Part attempt entirely (sites without the role).
DEFAULT_PLM_TYPE_ENV = "CATIA_MCP_DEFAULT_PLM_TYPE"

# Attribute set on the CATIAConnection (connection.py is not owned here) that
# remembers PLMCreate refusals: (app object, {plm_type: refusal text}). The
# app object is kept so a reconnect (new Application proxy) starts afresh.
_REFUSALS_ATTR = "_plm_create_refusals"


def _not_authorized(error: Exception) -> bool:
    text = str(error).lower()
    return "not authorized" in text or "not allowed" in text or "licen" in text


def catia_refused(error: Exception) -> bool:
    """True when the failure text shows CATIA itself ran PLMCreate/PLMOpen
    and refused it (field text: "Source: CATIAPLMNewService / Description:
    The method PLMCreate failed"), as opposed to the Evaluate machinery
    failing before the call. Only the latter is worth a direct-call retry."""
    text = str(error)
    lowered = text.lower()
    return (
        "PLMCreate failed" in text
        or "CATIAPLMNewService" in text
        or "PLMOpen failed" in text
        or "CATIAPLMOpenService" in text
        or "method plmcreate failed" in lowered
        or _not_authorized(error)
        # The CATScript engine ran the macro and the call inside it raised
        # (live R428: "Execution error ... Description: Unspecified error"
        # for a 3D Part the licence refuses). Only a script that never ran
        # (compile error, no SystemService) is worth the direct-call retry.
        or ("execution error" in lowered and "syntax" not in lowered)
    )


# PLMCreate refusals outlive the session: each refused attempt shows a
# modal licence dialog the user must click away, so after one refusal on
# this machine 'auto' creation goes straight to the fallback type. Entries
# expire so a newly granted role is tried again; an explicit plm_type is
# always attempted.
REFUSAL_TTL_S = 30 * 24 * 3600


def _refusals_file() -> Any:
    from catia_mcp.paths import state_dir

    return state_dir() / "plm_refusals.json"


def load_persisted_refusals() -> dict[str, str]:
    import json
    import time

    try:
        data = json.loads(_refusals_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    now = time.time()
    return {
        kind: str(entry.get("reason", ""))
        for kind, entry in data.items()
        if isinstance(entry, dict) and now - float(entry.get("at", 0)) < REFUSAL_TTL_S
    }


def persist_refusal(plm_type: str, reason: str | None) -> None:
    """Record (reason) or clear (None) a refusal on disk; best effort."""
    import json
    import time

    try:
        path = _refusals_file()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if reason is None:
            if data.pop(plm_type, None) is None:
                return
        else:
            data[plm_type] = {"reason": reason, "at": time.time()}
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


def refusal_summary(error: Exception) -> str:
    """Short human-readable refusal: the CATScript Description line when the
    COM error carries one, else the error text (bounded)."""
    text = str(error).replace("\\n", "\n")
    for line in text.splitlines():
        if "Description:" in line:
            return line.split("Description:", 1)[1].strip().rstrip(")").strip()
    return text if len(text) <= 300 else text[:297] + "..."


def normalize_create_type(value: str | None) -> str | None:
    if not value:
        return None
    key = value.strip().lower().replace(" ", "").replace("-", "")
    return CREATE_TYPES.get(key)


def default_part_type() -> str | None:
    """CATIA_MCP_DEFAULT_PLM_TYPE as a PLM type ('3DPart' / '3DShape'), or None."""
    raw = os.environ.get(DEFAULT_PLM_TYPE_ENV)
    kind = normalize_create_type(raw)
    if raw and kind not in ("3DPart", "3DShape"):
        raise InvalidArgument(
            f"{DEFAULT_PLM_TYPE_ENV}={raw!r} is not a part type; use '3DPart' or '3DShape'."
        )
    return kind


class PLMCreateRefused(UnsupportedCapability):
    """CATIA ran PLMCreate and refused it (licence / role / site rules)."""

    def __init__(self, plm_type: str, error: Exception, *, modified: bool | None, cached: bool = False):
        self.plm_type = plm_type
        self.reason = refusal_summary(error)
        prefix = "earlier in this session " if cached else ""
        super().__init__(
            f"3DEXPERIENCE {prefix}refused to create a {plm_type} (PLMCreate: {self.reason}).",
            modified=modified,
        )


@contextmanager
def file_alerts_off(app: Any) -> Iterator[None]:
    """Set app.DisplayFileAlerts = False for the block and restore it after,
    so a file/save/overwrite prompt cannot block the COM call (and with it the
    server's worker). Skipped when the member does not exist."""
    previous = safe_get(app, "DisplayFileAlerts")
    changed = False
    if previous is not None:
        try:
            app.DisplayFileAlerts = False
            changed = True
        except Exception:
            changed = False
    try:
        yield
    finally:
        if changed:
            try:
                app.DisplayFileAlerts = previous
            except Exception:
                pass


class PLMSession:
    """3DEXPERIENCE PLM operations bound to one CATIA connection."""

    def __init__(self, connection: CATIAConnection) -> None:
        self.conn = connection

    # ── Editors ──

    def editors(self) -> list[Any]:
        app = self.conn.app
        try:
            return list(iter_items(app.Editors))
        except Exception:
            return []

    def editor_count(self) -> int | None:
        try:
            return int(self.conn.app.Editors.Count)
        except Exception:
            try:
                return int(self.conn.app.Windows.Count)
            except Exception:
                return None

    # ── Create / open / save ──

    # ── Refusal cache (per connection) ──

    def refusals(self) -> dict[str, str]:
        """PLMCreate refusals recorded for the current CATIA Application.
        Cleared automatically when the connection holds a new Application
        (reconnect) or explicitly by reset_refusals()."""
        cached = getattr(self.conn, _REFUSALS_ATTR, None)
        app = self.conn.app
        if not cached or cached[0] is not app:
            cached = (app, load_persisted_refusals())
            setattr(self.conn, _REFUSALS_ATTR, cached)
        return cached[1]

    def refused(self, plm_type: str) -> str | None:
        return self.refusals().get(plm_type)

    def remember_refusal(self, plm_type: str, reason: str) -> None:
        self.refusals()[plm_type] = reason
        persist_refusal(plm_type, reason)

    def forget_refusal(self, plm_type: str) -> None:
        if self.refusals().pop(plm_type, None) is not None:
            persist_refusal(plm_type, None)

    def reset_refusals(self) -> None:
        setattr(self.conn, _REFUSALS_ATTR, None)

    # ── Create / open / save ──

    def create(self, plm_type: str) -> Any:
        """PLMCreate one object and return its (now active) editor.

        Raises PLMCreateRefused at once when CATIA ran the call and refused
        it (recorded in the refusal cache); the direct-call retry is kept
        only for failures of the Evaluate detour itself."""
        service = self.conn.session_service("PLMNewService")
        before = self.editor_count()
        app = self.conn.app
        with file_alerts_off(app):
            try:
                editor = call_out_object(app, service, "PLMCreate", plm_type)
            except Exception as script_error:
                after = self.editor_count()
                if before is not None and after is not None and after > before:
                    # The object was created but the out-parameter could not
                    # be read back: the new editor is the active one.
                    self.forget_refusal(plm_type)
                    return self.conn.active_editor
                counted = before is not None and after is not None
                if catia_refused(script_error):
                    refusal = PLMCreateRefused(
                        plm_type, script_error, modified=False if counted else None
                    )
                    self.remember_refusal(plm_type, refusal.reason)
                    raise refusal from script_error
                try:
                    # pywin32 with type information returns out-parameters.
                    editor = service.PLMCreate(plm_type)
                except Exception:
                    raise script_error from None
        # Success (e.g. an explicit retry after a role change): forget any
        # earlier refusal so later 'auto' calls try this type again.
        self.forget_refusal(plm_type)
        if editor is None or not resolves(editor, "ActiveObject"):
            editor = self.conn.active_editor
        return editor

    def create_part(self, plm_type: str = "auto") -> tuple[str, str]:
        """Create a part-like object ('auto', '3DPart' or '3DShape') and
        return (created type, note explaining any fallback).

        'auto' starts with CATIA_MCP_DEFAULT_PLM_TYPE when set, skips a type
        this connection already saw refused, and falls back from 3D Part to
        3D Shape on a refusal. An explicit type is always attempted (the
        refusal may have been lifted) and never falls back."""
        if plm_type != "auto":
            self.create(plm_type)
            return plm_type, ""
        default = default_part_type()
        attempts = ["3DShape"] if default == "3DShape" else ["3DPart", "3DShape"]
        notes: list[str] = []
        if default == "3DShape":
            notes.append(
                f"{DEFAULT_PLM_TYPE_ENV}=3DShape, so no 3D Part was attempted; a 3D "
                "Shape has no product node of its own."
            )
        refusals: list[str] = []
        for kind in list(attempts):
            cached = self.refused(kind)
            if cached is not None and kind != attempts[-1]:
                notes.append(
                    f"A {kind} was refused earlier on this machine ({cached}), so a "
                    f"{attempts[-1]} was created directly (no licence dialog); it has no "
                    f"product node of its own. Pass plm_type='{kind}' to try again."
                )
                continue
            try:
                self.create(kind)
            except Exception as error:  # 'auto' falls back on any failure
                reason = error.reason if isinstance(error, PLMCreateRefused) else refusal_summary(error)
                refusals.append(f"{kind}: {reason}")
                if kind != attempts[-1]:
                    # Whatever the wording, a failed attempt cost the user a
                    # dialog; do not try it again on the next 'auto' create.
                    self.remember_refusal(kind, reason)
                if kind == attempts[-1]:
                    raise UnsupportedCapability(
                        "3DEXPERIENCE refused to create the part ("
                        + "; ".join(refusals)
                        + "). Check that you are logged in, have a collaborative space "
                        "selected, and hold a role allowing creation.",
                        modified=getattr(error, "modified", None),
                    ) from error
                later = f" Later 'auto' creations go straight to {attempts[-1]}."
                notes.append(
                    f"A {kind} could not be created ({reason}), so a {attempts[-1]} was "
                    f"created instead; it has no product node of its own.{later}"
                )
                continue
            return kind, " ".join(notes)
        raise AssertionError("unreachable")  # pragma: no cover

    def open_entity(self, entity: Any) -> Any:
        service = self.conn.session_service("PLMOpenService")
        before = self.editor_count()
        app = self.conn.app
        with file_alerts_off(app):
            try:
                editor = call_out_object(app, service, "PLMOpen", entity)
            except Exception as script_error:
                after = self.editor_count()
                if before is not None and after is not None and after > before:
                    return self.conn.active_editor
                if catia_refused(script_error):
                    # CATIA ran PLMOpen and refused it; a direct retry would
                    # only repeat the refusal (see the module docstring).
                    raise
                try:
                    editor = service.PLMOpen(entity)
                except Exception:
                    raise script_error from None
        if editor is None or not resolves(editor, "ActiveObject"):
            editor = self.conn.active_editor
        return editor

    def propagate(self) -> None:
        """Save the active editor's content to the 3DSpace database."""
        self.conn.session_service("PLMPropagateService").PLMPropagate()

    # ── Search ──

    def search(
        self,
        value: str,
        base_type: str = "VPMReference",
        attribute: str = "V_Name",
        extra_criteria: dict[str, str] | None = None,
    ) -> list[Any]:
        service = self.conn.session_service("Search")
        db_search = service.DatabaseSearch
        # The DatabaseSearch object may persist across searches in a session;
        # clear earlier criteria where the release exposes a way to.
        for clear in ("ClearCriteria", "RemoveAllCriteria", "ResetCriteria"):
            if name_exists(db_search, clear):
                try:
                    getattr(db_search, clear)()
                except Exception:
                    pass
                break
        db_search.BaseType = base_type
        db_search.AddEasyCriteria(attribute, value)
        for name, criterion in (extra_criteria or {}).items():
            db_search.AddEasyCriteria(name, criterion)
        service.Search()
        return list(iter_items(db_search.Results))

    def pick_one(self, entities: list[Any], description: str, index: int | None) -> Any:
        if not entities:
            raise ObjectNotFound(
                f"No {description} found in the 3DEXPERIENCE database. Check the "
                "title (V_Name), the type, and your collaborative space access; '*' "
                "works as a wildcard."
            )
        if index is not None:
            if index < 1 or index > len(entities):
                raise ObjectNotFound(
                    f"index {index} is out of range: the search returned {len(entities)} result(s)."
                )
            return entities[index - 1]
        if len(entities) > 1:
            rows = [self.describe(e, i) for i, e in enumerate(entities[:25], start=1)]
            raise AmbiguousTarget(
                f"{len(entities)} objects match {description}. Nothing was opened; "
                "pass 'index' (1-based) or a 'revision' to pick one: "
                + "; ".join(
                    ", ".join(f"{k}={v}" for k, v in row.items()) for row in rows
                )
            )
        return entities[0]

    # ── Attributes ──

    @staticmethod
    def attribute(obj: Any, name: str) -> Any:
        try:
            value = obj.GetAttributeValue(name)
        except Exception:
            return None
        if value is None or value == "":
            return None
        return value if isinstance(value, (int, float, bool)) else str(value)

    def attributes(self, obj: Any, names: tuple[str, ...] | list[str] = STANDARD_ATTRIBUTES) -> dict[str, Any]:
        result = {}
        for name in names:
            value = self.attribute(obj, name)
            if value is not None:
                result[name] = value
        return result

    def describe(self, entity: Any, index: int | None = None) -> dict[str, Any]:
        row: dict[str, Any] = {}
        if index is not None:
            row["index"] = index
        row.update(self.attributes(entity, ("V_Name", "PLM_ExternalID", "revision", "current", "owner", "modified")))
        if len(row) <= (1 if index is not None else 0):
            row["name"] = str(safe_get(entity, "Name", "<unnamed>"))
        return row

    def set_title(self, obj: Any, title: str) -> bool:
        try:
            obj.SetAttributeValue("V_Name", title)
            return True
        except Exception:
            return False

    # ── Product structure ──

    def reference_of(self, obj: Any) -> Any | None:
        """VPMReference behind an occurrence / instance / reference."""
        if resolves(obj, "ReferenceRootOccurrenceOf"):
            return get_member(obj, "ReferenceRootOccurrenceOf")
        if resolves(obj, "InstanceOccurrenceOf"):
            instance = get_member(obj, "InstanceOccurrenceOf")
            return safe_get(instance, "ReferenceInstanceOf")
        if resolves(obj, "ReferenceInstanceOf"):
            return get_member(obj, "ReferenceInstanceOf")
        if resolves(obj, "Instances") or resolves(obj, "RepInstances"):
            return obj
        return None

    def plm_object_of(self, obj: Any) -> Any | None:
        """The PLM object carrying the attributes of an editor object: the
        VPMReference for products and 3D Parts, the VPMRepReference (the
        Part's / DrawingRoot's Parent) for a 3D Shape or Drawing."""
        reference = self.reference_of(obj)
        if reference is not None:
            return reference
        parent = safe_get(obj, "Parent")
        if parent is not None and resolves(parent, "GetAttributeValue"):
            return parent
        if resolves(obj, "GetAttributeValue"):
            return obj
        return None

    def plm_object_of_active(self) -> Any:
        plm_object = self.plm_object_of(self.conn.active_object())
        if plm_object is None:
            raise UnsupportedCapability(
                "Could not reach the PLM object (VPMReference / VPMRepReference) behind "
                "the active editor's object."
            )
        return plm_object

    def title_of(self, obj: Any) -> str:
        """Display title of an editor object (V_Name, else its Name)."""
        plm_object = self.plm_object_of(obj)
        if plm_object is not None:
            title = self.attribute(plm_object, "V_Name") or self.attribute(plm_object, "PLM_ExternalID")
            if title:
                return str(title)
        return str(safe_get(obj, "Name", "<unnamed>"))

    def occurrence_name(self, occurrence: Any) -> str:
        instance = safe_get(occurrence, "InstanceOccurrenceOf")
        for obj in (instance, occurrence):
            if obj is None:
                continue
            for attr in ("PLM_ExternalID", "V_Name"):
                value = self.attribute(obj, attr)
                if value:
                    return str(value)
            name = safe_get(obj, "Name")
            if name:
                return str(name)
        return "<unnamed>"

    def child_occurrences(self, occurrence: Any) -> list[Any]:
        try:
            return list(iter_items(occurrence.Occurrences))
        except Exception:
            return []

    def find_occurrence(self, root: Any, name: str) -> Any:
        """Depth-first search by instance name (PLM_ExternalID), then title."""
        matches: list[Any] = []

        def visit(occurrence: Any) -> None:
            for child in self.child_occurrences(occurrence):
                instance = safe_get(child, "InstanceOccurrenceOf")
                reference = safe_get(instance, "ReferenceInstanceOf") if instance else None
                names = {self.occurrence_name(child)}
                if reference is not None:
                    title = self.attribute(reference, "V_Name")
                    if title:
                        names.add(str(title))
                if name in names:
                    matches.append(child)
                visit(child)

        visit(root)
        if not matches:
            children = [self.occurrence_name(c) for c in self.child_occurrences(root)]
            raise ObjectNotFound(
                f"No component named '{name}' in the product structure. Top-level "
                f"components: {', '.join(children) or 'none'}. Use catia_list_components."
            )
        if len(matches) > 1:
            raise AmbiguousTarget(
                f"{len(matches)} components are named '{name}'. Use the instance name "
                "(PLM_ExternalID) shown by catia_get_product_tree."
            )
        return matches[0]
