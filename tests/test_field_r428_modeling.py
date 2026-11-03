"""Offline contract tests for the modeling fixes from the 2026-11-02 R428
field report (field report 2026-11-02; kept outside the repository).

They drive PartDesignTools / SketcherTools directly on the COM doubles of
tests/fakes.py. They pin orchestration (which references, which calls, in
which order, what is reported); they do not prove live CATIA behavior.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from catia_mcp.connection import PLATFORM_3DX, PLATFORM_V5, CATIAConnection
from catia_mcp.errors import CATIAToolError, InvalidArgument
from catia_mcp.tools.part_design import PartDesignTools
from catia_mcp.tools.sketcher import SketcherTools
from tests.fakes import App3DX, Coll, Hole, Selection, VolumeService, make_part

_BLOCK_MM3 = 600000.0  # 100 x 100 x 60 mm field block


def _connected(app, platform=PLATFORM_3DX) -> CATIAConnection:
    conn = CATIAConnection(platform=platform)
    conn.app = app
    conn.platform = platform
    return conn


def _cells(prefix: str, count: int) -> list[SimpleNamespace]:
    return [SimpleNamespace(Reference=f"{prefix}-{i}") for i in range(1, count + 1)]


class _Fillet:
    def __init__(self, first) -> None:
        self.Name = "EdgeFillet.1"
        self.edges = [first]

    def AddObjectToFillet(self, ref):
        self.edges.append(ref)


def _box_3dx():
    """Padded box as R428 exposes it: the generic body Edge search also
    returns the 4 sketch lines (16), the CGMEdge search on the Pad returns
    the 12 solid edges."""
    part = make_part()
    body = part.MainBody
    body.topology = {"Edge": _cells("body-edge", 16), "Face": _cells("face", 6)}
    pad = SimpleNamespace(Name="Pad.1", topology={"CGMEdge": _cells("cgm-edge", 12)})
    body.Shapes.append(pad)
    created: list[_Fillet] = []

    def add_fillet(target, propagation, radius):
        assert propagation == 1
        fillet = _Fillet(target)
        fillet.radius = radius
        created.append(fillet)
        return fillet

    part.ShapeFactory = SimpleNamespace(AddNewSolidEdgeFilletWithConstantRadius=add_fillet)
    updates: list = []
    part.UpdateObject = updates.append
    app = App3DX()
    app.open_editor(part, "Part")
    return PartDesignTools(_connected(app)), part, created, updates


def test_3dx_fillet_all_edges_uses_topology_search_and_add_object_to_fillet():
    tools, _, created, updates = _box_3dx()
    result = tools.execute("catia_fillet", {"radius": 5})
    assert len(created) == 1
    fillet = created[0]
    assert fillet.edges == [f"cgm-edge-{i}" for i in range(1, 13)]
    assert fillet.radius == 5
    assert updates == [fillet]
    assert "all 12 edges" in result


def test_3dx_list_edges_and_fillet_share_one_enumeration():
    tools, _, created, _ = _box_3dx()
    edges = json.loads(tools.execute("catia_list_edges", {}))
    assert [e["name"] for e in edges] == [f"Edge.{i}" for i in range(1, 13)]  # not 16
    tools.execute("catia_fillet", {"radius": 2, "edge_name": edges[-1]["name"]})
    assert created[0].edges == ["cgm-edge-12"]


def test_3dx_fillet_edge_out_of_range_is_object_not_found():
    tools, _, created, _ = _box_3dx()
    with pytest.raises(CATIAToolError) as info:
        tools.execute("catia_fillet", {"radius": 2, "edge_name": "Edge.13"})
    assert info.value.code == "OBJECT_NOT_FOUND"
    assert created == []


def test_3dx_fillet_add_object_refusal_removes_the_partial_fillet():
    tools, part, created, _ = _box_3dx()
    selection = tools.conn.hso
    deleted: list = []
    selection.Delete = lambda: deleted.extend(selection.items)

    def refuse(ref):
        raise RuntimeError("bad edge")

    original = part.ShapeFactory.AddNewSolidEdgeFilletWithConstantRadius

    def add(target, propagation, radius):
        fillet = original(target, propagation, radius)
        fillet.AddObjectToFillet = refuse
        return fillet

    part.ShapeFactory.AddNewSolidEdgeFilletWithConstantRadius = add
    with pytest.raises(CATIAToolError) as info:
        tools.execute("catia_fillet", {"radius": 5})
    assert info.value.code == "FEATURE_UPDATE_FAILED"
    assert deleted == [created[0]]
    assert "removed from the tree" in str(info.value)


def test_v5_fillet_keeps_the_whole_feature_reference():
    part = make_part()
    pad = SimpleNamespace(Name="Pad.1")
    part.MainBody.Shapes.append(pad)
    part.CreateReferenceFromObject = lambda obj: ("ref", obj)
    seen: list = []
    part.ShapeFactory = SimpleNamespace(
        AddNewSolidEdgeFilletWithConstantRadius=lambda t, p, r: seen.append(t) or SimpleNamespace(Name="EdgeFillet.1")
    )
    part.UpdateObject = lambda obj: None
    document = SimpleNamespace(Part=part, Selection=Selection(), GetWorkbench=lambda name: None)
    app = SimpleNamespace(
        Caption="CATIA V5",
        ActiveDocument=document,
        ActiveWindow=SimpleNamespace(ActiveViewer=SimpleNamespace(Reframe=lambda: None)),
    )
    PartDesignTools(_connected(app, PLATFORM_V5)).execute("catia_fillet", {"radius": 1})
    assert seen == [("ref", pad)]


# ── Holes ──


class ThreadHole(Hole):
    """Hole with the thread API; a standard table + M6 sets the tap drill."""

    def __init__(self, name: str = "Hole.1") -> None:
        super().__init__(name)
        self.HoleThreadDescription = SimpleNamespace(Value="")
        self.ThreadDepth = SimpleNamespace(Value=10.0)
        self.ThreadDiameter = SimpleNamespace(Value=11.0)
        self.ThreadPitch = SimpleNamespace(Value=1.5)
        self.tables: list[int] = []

    def CreateStandardThreadDesignTable(self, code):
        self.tables.append(code)


class _HoleRig:
    def __init__(self, *, hole_cls=ThreadHole, fail_updates: int = 0, sketch_deleted_with_hole: bool = True):
        self.part = make_part()
        self.part.CreateReferenceFromObject = lambda obj: obj
        body = self.part.MainBody
        body.Shapes.append(SimpleNamespace(Name="Pad.1"))
        point = SimpleNamespace(
            Name="Point.1", GetCoordinates=None, _fill_GetCoordinates=lambda: [10.0, 20.0]
        )
        axis_line = SimpleNamespace(Name="AbsoluteAxis")
        self.line = SimpleNamespace(Name="Line.1")  # no GetCoordinates: never evaluated
        self.sketch = SimpleNamespace(
            Name="Sketch.3",
            GeometricElements=Coll([axis_line, self.line, point]),
            _fill_GetAbsoluteAxisData=lambda: [0.0, 0.0, 60.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0],
            CloseEdition=lambda: None,
        )
        body.Sketches.append(self.sketch)
        self.holes: list = []
        self.volume = VolumeService(_BLOCK_MM3)
        self.fail_updates = fail_updates

        def add_hole(sketch, depth):
            hole = hole_cls(f"Hole.{len(self.holes) + 1}")
            self.holes.append(hole)
            return hole

        def update(obj):
            if self.fail_updates:
                self.fail_updates -= 1
                raise RuntimeError("update error")
            self.volume.volume_mm3 = _BLOCK_MM3 - 1000.0 * len(self.holes)

        def full_update():
            if self.fail_updates:
                self.fail_updates -= 1
                raise RuntimeError("update error")

        self.part.ShapeFactory = SimpleNamespace(AddNewHoleFromSketch=add_hole)
        self.part.UpdateObject = update
        self.part.Update = full_update
        app = App3DX()
        self.app = app
        editor = app.open_editor(self.part, "Part", {"MeasurableService": self.volume})
        self.deleted: list = []

        def delete():
            self.deleted.extend(editor.Selection.items)
            if sketch_deleted_with_hole:
                body.Sketches.items.remove(self.sketch)

        editor.Selection.Delete = delete
        self.tools = PartDesignTools(_connected(app))

    def hole(self, **args):
        return self.tools.execute("catia_hole", args)


class M6Hole(ThreadHole):
    """CATIA's M6 coarse table: the tap drill replaces the diameter."""

    def CreateStandardThreadDesignTable(self, code):
        super().CreateStandardThreadDesignTable(code)
        self.Diameter.Value = 4.917
        self.ThreadDiameter.Value = 6.0
        self.ThreadPitch.Value = 1.0


