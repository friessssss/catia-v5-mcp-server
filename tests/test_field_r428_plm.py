"""PLM findings of the 2026-11-02 R428 SP6 field report.

Covered: 'auto' part / product creation learns the site's customized PLM type
from content the user made in the UI (GetCustomType of open 3D Parts /
Physical Products) and remembers a working type per machine; the 3D Shape
fallback warns that it cannot be drawn; PLM attribute tools target the root
3D Part (V_PartNumber / EIN) instead of the inner 3D Shape; V_PartNumber
writes are verified by read-back; catia_assign_part_number.

The tool classes are driven directly (typed errors surface as exceptions).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import catia_mcp.tools.plm as plm_tools_module
from catia_mcp.connection import PLATFORM_3DX, CATIAConnection
from catia_mcp.errors import InvalidArgument, UnsupportedCapability
from catia_mcp.plm import (
    PART_KIND,
    PRODUCT_KIND,
    SHAPE_DRAWING_WARNING,
    PLMSession,
    load_not_part_types,
    load_persisted_refusals,
    load_working_types,
    persist_refusal,
)
from catia_mcp.tools.assembly import AssemblyTools
from catia_mcp.tools.document import DocumentTools
from catia_mcp.tools.plm import PLMTools
from tests.fakes import App3DX, Coll, NewService, PLMObject, make_part

REFUSAL = "The method PLMCreate failed"


class TypedPLMObject(PLMObject):
    """PLM object answering PLMEntity.GetCustomType (unverified on R428)."""

    def __init__(self, custom_type: str | None = None, **attributes):
        super().__init__(**attributes)
        self.custom_type = custom_type

    def GetCustomType(self):
        if self.custom_type is None:
            raise AttributeError("GetCustomType")
        return self.custom_type


class RepReference(TypedPLMObject):
    def __init__(self, part, custom_type=None, **attributes):
        super().__init__(custom_type, **attributes)
        self.part = part

    def GetItem(self, name):
        assert name == "Part"
        return self.part


def open_3d_part(app, title, custom_type=None, shape_type=None, part_number=None):
    """A 3D Part as the UI makes it; the editor shows the inner Part (shape
    level), like the field report's 3D Part editor."""
    attrs = {"V_Name": title, "PLM_ExternalID": f"prd-{title}", "V_usage": "3DPart"}
    if part_number:
        attrs["V_PartNumber"] = part_number
    reference = TypedPLMObject(custom_type, **attrs)
    part = make_part(title)
    rep = RepReference(part, shape_type, V_Name=title, PLM_ExternalID=f"rep-{title}")
    part.Parent = rep
    reference.RepInstances = Coll([SimpleNamespace(ReferenceInstanceOf=rep)])
    root = SimpleNamespace(ReferenceRootOccurrenceOf=reference, Occurrences=Coll())
    editor = app.open_editor(part, f"{title} window", {"PLMProductService": SimpleNamespace(RootOccurrence=root)})
    return editor, reference, part


def open_product(app, title, custom_type=None):
    reference = TypedPLMObject(custom_type, V_Name=title, PLM_ExternalID=f"prd-{title}")
    reference.RepInstances = Coll()
    root = SimpleNamespace(ReferenceRootOccurrenceOf=reference, Occurrences=Coll())
    app.open_editor(root, f"{title} window", {"PLMProductService": SimpleNamespace(RootOccurrence=root)})
    return reference, root


class SiteNewService(NewService):
    """PLMNewService of a site with customized types: the base types are
    refused, the site types create 3D Parts / Physical Products."""

    def __init__(self, app, refuse=("3DPart", "VPMReference"), part_types=("LS_Part",), product_types=("LS_Product",)):
        super().__init__(app)
        self.refuse_types = refuse
        self.part_types = part_types
        self.product_types = product_types
        self.attempts: list[str] = []

    def _out_PLMCreate(self, plm_type):
        self.attempts.append(plm_type)
        if plm_type in self.refuse_types:
            raise RuntimeError(f"Source: CATIAPLMNewService\nDescription: {REFUSAL}")
        if plm_type in self.part_types:
            return open_3d_part(self.app, "Part1", plm_type)[0]
        if plm_type in self.product_types:
            open_product(self.app, "Product1", plm_type)
            return self.app.ActiveEditor
        return super()._out_PLMCreate(plm_type)

    def PLMCreate(self, plm_type):
        raise AssertionError("direct PLMCreate retry after a CATIA refusal")


