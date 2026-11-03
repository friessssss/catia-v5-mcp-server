"""Offline contract tests for Part Design / Sketcher robustness.

They use the COM doubles of tests/fakes.py plus a few local ones. They pin
orchestration contracts (typed failure codes, broken features removed,
volume verification, open-sketch handling, session reset); they do not
prove live CATIA behavior.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from catia_mcp.connection import PLATFORM_3DX, PLATFORM_V5
from catia_mcp.server import CATIAMCPServer
from catia_mcp.tools.gsd import GSDTools
from catia_mcp.tools.part_design import PartDesignTools
from catia_mcp.tools.sketcher import SketcherTools
from tests.fakes import App3DX, Coll, Hole, Selection, VolumeService, make_part

_PLATFORMS = [PLATFORM_V5, PLATFORM_3DX]
_BLOCK_MM3 = 240000.0


def run(coro):
    return asyncio.run(coro)


async def _call(server: CATIAMCPServer, name: str, arguments: dict | None = None):
    return await server.call_tool(name, arguments or {})


def _text(result) -> str:
    return result.content[0].text


def _code(result) -> str | None:
    return (result.structuredContent or {}).get("code")


class Rig:
    """Server on one platform with a part, a measurable body volume and a
    selection whose Delete records what was removed from the tree."""

    def __init__(self, platform: str, *, measurable: bool = True, volume_mm3: float = _BLOCK_MM3) -> None:
        self.part = make_part()
        self.part.CreateReferenceFromObject = lambda obj: obj
        self.part.UpdateObject = lambda obj: None
        self.volume = VolumeService(volume_mm3)
        self.selection = Selection()
        self.deleted: list = []
        self.selection.Delete = lambda: self.deleted.extend(self.selection.items)
        if platform == PLATFORM_V5:
            spa = (
                SimpleNamespace(GetMeasurable=lambda ref: SimpleNamespace(Volume=self.volume.volume_mm3 / 1e9))
                if measurable
                else None
            )
            document = SimpleNamespace(
                Part=self.part, Selection=self.selection, GetWorkbench=lambda name: spa
            )
            app = SimpleNamespace(
                Caption="CATIA V5",
                ActiveDocument=document,
                ActiveWindow=SimpleNamespace(ActiveViewer=SimpleNamespace(Reframe=lambda: None)),
            )
        else:
            app = App3DX()
            services = {"MeasurableService": self.volume} if measurable else {}
            editor = app.open_editor(self.part, "Shape", services)
            editor.Selection = self.selection
        self.server = CATIAMCPServer()
        self.server.connection.app = app
        self.server.connection.platform = platform

    def call(self, name: str, arguments: dict | None = None):
        return run(_call(self.server, name, arguments))

    def close(self) -> None:
        run(self.server.close())


@pytest.fixture(params=_PLATFORMS)
def rig(request):
    made: list[Rig] = []

    def factory(**kwargs) -> Rig:
        r = Rig(request.param, **kwargs)
        made.append(r)
        return r

    yield factory
    for r in made:
        r.close()


def _add_sketch(part, name: str = "Sketch.1") -> SimpleNamespace:
    sketch = SimpleNamespace(Name=name, closed=0)
    sketch.CloseEdition = lambda: setattr(sketch, "closed", sketch.closed + 1)
    part.MainBody.Sketches.append(sketch)
    return sketch


# ── Failed updates remove the broken feature ──


def test_failed_fillet_update_deletes_the_feature(rig):
    r = rig()
    r.part.MainBody.Shapes.append(SimpleNamespace(Name="Pad.1"))
    fillet = SimpleNamespace(Name="EdgeFillet.1")
    r.part.ShapeFactory.AddNewSolidEdgeFilletWithConstantRadius = lambda target, prop, radius: fillet

    def update(obj):
        raise RuntimeError("Update error: radius too large")

    r.part.UpdateObject = update
    result = r.call("catia_fillet", {"radius": 50})
    assert result.isError
    assert _code(result) == "FEATURE_UPDATE_FAILED"
    assert result.structuredContent["effects"]["modified"] is None
    assert r.deleted == [fillet]
    message = result.structuredContent["message"]
    assert "EdgeFillet.1" in message and "removed from the tree" in message
    assert r.selection.items == []  # selection left clean


def test_failed_chamfer_that_cannot_be_deleted_says_so(rig):
    r = rig()
    r.part.MainBody.Shapes.append(SimpleNamespace(Name="Pad.1"))
    r.part.ShapeFactory.AddNewChamfer = lambda *a: SimpleNamespace(Name="Chamfer.1")
    r.part.UpdateObject = lambda obj: (_ for _ in ()).throw(RuntimeError("bad"))

    def refuse():
        raise RuntimeError("delete refused")

    r.selection.Delete = refuse
    result = r.call("catia_chamfer", {"length": 2})
    assert _code(result) == "FEATURE_UPDATE_FAILED"
    message = result.structuredContent["message"]
    assert "could NOT be removed" in message and "catia_delete_feature" in message


def _revolve_rig(r, kind: str):
    sketch = _add_sketch(r.part)
    sketch.GeometricElements = Coll([SimpleNamespace(Name="AbsoluteAxis", GetItem=lambda name: "vdir")])
    feature = SimpleNamespace(Name=f"{kind}.1", RevoluteAxis=None, FirstAngle=None)
    setattr(r.part.ShapeFactory, f"AddNew{kind}", lambda sketch: feature)
    return feature


def test_shaft_keeps_the_full_update_fallback(rig):
    r = rig()
    shaft = _revolve_rig(r, "Shaft")
    r.part.UpdateObject = lambda obj: (_ for _ in ()).throw(RuntimeError("UpdateObject E_FAIL"))
    full = []
    r.part.Update = lambda: full.append(1)
    result = r.call("catia_shaft", {"angle": 180})
    assert not result.isError, _text(result)
    assert full == [1] and r.deleted == [] and shaft.FirstAngle == 180


def test_groove_failing_both_updates_is_removed(rig):
    r = rig()
    groove = _revolve_rig(r, "Groove")
    r.part.UpdateObject = lambda obj: (_ for _ in ()).throw(RuntimeError("E_FAIL"))
    r.part.Update = lambda: (_ for _ in ()).throw(RuntimeError("E_FAIL"))
    result = r.call("catia_groove", {})
    assert _code(result) == "FEATURE_UPDATE_FAILED"
    assert r.deleted == [groove]


# ── Pad ──


def _pad_rig(r, adds_mm3: float, pad_cls=None):
    _add_sketch(r.part)
    r.part.MainBody.Shapes.append(SimpleNamespace(Name="Pad.0"))
    pads: list = []

    def add_pad(sketch, height):
        pad = (pad_cls or SimpleNamespace)()
        pad.Name = f"Pad.{len(pads) + 1}"
        pad.sketch = sketch
        pads.append(pad)
        return pad

    def update(obj):
        if obj in pads:  # sketch updates (catia_close_sketch) add nothing
            r.volume.volume_mm3 = _BLOCK_MM3 + adds_mm3

    r.part.ShapeFactory.AddNewPad = add_pad
    r.part.UpdateObject = update
    return pads


def test_pad_reports_the_added_volume(rig):
    r = rig()
    pads = _pad_rig(r, 1000.0)
    result = r.call("catia_pad", {"height": 10})
    assert not result.isError, _text(result)
    assert _text(result) == "Pad 'Pad.1' created: 10 mm (normal). Added 1000.0 mm3 (240000.0 -> 241000.0)."
    assert len(pads) == 1


def test_first_pad_of_an_empty_body_counts_from_zero(rig):
    r = rig(volume_mm3=0.0)
    _add_sketch(r.part)
    r.part.ShapeFactory.AddNewPad = lambda sketch, h: SimpleNamespace(Name="Pad.1")
    r.part.UpdateObject = lambda obj: setattr(r.volume, "volume_mm3", 240000.0)
    result = r.call("catia_pad", {"height": 40})
    assert not result.isError, _text(result)
    assert "Added 240000.0 mm3 (0.0 -> 240000.0)" in _text(result)


def test_pad_adding_nothing_is_feature_no_effect(rig):
    r = rig()
    _pad_rig(r, 0.0)
    result = r.call("catia_pad", {"height": 10})
    assert _code(result) == "FEATURE_NO_EFFECT"
    assert result.structuredContent["effects"]["modified"] is None
    assert "Pad.1" in result.structuredContent["message"]


def test_pad_without_volume_measurement_says_so(rig):
    r = rig(measurable=False)
    _pad_rig(r, 1000.0)
    result = r.call("catia_pad", {"height": 10})
    assert not result.isError, _text(result)
    assert "volume verification unavailable" in _text(result)


class _ParamOnlyPad:
    """Factory-returned pad whose direct property PUTs fail (B30 evidence);
    the extent is set through the parameter object's Value."""

    def __init__(self) -> None:
        object.__setattr__(self, "_params", {
            "IsSymmetric": SimpleNamespace(Value=False),
            "DirectionOrientation": SimpleNamespace(Value=0),
        })

    def __getattr__(self, name):
        params = object.__getattribute__(self, "_params")
        if name in params:
            return params[name]
        raise AttributeError(name)

    def __setattr__(self, name, value):
        if name in object.__getattribute__(self, "_params"):
            raise RuntimeError("DISP_E_MEMBERNOTFOUND on PUT")
        object.__setattr__(self, name, value)