def test_threaded_hole_sets_standard_designation_depth_and_reads_back():
    rig = _HoleRig(hole_cls=M6Hole)
    result = rig.hole(diameter=6, depth=60, thread_standard="metric_coarse", designation="M6", thread_depth=60)
    hole = rig.holes[0]
    assert hole.tables == [1]
    assert hole.ThreadingMode == 0  # threaded
    assert hole.HoleThreadDescription.Value == "M6"
    assert hole.ThreadDepth.Value == 60
    assert "D4.917 mm" in result
    assert "Threaded: M6 (metric_coarse)" in result
    assert "thread depth 60 mm" in result and "pitch 1 mm" in result


def test_threaded_flag_defaults_to_metric_coarse_m_diameter_and_hole_depth():
    rig = _HoleRig()
    rig.hole(diameter=8, depth=20, threaded=True)
    hole = rig.holes[0]
    assert hole.tables == [1]
    assert hole.HoleThreadDescription.Value == "M8"
    assert hole.ThreadDepth.Value == 20  # not the 10 mm CATIA carried over


def test_smooth_hole_resets_carried_over_threading_before_the_update():
    rig = _HoleRig()
    rig.hole(diameter=6, depth=20)
    hole = rig.holes[0]
    assert hole.ThreadingMode == 1  # smooth; the fake starts threaded (0)
    assert hole.tables == []


