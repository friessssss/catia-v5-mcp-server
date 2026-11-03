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

Site types (2026-11-02 field report, R428 SP6): the same user who got
"PLMCreate failed" for 3DPart / VPMReference creates 3D Parts and Physical
Products in the UI. 'auto' creation therefore also tries the customized type
of content open in the session (PLMEntity.GetCustomType, UNVERIFIED on R428)
and remembers a type that worked in state_dir()/plm_types.json.
scripts/probe_3dx.py collects the live evidence.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from catia_mcp.automation import (
    call_out_object,
    com_name,
    get_member,
    iter_items,
    name_exists,
    resolves,
    safe_get,
)
from catia_mcp.connection import CATIAConnection
from catia_mcp.errors import (
    AmbiguousTarget,
    CATIAToolError,
    InvalidArgument,
    ObjectNotFound,
    UnsupportedCapability,
)

# Attributes read for search results and attribute reports. Not every type
# carries every attribute; missing ones are skipped. V_PartNumber holds the
# Enterprise Item Number (EIN) on the 3D Part's root VPMReference
# (field-proven on R428 SP6, 2026-11-02 field report: the drawing attribute
# link "VPMReference" / "V_PartNumber" showed the EIN).
STANDARD_ATTRIBUTES = (
    "V_Name",
    "PLM_ExternalID",
    "V_PartNumber",
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

# Site-specific PLM type names (any string, e.g. a customized 3D Part type of
# the company data model) tried by 'auto' creation before the standard types.
PART_TYPE_ENV = "CATIA_MCP_PART_TYPE"
PRODUCT_TYPE_ENV = "CATIA_MCP_PRODUCT_TYPE"

# Keys of plm_types.json (the type that last created a part / product on
# this machine).
PART_KIND = "part"
PRODUCT_KIND = "product"

# Kernel type names GetCustomType may answer for uncustomized content. They
# are not site types: a VPMReference creates a Physical Product, not a part.
_BASE_TYPES = frozenset(
    name.lower()
    for name in (
        "VPMReference", "VPMRepReference", "VPMInstance", "VPMRepInstance",
        "PLMEntity", "PLMCoreReference", "PLMCoreRepReference", "PLMReference",
        "3DPart", "3DShape", "Drawing",
    )
)
# Product structure children read per open product during type discovery.
_DISCOVERY_CHILDREN = 25

# Uncached PLMCreate attempts one 'auto' creation may make before it stops
# trying candidates (each refusal shows the user a modal licence dialog; the
# pre-discovery behaviour was at most one, for 3DPart). Candidates left over
# are tried by later 'auto' calls, since refused ones are cached on disk.
MAX_AUTO_ATTEMPTS = 2

# Accepted shape of a PLM type name passed by an agent or an environment
# variable (typed InvalidArgument instead of a CATIA licence dialog).
_TYPE_NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.:")

SHAPE_DRAWING_WARNING = (
    "Warning: a standalone 3D Shape cannot be put on a drawing or into an "
    "assembly; to draw it, the geometry must live in a 3D Part "
    "(catia_drawing_add_view refuses a 3D Shape): ask the user to open any 3D "
    "Part they made in the CATIA "
    "UI (or create one there) and retry catia_new_part, which then learns the "
    "site's 3D Part type from it, or rebuild the geometry inside that 3D Part."
)

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


def _types_file() -> Any:
    from catia_mcp.paths import state_dir

    return state_dir() / "plm_types.json"


def load_working_types() -> dict[str, str]:
    """{'part' | 'product': PLM type that last created one on this machine}."""
    import json

    try:
        data = json.loads(_types_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        kind: str(entry["type"])
        for kind, entry in data.items()
        if isinstance(entry, dict) and entry.get("type")
    }


def persist_working_type(kind: str, plm_type: str | None) -> None:
    """Remember (plm_type) or forget (None) the working type of a kind; best effort."""
    import json
    import time

    try:
        path = _types_file()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        if plm_type is None:
            if data.pop(kind, None) is None:
                return
        else:
            data[kind] = {"type": plm_type, "at": time.time()}
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


_NOT_PART_KEY = "not_part_types"


def load_not_part_types() -> list[str]:
    """Types whose PLMCreate made something without a 3D Shape on this machine."""
    import json

    try:
        data = json.loads(_types_file().read_text(encoding="utf-8"))
        return [str(t) for t in data.get(_NOT_PART_KEY, {}).get("types", [])]
    except (OSError, ValueError, AttributeError):
        return []


def _store_not_part_types(types: list[str]) -> None:
    import json

    try:
        path = _types_file()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        if types:
            data[_NOT_PART_KEY] = {"types": types}
        elif data.pop(_NOT_PART_KEY, None) is None:
            return
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


def remember_not_part_type(plm_type: str) -> None:
    types = load_not_part_types()
    if plm_type not in types:
        _store_not_part_types([*types, plm_type])


def forget_not_part_type(plm_type: str) -> None:
    types = load_not_part_types()
    if plm_type in types:
        _store_not_part_types([t for t in types if t != plm_type])


def _add_type(entries: list[dict[str, str]], plm_type: str | None, source: str) -> None:
    """Append a discovered site type (not a kernel name, not a duplicate)."""
    if not plm_type or is_base_type(plm_type):
        return
    if plm_type.lower() in {e["type"].lower() for e in entries}:
        return
    entries.append({"type": plm_type, "source": source})


OPEN_UI_PART_HINT = (
    "If the user can create 3D Parts in the CATIA UI, ask them to open any 3D Part "
    "they made there (or create one), then retry catia_new_part: the tool learns "
    "the site's part type from it. Or set CATIA_MCP_PART_TYPE to the type name."
)
OPEN_UI_PRODUCT_HINT = (
    "If the user can create Physical Products in the CATIA UI, ask them to open any "
    "Physical Product they made there, then retry catia_new_product: the tool learns "
    "the site's product type from it. Or open an existing product with "
    "catia_open_from_database."
)


def custom_type_of(obj: Any) -> str | None:
    """PLM type of a reference / representation via PLMEntity.GetCustomType.

    GetCustomType is listed on PLMEntity in the 3DEXPERIENCE automation
    reference but is NOT verified on R428 SP6 (scripts/probe_3dx.py records
    what it answers). Any failure reads as "unknown". Attribute values such
    as V_usage or the PLM_ExternalID prefix are deliberately NOT used: they
    are not type names PLMCreate accepts."""
    if obj is None:
        return None
    try:
        value = get_member(obj, "GetCustomType")
    except Exception:
        return None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def is_base_type(plm_type: str | None) -> bool:
    return bool(plm_type) and str(plm_type).strip().lower() in _BASE_TYPES


def check_type_name(value: str, source: str) -> str:
    """A PLM type name as given (trimmed), or InvalidArgument before CATIA is asked."""
    text = (value or "").strip()
    if not text or len(text) > 100 or any(c not in _TYPE_NAME_CHARS for c in text):
        raise InvalidArgument(
            f"{source}={value!r} is not a PLM type name (letters, digits, '_', '-', "
            "'.', ':'; e.g. '3DPart' or your site's customized type)."
        )
    return text


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

    # ── Type discovery (site-customized PLM types) ──
    #
    # Field context (2026-11-02 field report, R428 SP6): PLMCreate("3DPart")
    # and ("VPMReference") failed with "PLMCreate failed" / "Unspecified
    # error" although the same user creates 3D Parts and Physical Products in
    # the UI. Leading hypothesis (UNVERIFIED): the company data model uses
    # customized types (specializations of VPMReference / 3D Shape) and the UI
    # creates those while scripts asked for the base type. Discovery reads the
    # type of content the user already made in the UI (GetCustomType, itself
    # unverified on R428) so 'auto' creation can ask for that type instead.

    def root_occurrence_of(self, editor: Any) -> Any | None:
        """Root occurrence of an editor, or None (standalone 3D Shape, drawing).

        Field-proven on R428 SP6 (2026-11-02 field report):
        Editor.GetService("PLMProductService").RootOccurrence."""
        try:
            root = editor.GetService("PLMProductService").RootOccurrence
        except Exception:
            root = None
        if root is None:
            obj = safe_get(editor, "ActiveObject")
            if obj is not None and resolves(obj, "ReferenceRootOccurrenceOf"):
                root = obj
        return root

    def root_reference(self, editor: Any | None = None) -> Any | None:
        """Root VPMReference (the 3D Part / Physical Product itself) of an
        editor, or None. Field-proven on R428 SP6 (2026-11-02 field report):
        ActiveEditor.GetService("PLMProductService").RootOccurrence
        .ReferenceRootOccurrenceOf."""
        if editor is None:
            editor = self.conn.active_editor
        root = self.root_occurrence_of(editor)
        if root is None:
            return None
        try:
            reference = get_member(root, "ReferenceRootOccurrenceOf")
        except Exception:
            return None
        if reference is None or not resolves(reference, "GetAttributeValue"):
            return None
        return reference

    def label_of(self, obj: Any) -> str:
        """V_Name, else PLM_ExternalID, else Name of a PLM object."""
        for name in ("V_Name", "PLM_ExternalID"):
            value = self.attribute(obj, name)
            if value:
                return str(value)
        return str(safe_get(obj, "Name", "<unnamed>"))

    def _note_reference(
        self, reference: Any, via: str, found: dict[str, list[dict[str, str]]], sources: list[dict[str, Any]]
    ) -> None:
        title = self.label_of(reference)
        part = self.conn.part_from_reference(reference)
        custom = custom_type_of(reference)
        row: dict[str, Any] = {
            "title": title,
            "via": via,
            "kind": "3D Part" if part is not None else "Physical Product",
            "custom_type": custom,
        }
        if part is not None:
            rep_type = custom_type_of(safe_get(part, "Parent"))
            row["shape_custom_type"] = rep_type
            _add_type(found["shape"], rep_type, title)
        _add_type(found[PART_KIND if part is not None else PRODUCT_KIND], custom, title)
        sources.append(row)

    def discover_types(self) -> dict[str, Any]:
        """Customized PLM types of the content open in this session.

        Every open editor is visited: a root reference with a 3D Shape
        representation is a 3D Part, one without is a Physical Product (its
        first-level components are visited too); a standalone 3D Shape gives
        a shape type. Kernel names (VPMReference, 3DPart, ...) are not site
        types and are left out of the candidate lists. Read-only."""
        found: dict[str, list[dict[str, str]]] = {PART_KIND: [], PRODUCT_KIND: [], "shape": []}
        sources: list[dict[str, Any]] = []
        for editor in self.editors():
            obj = safe_get(editor, "ActiveObject")
            if obj is None or resolves(obj, "Sheets"):
                continue
            root = self.root_occurrence_of(editor)
            reference = self.root_reference(editor) if root is not None else None
            if reference is None:
                if resolves(obj, "ShapeFactory"):
                    rep = safe_get(obj, "Parent")
                    title = self.label_of(rep) if rep is not None else com_name(obj)
                    rep_type = custom_type_of(rep)
                    sources.append(
                        {"title": title, "via": "open editor", "kind": "3D Shape", "shape_custom_type": rep_type}
                    )
                    _add_type(found["shape"], rep_type, title)
                continue
            self._note_reference(reference, "open editor", found, sources)
            parent = self.label_of(reference)
            for child in self.child_occurrences(root)[:_DISCOVERY_CHILDREN]:
                child_reference = self.reference_of(child)
                if child_reference is not None:
                    self._note_reference(child_reference, f"component of '{parent}'", found, sources)
        return {**found, "sources": sources}

    def _candidates(
        self, kind: str, standard: str, env_name: str, discovered: list[dict[str, str]], late: list[dict[str, str]]
    ) -> list[tuple[str, str]]:
        """(type, origin) in the order 'auto' creation tries them:
        remembered working type -> env type -> discovered types -> standard
        type -> `late` (ambiguous discovered types)."""
        out: list[tuple[str, str]] = []

        def add(plm_type: str | None, origin: str) -> None:
            if plm_type and plm_type.lower() not in {t.lower() for t, _ in out}:
                out.append((plm_type, origin))

        add(load_working_types().get(kind), f"it created a {kind} on this machine before")
        raw = os.environ.get(env_name)
        if raw and raw.strip():
            add(check_type_name(raw, env_name), f"set by {env_name}")
        for entry in discovered:
            add(entry["type"], f"type of '{entry['source']}' open in this session")
        add(standard, "standard type")
        for entry in late:
            add(entry["type"], f"type of '{entry['source']}' open in this session")
        return out

    def _part_type_failed(self, plm_type: str) -> None:
        if load_working_types().get(PART_KIND, "").lower() == plm_type.lower():
            persist_working_type(PART_KIND, None)

    def _after_part_created(self, plm_type: str, origin: str) -> str:
        """Verify a non-3DShape creation produced a part, remember the type."""
        if plm_type == "3DShape":
            return SHAPE_DRAWING_WARNING
        try:
            self.conn.get_active_part()
        except CATIAToolError as error:
            # CATIA did not refuse, so this is not a licence refusal: mark the
            # type as "not a part type" (a product type may share it).
            remember_not_part_type(plm_type)
            self._part_type_failed(plm_type)
            raise UnsupportedCapability(
                f"PLMCreate('{plm_type}') ({origin}) ran, but the new editor shows no 3D "
                f"Shape ({error}). The new object is open, unsaved, in the session: close "
                f"it with catia_close_document. '{plm_type}' will not be used for parts "
                "again; pass plm_type explicitly to override.",
                modified=True,
            ) from error
        persist_working_type(PART_KIND, plm_type)
        if plm_type == "3DPart":
            return ""
        return (
            f"Created with the site's PLM type '{plm_type}' ({origin}); remembered on "
            "this machine for later catia_new_part calls."
        )

    def create_part(self, plm_type: str = "auto", *, shape_fallback: bool = True) -> tuple[str, str]:
        """Create a part and return (created type, note).

        plm_type: 'auto', '3DPart', '3DShape' or a site type name. An explicit
        type is always attempted (a refusal may have been lifted) and never
        falls back. 'auto' tries, skipping every type refused on this machine
        (each refused attempt costs the user a modal licence dialog):
        remembered working type -> CATIA_MCP_PART_TYPE -> customized types of
        3D Parts open in the session -> '3DPart' -> '3DShape' (only with
        shape_fallback; never skipped). CATIA_MCP_DEFAULT_PLM_TYPE=3DShape
        goes straight to 3DShape."""
        if plm_type != "auto":
            kind = normalize_create_type(plm_type) or check_type_name(plm_type, "plm_type")
            if kind in ("Drawing", "VPMReference"):
                raise InvalidArgument(
                    f"plm_type '{plm_type}' does not create a part; use catia_new_drawing or "
                    "catia_new_product."
                )
            try:
                self.create(kind)
            except PLMCreateRefused as refusal:
                raise UnsupportedCapability(
                    f"{refusal} {OPEN_UI_PART_HINT}", modified=refusal.modified
                ) from refusal
            if kind != "3DShape":
                forget_not_part_type(kind)
            return kind, self._after_part_created(kind, "requested explicitly")
        if default_part_type() == "3DShape":
            candidates: list[tuple[str, str]] = []
            notes = [f"{DEFAULT_PLM_TYPE_ENV}=3DShape, so no 3D Part was attempted."]
        else:
            discovery = self.discover_types()
            not_part = {t.lower() for t in load_not_part_types()}
            product_types = {e["type"].lower() for e in discovery[PRODUCT_KIND]}
            usable = [e for e in discovery[PART_KIND] if e["type"].lower() not in not_part]
            # A type also seen on a Physical Product is probably a VPMReference
            # specialization that creates a product, not a part: try it last.
            early = [e for e in usable if e["type"].lower() not in product_types]
            late = [e for e in usable if e["type"].lower() in product_types]
            candidates = self._candidates(PART_KIND, "3DPart", PART_TYPE_ENV, early, late)
            notes = []
        final = "3DShape" if shape_fallback else None
        skipped: list[tuple[str, str]] = []
        failures: list[str] = []
        deferred: list[str] = []
        modified: bool | None = False  # whether a failed attempt may have created something
        for kind, origin in candidates:
            if final is not None and kind.lower() == final.lower():
                continue
            cached = self.refused(kind)
            if cached is not None:
                skipped.append((kind, cached))
                continue
            if len(failures) >= MAX_AUTO_ATTEMPTS:
                deferred.append(kind)
                continue
            try:
                self.create(kind)
            except Exception as error:  # 'auto' moves on after any failure
                reason = error.reason if isinstance(error, PLMCreateRefused) else refusal_summary(error)
                failures.append(f"{kind} ({origin}): {reason}")
                # Whatever the wording, a failed attempt cost the user a
                # dialog; do not try it again on the next 'auto' create.
                self.remember_refusal(kind, reason)
                self._part_type_failed(kind)
                if getattr(error, "modified", None) is not False:
                    modified = None
                continue
            note = self._after_part_created(kind, origin)
            if failures:
                notes.append(
                    "Refused first (not tried again by 'auto'): " + "; ".join(failures) + "."
                )
            return kind, " ".join(n for n in [*notes, note] if n)
        deferred_note = (
            f" Not tried in this call (at most {MAX_AUTO_ATTEMPTS} licence dialogs per "
            "call): " + ", ".join(deferred) + "; a later 'auto' call tries them."
            if deferred
            else ""
        )
        if final is None:
            if failures:
                raise UnsupportedCapability(
                    "3DEXPERIENCE refused to create a 3D Part (" + "; ".join(failures) + ")."
                    + deferred_note + " " + OPEN_UI_PART_HINT,
                    modified=modified,
                )
            raise UnsupportedCapability(
                "3DEXPERIENCE refused to create a 3D Part earlier on this machine ("
                + ("; ".join(f"{k}: {r}" for k, r in skipped) or "no part type available")
                + "); not retried, to avoid another licence dialog. "
                + OPEN_UI_PART_HINT,
                modified=False,
            )
        try:
            self.create(final)
        except Exception as error:
            reason = error.reason if isinstance(error, PLMCreateRefused) else refusal_summary(error)
            raise UnsupportedCapability(
                "3DEXPERIENCE refused to create the part ("
                + "; ".join([*failures, *(f"{k}: {r}" for k, r in skipped), f"{final}: {reason}"])
                + "). Check that you are logged in, have a collaborative space "
                "selected, and hold a role allowing creation.",
                modified=getattr(error, "modified", None),
            ) from error
        for kind, cached in skipped:
            notes.append(
                f"A {kind} was refused earlier on this machine ({cached}), "
                f"so it was not tried again (no licence dialog). Pass plm_type='{kind}' to "
                "try again."
            )
        if failures:
            notes.append(
                "Could not create " + "; ".join(failures) + f", so a {final} was created "
                "instead. Later 'auto' creations skip the refused types."
            )
        if deferred_note:
            notes.append(deferred_note.strip())
        notes.append(SHAPE_DRAWING_WARNING)
        return final, " ".join(notes)

    def create_product(self, plm_type: str = "auto") -> tuple[str, str]:
        """Create a Physical Product and return (created type, note).

        'auto' tries, skipping types refused on this machine: remembered
        working product type -> CATIA_MCP_PRODUCT_TYPE -> customized types of
        Physical Products open in the session -> 'VPMReference'. Discovery
        and custom types are UNVERIFIED on R428 (see discover_types)."""
        if plm_type != "auto":
            kind = normalize_create_type(plm_type) or check_type_name(plm_type, "plm_type")
            if kind in ("3DPart", "3DShape", "Drawing"):
                raise InvalidArgument(
                    f"plm_type '{plm_type}' does not create a Physical Product; use "
                    "catia_new_part or catia_new_drawing."
                )
            try:
                self.create(kind)
            except PLMCreateRefused as refusal:
                raise UnsupportedCapability(
                    f"{refusal} {OPEN_UI_PRODUCT_HINT}", modified=refusal.modified
                ) from refusal
            persist_working_type(PRODUCT_KIND, kind)
            return kind, ""
        discovery = self.discover_types()
        candidates = self._candidates(
            PRODUCT_KIND, "VPMReference", PRODUCT_TYPE_ENV, discovery[PRODUCT_KIND], []
        )
        skipped: list[str] = []
        failures: list[str] = []
        deferred: list[str] = []
        modified: bool | None = False
        for kind, origin in candidates:
            cached = self.refused(kind)
            if cached is not None:
                skipped.append(f"{kind}: {cached}")
                continue
            if len(failures) >= MAX_AUTO_ATTEMPTS:
                deferred.append(kind)  # bounded licence dialogs per call
                continue
            try:
                self.create(kind)
            except Exception as error:
                reason = error.reason if isinstance(error, PLMCreateRefused) else refusal_summary(error)
                failures.append(f"{kind} ({origin}): {reason}")
                self.remember_refusal(kind, reason)
                if load_working_types().get(PRODUCT_KIND, "").lower() == kind.lower():
                    persist_working_type(PRODUCT_KIND, None)
                if getattr(error, "modified", None) is not False:
                    modified = None
                continue
            persist_working_type(PRODUCT_KIND, kind)
            notes = []
            if kind != "VPMReference":
                notes.append(
                    f"Created with the site's PLM type '{kind}' ({origin}); remembered on "
                    "this machine for later catia_new_product calls."
                )
            if failures:
                notes.append("Refused first (not tried again by 'auto'): " + "; ".join(failures) + ".")
            return kind, " ".join(notes)
        if failures:
            later = (
                f" Not tried in this call (at most {MAX_AUTO_ATTEMPTS} licence dialogs per "
                "call): " + ", ".join(deferred) + "; a later call tries them."
                if deferred
                else ""
            )
            raise UnsupportedCapability(
                "3DEXPERIENCE refused to create a Physical Product (" + "; ".join(failures)
                + ")." + later + " " + OPEN_UI_PRODUCT_HINT,
                modified=modified,
            )
        raise UnsupportedCapability(
            "3DEXPERIENCE refused to create a Physical Product earlier on this machine ("
            + "; ".join(skipped)
            + "); not retried, to avoid another licence dialog. "
            + OPEN_UI_PRODUCT_HINT,
            modified=False,
        )

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