@pytest.mark.parametrize(("args", "prop", "value"), [
    ({"symmetric": True}, "IsSymmetric", True),
    ({"direction": "both"}, "IsSymmetric", True),
    ({"direction": "reverse"}, "DirectionOrientation", 1),
])
def test_pad_extent_goes_through_the_parameter_fallback(rig, args, prop, value):
    r = rig()
    pads = _pad_rig(r, 1000.0, _ParamOnlyPad)
    result = r.call("catia_pad", {"height": 10, **args})
    assert not result.isError, _text(result)
    assert getattr(pads[0], prop).Value == value


def test_pad_extent_rejected_entirely_is_typed_with_a_delete_hint(rig):
    class Stubborn:
        def __setattr__(self, name, value):
            if name == "IsSymmetric":
                raise RuntimeError("rejected")
            object.__setattr__(self, name, value)

    r = rig()
    _pad_rig(r, 1000.0, Stubborn)
    result = r.call("catia_pad", {"height": 10, "symmetric": True})
    assert _code(result) == "WRONG_GEOMETRY_EFFECT"
    assert result.structuredContent["effects"]["modified"] is None
    assert "catia_delete_feature" in result.structuredContent["message"]


# ── Hole ──


class _UntypedHole(Hole):
    """Hole whose Type CATIA refuses (neither PUT nor parameter object)."""

    @property
    def Type(self):
        return 0

    @Type.setter
    def Type(self, value):
        raise RuntimeError("E_FAIL")