def connected(app) -> CATIAConnection:
    conn = CATIAConnection(platform=PLATFORM_3DX)
    conn.app = app
    conn.platform = PLATFORM_3DX
    return conn


def site(**kwargs):
    app = App3DX()
    service = SiteNewService(app, **kwargs)
    app.session_services["PLMNewService"] = service
    return app, service


# ── 1. 3D Part creation learns the site's part type ──


def test_discovery_reads_custom_types_of_open_ui_content_and_skips_kernel_names():
    app, _ = site()
    open_3d_part(app, "test part A", "LS_Part", shape_type="LS_Shape")
    open_3d_part(app, "plain part", "VPMReference")  # kernel name: not a site type
    open_product(app, "Rig", "LS_Product")
    found = PLMSession(connected(app)).discover_types()
    assert [e["type"] for e in found[PART_KIND]] == ["LS_Part"]
    assert found[PART_KIND][0]["source"] == "test part A"
    assert [e["type"] for e in found[PRODUCT_KIND]] == ["LS_Product"]
    assert [e["type"] for e in found["shape"]] == ["LS_Shape"]
    assert len(found["sources"]) == 3


def test_new_part_uses_the_discovered_type_when_3d_part_was_refused_before():
    persist_refusal("3DPart", REFUSAL)
    app, service = site()
    open_3d_part(app, "test part A", "LS_Part")
    text = DocumentTools(connected(app)).execute("catia_new_part", {"name": "Block"})
    assert service.attempts == ["LS_Part"]  # no dialog for the cached 3DPart
    assert "Created new LS_Part 'Block'" in text
    assert "test part A" in text and "remembered" in text
    assert "cannot be put on a drawing" not in text
    assert load_working_types() == {PART_KIND: "LS_Part"}


def test_discovered_type_is_tried_before_3d_part_and_the_refusal_moves_on():
    app, service = site(refuse=("3DPart", "VPMReference", "LS_Part"))
    open_3d_part(app, "test part A", "LS_Part")
    text = DocumentTools(connected(app)).execute("catia_new_part", {})
    assert service.attempts == ["LS_Part", "3DPart", "3DShape"]
    assert "Created new 3DShape" in text and "LS_Part" in text
    assert set(load_persisted_refusals()) == {"LS_Part", "3DPart"}
    # Next 'auto' call: both refused types are skipped, no dialog at all.
    service.attempts.clear()
    again = DocumentTools(connected(app)).execute("catia_new_part", {})
    assert service.attempts == ["3DShape"] and "earlier on this machine" in again


def test_working_type_is_persisted_and_used_first_next_time():
    persist_refusal("3DPart", REFUSAL)
    app, _service = site()
    open_3d_part(app, "test part A", "LS_Part")
    DocumentTools(connected(app)).execute("catia_new_part", {})
    # A fresh session with nothing open: the remembered type is used at once.
    app2, service2 = site(refuse=("3DPart", "VPMReference"))
    text = DocumentTools(connected(app2)).execute("catia_new_part", {"name": "Next"})
    assert service2.attempts == ["LS_Part"]
    assert "Created new LS_Part 'Next'" in text and "created a part on this machine before" in text


def test_part_type_env_accepts_any_type_name(monkeypatch):
    monkeypatch.setenv("CATIA_MCP_PART_TYPE", "LS_Part")
    app, service = site()
    DocumentTools(connected(app)).execute("catia_new_part", {})
    assert service.attempts == ["LS_Part"]


def test_invalid_type_names_are_refused_before_asking_catia(monkeypatch):
    app, service = site()
    with pytest.raises(InvalidArgument):
        DocumentTools(connected(app)).execute("catia_new_part", {"plm_type": "bad type; DROP"})
    with pytest.raises(InvalidArgument):
        DocumentTools(connected(app)).execute("catia_new_part", {"plm_type": "Drawing"})
    monkeypatch.setenv("CATIA_MCP_PART_TYPE", "no spaces allowed")
    with pytest.raises(InvalidArgument):
        DocumentTools(connected(app)).execute("catia_new_part", {})
    assert service.attempts == []