@pytest.mark.parametrize(("args", "fragment"), [
    ({"diameter": 6, "depth": 20, "thread_depth": 30}, "exceeds"),
    ({"diameter": 6, "depth": 20, "thread_standard": "unc"}, "designation"),
    ({"diameter": 6}, "depth is required"),
    ({"diameter": 8, "depth": 20, "thread_standard": "metric_fine"}, "M8x1"),
])
def test_bad_thread_arguments_are_refused_before_creating(args, fragment):
    rig = _HoleRig()
    with pytest.raises(InvalidArgument) as info:
        rig.tools.execute("catia_hole", args)
    assert fragment in str(info.value)
    assert rig.holes == []


def test_designation_not_in_table_is_wrong_geometry_effect():
    rig = _HoleRig()
    original_update = rig.part.UpdateObject

    def update(obj):  # CATIA falls back to the table's M6 entry
        original_update(obj)
        obj.HoleThreadDescription.Value = "M6"

    rig.part.UpdateObject = update
    with pytest.raises(CATIAToolError) as info:
        rig.hole(diameter=6, depth=20, designation="M7")
    assert info.value.code == "WRONG_GEOMETRY_EFFECT"
    assert "'M6'" in str(info.value)


def test_failed_hole_update_removes_only_the_hole_and_reports_sketch_points():
    rig = _HoleRig(fail_updates=2)
    with pytest.raises(CATIAToolError) as info:
        rig.hole(diameter=6, depth=60)
    error = info.value
    assert error.code == "FEATURE_UPDATE_FAILED"
    assert rig.deleted == [rig.holes[0]]  # the selection held the hole only
    message = str(error)
    assert "Sketch.3" in message
    assert "(10, 20) = 3D (10, 20, 60)" in message
    assert "catia_sketch_point" in message


def test_failed_hole_update_with_sketch_kept_says_retry_with_it():
    rig = _HoleRig(fail_updates=2, sketch_deleted_with_hole=False)
    with pytest.raises(CATIAToolError) as info:
        rig.hole(diameter=6, depth=60)
    assert "sketch_name='Sketch.3'" in str(info.value)


def test_through_all_sets_up_to_last_and_skips_the_depth():
    class LimitHole(ThreadHole):
        def __init__(self, name="Hole.1"):
            super().__init__(name)
            self.BottomLimit = SimpleNamespace(Dimension=SimpleNamespace(Value=10.0), LimitMode=0)

    rig = _HoleRig(hole_cls=LimitHole)
    result = rig.hole(diameter=6, through_all=True, thread_depth=60)
    hole = rig.holes[0]
    assert hole.BottomLimit.LimitMode == 2
    assert "through all" in result
    assert hole.ThreadDepth.Value == 60


# ── Sketch rectangle dimensions ──


class _Constraints:
    def __init__(self) -> None:
        self.added: list = []

    def AddMonoEltCst(self, kind, ref):
        cst = SimpleNamespace(
            Name=f"Length.{len(self.added) + 1}", kind=kind, ref=ref,
            Mode=None, Dimension=SimpleNamespace(Value=None),
        )
        self.added.append(cst)
        return cst


def _sketcher():
    part = make_part()
    part.CreateReferenceFromObject = lambda obj: ("ref", obj)
    part.OriginElements = SimpleNamespace(PlaneXY="xy")
    lines: list = []
    factory = SimpleNamespace(
        CreateLine=lambda *c: lines.append(SimpleNamespace(coords=c)) or lines[-1]
    )
    constraints = _Constraints()
    sketch = SimpleNamespace(Name="Sketch.1", Constraints=constraints, OpenEdition=lambda: factory)
    part.MainBody.Sketches = SimpleNamespace(Add=lambda ref: sketch)
    app = App3DX()
    app.open_editor(part, "Part")
    tools = SketcherTools(_connected(app))
    tools.execute("catia_create_sketch", {"plane": "xy"})
    return tools, lines, constraints