def _hole_rig(r, hole_cls=Hole):
    _add_sketch(r.part, "Sketch.2")
    r.part.MainBody.Shapes.append(SimpleNamespace(Name="Pad.1"))
    holes: list = []
    updates: list = []

    def add_hole(sketch, depth):
        hole = hole_cls(f"Hole.{len(holes) + 1}")
        holes.append(hole)
        return hole

    def update(obj):
        updates.append(obj)
        r.volume.volume_mm3 = _BLOCK_MM3 - 1000.0 * len(holes)

    r.part.ShapeFactory.AddNewHoleFromSketch = add_hole
    r.part.UpdateObject = update
    return holes, updates


def test_hole_type_rejected_is_wrong_geometry_effect(rig):
    r = rig()
    _hole_rig(r, _UntypedHole)
    result = r.call("catia_hole", {"diameter": 8, "depth": 20, "type": "counterbored"})
    assert _code(result) == "WRONG_GEOMETRY_EFFECT"
    assert result.structuredContent["effects"]["modified"] is None
    message = result.structuredContent["message"]
    assert "Hole.1" in message and "counterbored" in message and "remains in the tree" in message


def test_hole_type_applied_updates_once(rig):
    r = rig()
    holes, updates = _hole_rig(r)
    result = r.call("catia_hole", {"diameter": 8, "depth": 20, "type": "countersunk"})
    assert not result.isError, _text(result)
    assert holes[0].Type == 3
    assert updates == [holes[0]]  # one update, not two
    assert "Type: countersunk" in _text(result)


# ── Typed input / lookup errors ──