def test_explicit_custom_plm_type_is_accepted_and_schema_allows_it():
    app, service = site()
    tools = DocumentTools(connected(app))
    schema = next(t for t in tools.get_tool_definitions() if t["name"] == "catia_new_part")
    plm_type = schema["inputSchema"]["properties"]["plm_type"]
    assert "enum" not in plm_type and plm_type["type"] == "string"
    assert "open any 3D Part they made in the UI" in schema["description"]
    text = tools.execute("catia_new_part", {"plm_type": "LS_Part"})
    assert service.attempts == ["LS_Part"] and "Created new LS_Part" in text
    assert load_working_types()[PART_KIND] == "LS_Part"


def test_type_that_creates_no_3d_shape_is_typed_and_never_used_for_parts_again():
    persist_refusal("3DPart", REFUSAL)
    app, service = site(part_types=(), product_types=("LS_Ref",))
    open_3d_part(app, "test part A", "LS_Ref")  # same type on parts and products
    with pytest.raises(UnsupportedCapability) as info:
        DocumentTools(connected(app)).execute("catia_new_part", {})
    assert info.value.modified is True and "no 3D Shape" in str(info.value)
    assert load_not_part_types() == ["LS_Ref"] and PART_KIND not in load_working_types()
    assert "LS_Ref" not in load_persisted_refusals()  # not a licence refusal
    service.attempts.clear()
    DocumentTools(connected(app)).execute("catia_new_part", {})
    assert service.attempts == ["3DShape"]


# ── 2. 3D Shape fallback warns about drawings ──


def test_shape_fallback_warns_that_it_cannot_be_drawn():
    app, service = site()
    text = DocumentTools(connected(app)).execute("catia_new_part", {"name": "Block"})
    assert service.attempts == ["3DPart", "3DShape"]
    assert "Created new 3DShape 'Block'" in text
    assert "cannot be put on a drawing or into an assembly; to draw it" in text
    assert SHAPE_DRAWING_WARNING in text


def test_refused_explicit_3d_part_tells_the_agent_to_open_a_ui_part():
    app, _ = site()
    with pytest.raises(UnsupportedCapability, match="open any 3D Part they made"):
        DocumentTools(connected(app)).execute("catia_new_part", {"plm_type": "3DPart"})


# ── 3. Physical Product creation ──


def test_new_product_uses_the_discovered_product_type():
    persist_refusal("VPMReference", REFUSAL)
    app, service = site()
    open_product(app, "Rig", "LS_Product")
    text = DocumentTools(connected(app)).execute("catia_new_product", {"name": "Frame"})
    assert service.attempts == ["LS_Product"]
    assert "Physical Product (LS_Product) 'Frame'" in text
    assert load_working_types() == {PRODUCT_KIND: "LS_Product"}


def test_new_product_with_everything_refused_is_typed_and_not_retried():
    app, service = site()
    with pytest.raises(UnsupportedCapability, match="open any Physical Product"):
        DocumentTools(connected(app)).execute("catia_new_product", {})
    with pytest.raises(UnsupportedCapability, match="not retried") as info:
        DocumentTools(connected(app)).execute("catia_new_product", {})
    assert info.value.modified is False
    assert service.attempts == ["VPMReference"]


def test_add_new_part_uses_the_learned_type_and_never_falls_back_to_a_shape():
    persist_refusal("3DPart", REFUSAL)
    app, service = site()
    _, _root = open_product(app, "Rig")
    with pytest.raises(UnsupportedCapability) as info:
        AssemblyTools(connected(app)).execute("catia_add_new_part", {})
    assert "earlier on this machine" in str(info.value)
    assert "catia_insert_existing_component" in str(info.value)
    assert info.value.modified is False and service.attempts == []


# ── 4. PLM attributes on the root 3D Part ──


def test_get_attributes_auto_reads_the_root_3d_part_with_its_part_number():
    app, _ = site()
    open_3d_part(app, "test part A", "LS_Part", part_number="1000001")
    tools = PLMTools(connected(app))
    result = json.loads(tools.execute("catia_get_plm_attributes", {}))
    assert result["attributes"]["V_PartNumber"] == "1000001"
    assert result["read_from"] == {
        "level": "3D Part (root reference)",
        "title": "test part A",
        "type": "LS_Part",
    }
    shape = json.loads(tools.execute("catia_get_plm_attributes", {"target": "shape"}))
    assert shape["read_from"]["level"] == "3D Shape (representation)"
    assert "V_PartNumber" in shape["unavailable"]