def test_centered_rectangle_adds_width_and_height_length_constraints():
    tools, _, constraints = _sketcher()
    result = tools.execute("catia_sketch_centered_rectangle", {"width": 100, "height": 80})
    assert [c.kind for c in constraints.added] == [5, 5]
    assert [c.Dimension.Value for c in constraints.added] == [100, 80]
    assert [c.Mode for c in constraints.added] == [0, 0]
    horizontal, vertical = (c.ref[1].coords for c in constraints.added)
    assert horizontal[1] == horizontal[3] and vertical[0] == vertical[2]
    assert "width 100 mm ('Length.1')" in result and "height 80 mm ('Length.2')" in result


def test_rectangle_dimensions_can_be_turned_off():
    tools, lines, constraints = _sketcher()
    tools.execute("catia_sketch_rectangle", {"x1": 0, "y1": 0, "x2": 10, "y2": 5, "dimensions": False})
    assert len(lines) == 4 and constraints.added == []


def test_rectangle_constraint_failure_is_typed_and_says_lines_exist():
    tools, lines, constraints = _sketcher()

    def refuse(kind, ref):
        raise RuntimeError("E_FAIL")

    constraints.AddMonoEltCst = refuse
    with pytest.raises(CATIAToolError) as info:
        tools.execute("catia_sketch_rectangle", {"x1": 0, "y1": 0, "x2": 10, "y2": 5})
    assert info.value.modified is None
    assert "Do not redraw" in str(info.value)
    assert len(lines) == 4


def test_schemas_advertise_the_new_options():
    hole = next(t for t in PartDesignTools(CATIAConnection()).get_tool_definitions() if t["name"] == "catia_hole")
    props = hole["inputSchema"]["properties"]
    assert props["thread_standard"]["enum"] == ["metric_coarse", "metric_fine", "unc", "unf"]
    assert {"designation", "thread_depth", "through_all"} <= set(props)
    assert hole["inputSchema"]["required"] == ["diameter"]
    for name in ("catia_sketch_rectangle", "catia_sketch_centered_rectangle"):
        tool = next(t for t in SketcherTools(CATIAConnection()).get_tool_definitions() if t["name"] == name)
        assert tool["inputSchema"]["properties"]["dimensions"]["default"] is True


# ── Review follow-ups ──


def test_v5_topology_search_error_still_propagates():
    from catia_mcp.references import search_final_topology

    class Broken(Selection):
        def Search(self, query):
            raise RuntimeError("search failed")

    body = SimpleNamespace(Shapes=Coll([SimpleNamespace(Name="Pad.1")]))
    with pytest.raises(RuntimeError, match="search failed"):
        search_final_topology(Broken(), body, "Edge", is_3dx=False)


def test_3dx_unknown_cgmedge_type_falls_back_to_the_generic_edge_search():
    from catia_mcp.references import search_final_topology

    class NoCGM(Selection):
        def Search(self, query):
            if "CGMEdge" in query:
                raise RuntimeError("unknown type")
            super().Search(query)

    body = SimpleNamespace(
        Shapes=Coll([SimpleNamespace(Name="Pad.1")]), topology={"Edge": _cells("e", 16)}
    )
    assert search_final_topology(NoCGM(), body, "Edge", is_3dx=True) == 16


def test_unreadable_threading_mode_does_not_block_a_plain_hole():
    class OpaqueHole(ThreadHole):
        """ThreadingMode neither readable nor writable once created."""

        def __init__(self, name="Hole.1"):
            super().__init__(name)
            self._ready = True

        def _get_mode(self):
            raise AttributeError("ThreadingMode")

        def _set_mode(self, value):
            if self.__dict__.get("_ready"):
                raise AttributeError("ThreadingMode")

        ThreadingMode = property(_get_mode, _set_mode)

    rig = _HoleRig(hole_cls=OpaqueHole)
    assert "created" in rig.hole(diameter=6, depth=20)


def test_sketch_snapshot_evaluates_points_only():
    rig = _HoleRig()
    rig.hole(diameter=6, depth=20)
    evaluated = [(method, params[0]) for _, method, params in rig.app.SystemService.calls]
    assert ("GetCoordinates", rig.line) not in evaluated
    assert [m for m, _ in evaluated].count("GetCoordinates") == 1