@pytest.mark.parametrize(("tool", "args"), [
    ("catia_fillet", {"radius": 0}),
    ("catia_chamfer", {"length": -1}),
    ("catia_chamfer", {"length": 1, "angle": 90}),
    ("catia_pad", {"height": 0}),
    ("catia_shaft", {"angle": 0}),
    ("catia_groove", {"angle": 400}),
    ("catia_circ_pattern", {"count": 0}),
    ("catia_rect_pattern", {"dir1_count": 2, "dir1_spacing": 10, "dir1_edge": "Edge.1"}),
    ("catia_shell", {"thickness": 2}),
    ("catia_draft", {"angle": 3}),
    ("catia_thickness", {"offset": 1}),
])
def test_bad_input_is_invalid_argument_untouched(rig, tool, args):
    r = rig()
    r.part.MainBody.Shapes.append(SimpleNamespace(Name="Pad.1"))
    r.part.OriginElements = SimpleNamespace(PlaneXY="xy", PlaneYZ="yz", PlaneZX="zx")
    result = r.call(tool, args)
    assert _code(result) == "INVALID_ARGUMENT", _text(result)
    assert result.structuredContent["effects"]["modified"] is False


@pytest.mark.parametrize(("tool", "args"), [
    ("catia_pad", {"height": 10}),  # no sketch at all
    ("catia_pad", {"height": 10, "sketch_name": "Nope"}),
    ("catia_delete_feature", {"feature_name": "Nope.1"}),
    ("catia_list_faces", {"body_name": "Nope"}),
])
def test_missing_objects_are_object_not_found_untouched(rig, tool, args):
    r = rig()
    result = r.call(tool, args)
    assert _code(result) == "OBJECT_NOT_FOUND", _text(result)
    assert result.structuredContent["effects"]["modified"] is False


def test_sketcher_bad_input_is_invalid_argument(rig):
    r = rig()
    tools: SketcherTools = r.server.sketcher_tools
    tools._active_sketch = SimpleNamespace(Name="Sketch.1", Constraints=None, GeometricElements=None)
    r.server.connection.open_sketch = tools._active_sketch
    result = r.call("catia_sketch_constraint", {"type": "radius", "geometry_index_1": 1})
    assert _code(result) == "INVALID_ARGUMENT"
    assert result.structuredContent["effects"]["modified"] is False


def test_sketcher_without_a_sketch_is_object_not_found(rig):
    r = rig()
    result = r.call("catia_sketch_line", {"x1": 0, "y1": 0, "x2": 1, "y2": 1})
    assert _code(result) == "OBJECT_NOT_FOUND"


# ── Feature types ──


class _TypedShape:
    """Late-bound COM object exposing its class name via type information."""

    def __init__(self, name: str, cls: str) -> None:
        self.Name = name
        info = SimpleNamespace(GetDocumentation=lambda index: (cls, "", 0, ""))
        self._oleobj_ = SimpleNamespace(GetTypeInfo=lambda: info)


def test_list_features_reports_type_information_then_falls_back(rig):
    r = rig()
    r.part.MainBody.Shapes = Coll([
        _TypedShape("Pad.1", "Pad"),
        _TypedShape("Trou.1", "Hole"),  # localized tree name, class from type info
        SimpleNamespace(Name="EdgeFillet.3"),  # no type information
        Hole("Hole.9"),  # Hole.Type is the CatHoleType enum, not a type name
    ])
    result = r.call("catia_list_features")
    features = json.loads(_text(result))
    assert [f["type"] for f in features] == ["Pad", "Hole", "EdgeFillet", "Hole"]


# ── Open sketch ──


def _sketchable_part(r):
    """Part whose sketches can be created and opened through the sketcher."""
    factory = SimpleNamespace(lines=[])
    factory.CreateLine = lambda *a: factory.lines.append(a)
    r.part.OriginElements = SimpleNamespace(PlaneXY="xy", PlaneYZ="yz", PlaneZX="zx")
    sketches = r.part.MainBody.Sketches

    def add(ref):
        sketch = SimpleNamespace(Name=f"Sketch.{sketches.Count + 1}", closed=0)
        sketch.OpenEdition = lambda: factory
        sketch.CloseEdition = lambda: setattr(sketch, "closed", sketch.closed + 1)
        sketches.append(sketch)
        return sketch

    sketches.Add = add
    return factory