def test_get_attributes_without_ein_says_none_is_assigned():
    app, _ = site()
    open_3d_part(app, "test part A", "LS_Part")
    result = json.loads(PLMTools(connected(app)).execute("catia_get_plm_attributes", {"target": "part"}))
    assert "V_PartNumber" in result["unavailable"] and "Enterprise Item Number" in result["note"]


def test_part_target_on_a_standalone_shape_is_typed():
    app, _ = site()
    app.open_editor(make_part("Shape1"), "Shape")
    tools = PLMTools(connected(app))
    with pytest.raises(UnsupportedCapability, match="no root 3D Part"):
        tools.execute("catia_get_plm_attributes", {"target": "part"})
    auto = json.loads(tools.execute("catia_get_plm_attributes", {}))
    assert auto["read_from"]["level"] == "3D Shape (representation)"
    with pytest.raises(InvalidArgument):
        tools.execute("catia_get_plm_attributes", {"target": "everything"})


# ── 5. V_PartNumber / EIN ──


def test_set_part_number_is_verified_by_read_back():
    app, _ = site()
    _, reference, _ = open_3d_part(app, "test part A", "LS_Part")
    result = json.loads(
        PLMTools(connected(app)).execute(
            "catia_set_plm_attributes", {"attributes": {"V_PartNumber": "1000001"}, "target": "part"}
        )
    )
    assert result["applied"] == {"V_PartNumber": "1000001"}
    assert reference.attributes["V_PartNumber"] == "1000001"
    assert result["written_to"]["level"] == "3D Part (root reference)"


def test_part_number_silently_unchanged_is_unsupported():
    app, _ = site()
    _, reference, _ = open_3d_part(app, "test part A", "LS_Part")
    reference.SetAttributeValue = lambda name, value: None  # site ignores the write
    with pytest.raises(UnsupportedCapability, match="Assign Enterprise Item Number") as info:
        PLMTools(connected(app)).execute(
            "catia_set_plm_attributes", {"attributes": {"V_PartNumber": "1000001"}}
        )
    assert info.value.modified is False and "unchanged" in str(info.value)


def test_part_number_write_refused_with_other_attributes_applied_reports_modified():
    app, _ = site()
    _, reference, _ = open_3d_part(app, "test part A", "LS_Part")
    original = reference.SetAttributeValue

    def guarded(name, value):
        if name == "V_PartNumber":
            raise RuntimeError("attribute is read-only")
        original(name, value)

    reference.SetAttributeValue = guarded
    with pytest.raises(UnsupportedCapability, match="read-only") as info:
        PLMTools(connected(app)).execute(
            "catia_set_plm_attributes",
            {"attributes": {"V_description": "Block", "V_PartNumber": "1"}},
        )
    assert info.value.modified is True and "V_description" in str(info.value)


def test_assign_part_number_with_a_number_writes_and_reads_back():
    app, _ = site()
    _, _reference, _ = open_3d_part(app, "test part A", "LS_Part")
    result = PLMTools(connected(app)).execute("catia_assign_part_number", {"part_number": "1000001"})
    assert result["ok"] and result["data"]["part_number"] == "1000001"
    assert result["effects"]["modified"] is True


def test_assign_part_number_without_a_number_starts_the_command_and_reads_back(monkeypatch):
    monkeypatch.setattr(plm_tools_module, "_sleep", lambda s: None)
    app, _ = site()
    _, reference, _ = open_3d_part(app, "test part A", "LS_Part")
    started = []

    def start_command(name):
        started.append(name)
        if name != "Assign Enterprise Item Number":
            raise RuntimeError("unknown command")
        reference.attributes["V_PartNumber"] = "1201267"

    app.StartCommand = start_command
    result = PLMTools(connected(app)).execute("catia_assign_part_number", {})
    assert started == ["Assign Enterprise Item Number"]  # stops at the first accepted
    assert result["code"] == "OK" and result["data"]["part_number"] == "1201267"


def test_assign_part_number_dialog_pending_is_reported(monkeypatch):
    monkeypatch.setattr(plm_tools_module, "_sleep", lambda s: None)
    app, _ = site()
    open_3d_part(app, "test part A", "LS_Part")
    started = []
    app.StartCommand = started.append  # opens a dialog; nothing changes yet
    result = PLMTools(connected(app)).execute("catia_assign_part_number", {"command": "My EIN"})
    assert started == ["My EIN"]
    assert result["ok"] and result["code"] == "PENDING_USER_ACTION"
    assert result["effects"]["modified"] is None and "complete it" in result["message"]


def test_assign_part_number_keeps_an_existing_number():
    app, _ = site()
    open_3d_part(app, "test part A", "LS_Part", part_number="1000001")
    app.StartCommand = lambda name: pytest.fail("command started")
    result = PLMTools(connected(app)).execute("catia_assign_part_number", {})
    assert result["data"]["part_number"] == "1000001" and result["effects"]["modified"] is False


def test_assign_part_number_on_a_shape_is_typed():
    app, _ = site()
    app.open_editor(make_part("Shape1"), "Shape")
    with pytest.raises(UnsupportedCapability):
        PLMTools(connected(app)).execute("catia_assign_part_number", {"part_number": "1"})


def test_new_tool_is_listed_with_annotations():
    names = {t["name"]: t for t in PLMTools(connected(App3DX())).get_tool_definitions()}
    assert names["catia_assign_part_number"]["annotations"]["readOnlyHint"] is False
    assert names["catia_get_plm_attributes"]["inputSchema"]["properties"]["target"]["enum"] == [
        "auto", "part", "shape"
    ]


# ── Review follow-ups ──


def test_auto_part_creation_shows_at_most_two_licence_dialogs_per_call():
    # Three customized part types open, all refused, plus 3DPart: each refusal
    # is a modal dialog, so one call stops after MAX_AUTO_ATTEMPTS failures.
    refused = ("3DPart", "VPMReference", "LS_A", "LS_B", "LS_C")
    app, service = site(refuse=refused)
    for title, kind in (("a", "LS_A"), ("b", "LS_B"), ("c", "LS_C")):
        open_3d_part(app, title, kind)
    text = DocumentTools(connected(app)).execute("catia_new_part", {})
    assert service.attempts == ["LS_A", "LS_B", "3DShape"]
    assert "Not tried in this call" in text and "LS_C" in text
    assert SHAPE_DRAWING_WARNING in text
    # The next call tries the deferred candidates, never the refused ones.
    service.attempts.clear()
    DocumentTools(connected(app)).execute("catia_new_part", {})
    assert service.attempts == ["LS_C", "3DPart", "3DShape"]


def test_skipped_type_with_colon_is_reported_whole():
    persist_refusal("3DPart", REFUSAL)
    persist_refusal("LS:Part", "Description: nope")
    app, service = site(refuse=("3DPart", "VPMReference", "LS:Part"))
    open_3d_part(app, "test part A", "LS:Part")
    text = DocumentTools(connected(app)).execute("catia_new_part", {})
    assert service.attempts == ["3DShape"]
    assert "A LS:Part was refused earlier" in text and "plm_type='LS:Part'" in text


def test_part_number_refusal_counts_other_written_attributes_even_if_read_back_differs():
    app, _ = site()
    _, reference, _ = open_3d_part(app, "test part A", "LS_Part")
    original = reference.SetAttributeValue

    def guarded(name, value):
        if name == "V_PartNumber":
            raise RuntimeError("attribute is read-only")
        original(name, str(value).upper())  # CATIA normalizes the value

    reference.SetAttributeValue = guarded
    with pytest.raises(UnsupportedCapability) as info:
        PLMTools(connected(app)).execute(
            "catia_set_plm_attributes",
            {"attributes": {"V_description": "block", "V_PartNumber": "1"}},
        )
    assert info.value.modified is True and "V_description" in str(info.value)


def test_assign_same_part_number_reports_no_change():
    app, _ = site()
    open_3d_part(app, "test part A", "LS_Part", part_number="1000001")
    result = PLMTools(connected(app)).execute("catia_assign_part_number", {"part_number": "1000001"})
    assert result["ok"] and result["effects"]["modified"] is False