def test_open_sketch_is_closed_before_pad_and_used_as_profile(rig):
    r = rig()
    _sketchable_part(r)
    pads = _pad_rig(r, 1000.0)  # adds Sketch.1 (closed) before ours
    created = r.call("catia_create_sketch", {"plane": "xy"})
    assert not created.isError, _text(created)
    sketch = r.part.MainBody.Sketches.Item(2)
    assert r.server.connection.open_sketch is sketch

    result = r.call("catia_pad", {"height": 10})
    assert not result.isError, _text(result)
    assert sketch.closed == 1
    assert pads[0].sketch is sketch  # built on the sketch just drawn
    assert "sketch 'Sketch.2' was still open" in _text(result)
    assert r.server.connection.open_sketch is None

    # The sketcher notices: closing again is a no-op, drawing needs a new sketch.
    again = r.call("catia_close_sketch")
    assert not again.isError and "already closed" in _text(again)
    assert sketch.closed == 1
    line = r.call("catia_sketch_line", {"x1": 0, "y1": 0, "x2": 1, "y2": 1})
    assert _code(line) == "OBJECT_NOT_FOUND"


def test_explicit_close_clears_the_shared_open_sketch(rig):
    r = rig()
    _sketchable_part(r)
    _pad_rig(r, 1000.0)
    r.call("catia_create_sketch", {"plane": "xy"})
    r.call("catia_close_sketch")
    assert r.server.connection.open_sketch is None
    result = r.call("catia_pad", {"height": 10})
    assert not result.isError, _text(result)
    assert "still open" not in _text(result)


def test_auto_close_note_survives_a_typed_failure(rig):
    r = rig()
    _sketchable_part(r)
    _pad_rig(r, 0.0)
    r.call("catia_create_sketch", {"plane": "xy"})
    result = r.call("catia_pad", {"height": 10})
    assert _code(result) == "FEATURE_NO_EFFECT"
    assert "was still open" in result.structuredContent["message"]


# ── Session reset ──


def test_reset_session_state_clears_cached_proxies():
    conn = SimpleNamespace(open_sketch=object())
    sketcher = SketcherTools(conn)
    sketcher._active_sketch = object()
    sketcher._active_factory = object()
    sketcher._sketch_part = object()
    sketcher.reset_session_state()
    assert sketcher._active_sketch is None and sketcher._active_factory is None
    assert sketcher._sketch_part is None and conn.open_sketch is None

    conn.open_sketch = object()
    part_design = PartDesignTools(conn)
    old_measurer = part_design.measure
    part_design.measure._codes["volume"] = 3
    part_design._auto_closed_sketch = "Sketch.1"
    part_design.reset_session_state()
    assert part_design.measure is not old_measurer and part_design.measure._codes == {}
    assert part_design._auto_closed_sketch is None and conn.open_sketch is None

    conn.open_sketch = object()
    gsd = GSDTools(conn)
    gsd._active_geoset_name = "GSD_Set"
    gsd._created_points = 2
    gsd.reset_session_state()
    assert gsd._active_geoset_name is None and gsd._created_points == 0
    assert conn.open_sketch is None


# ── Review follow-ups ──


def test_pad_within_measurement_noise_is_feature_no_effect(rig):
    r = rig()
    _pad_rig(r, 1e-6)  # re-measurement drift, not material
    result = r.call("catia_pad", {"height": 10})
    assert _code(result) == "FEATURE_NO_EFFECT", _text(result)


def test_failed_auto_close_keeps_the_sketch_published(rig):
    r = rig()
    _sketchable_part(r)
    _pad_rig(r, 1000.0)
    r.call("catia_create_sketch", {"plane": "xy"})
    sketch = r.server.connection.open_sketch

    def refuse():
        raise RuntimeError("CloseEdition E_FAIL")

    sketch.CloseEdition = refuse
    result = r.call("catia_pad", {"height": 10})
    assert result.isError and "could not be closed" in result.structuredContent["message"]
    # Still open in CATIA: the sketcher must not claim it is already closed.
    assert r.server.connection.open_sketch is sketch
    again = r.call("catia_close_sketch")
    assert "already closed" not in _text(again)


def test_open_point_sketch_is_closed_before_hole(rig):
    r = rig()
    _sketchable_part(r)
    _hole_rig(r)
    r.call("catia_create_sketch", {"plane": "xy"})
    sketch = r.server.connection.open_sketch
    result = r.call("catia_hole", {"diameter": 8, "depth": 20})
    assert not result.isError, _text(result)
    assert sketch.closed == 1 and r.server.connection.open_sketch is None
    assert "was still open" in _text(result)
