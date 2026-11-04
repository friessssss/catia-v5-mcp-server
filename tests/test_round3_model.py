"""Offline tests for the modeling / measurement / screenshot fixes from the
R428 SP6 field reports 2026-11-03 and 2026-11-04 (kept outside the
repository).

Covered: inertia and bounding box on a named body and on all bodies, the
empty-default-body hint, the 3DEXPERIENCE volume target fallback,
put_property_raw (DISPATCH_PROPERTYPUT), catia_scale_body (AddNewAffinity2
+ raw AxisOrigin + bounding-box verification), and catia_screenshot's
refresh, identical-bytes retry and 2D origin/zoom. In-memory doubles only:
these prove orchestration, not live CATIA behaviour.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

import catia_mcp.tools.export as export_module
from catia_mcp.automation import DISPATCH_PROPERTYPUT, put_property_raw
from catia_mcp.connection import PLATFORM_3DX, CATIAConnection
from catia_mcp.errors import CATIAToolError, InvalidArgument, ObjectNotFound, UnsupportedCapability
from catia_mcp.tools.export import ExportTools
from catia_mcp.tools.measurement import MeasurementTools
from catia_mcp.tools.part_design import PartDesignTools
from tests.fakes import App3DX, Coll, make_part

# ── doubles ──


def connected(app) -> CATIAConnection:
    conn = CATIAConnection(platform=PLATFORM_3DX)
    conn.app = app
    conn.platform = PLATFORM_3DX
    return conn


def _box_vertices(lo, hi):
    return [
        SimpleNamespace(Reference=("vertex", x, y, z))
        for x in (lo[0], hi[0])
        for y in (lo[1], hi[1])
        for z in (lo[2], hi[2])
    ]


class Body:
    def __init__(self, name, volume_mm3=0.0, lo=(0, 0, 0), hi=(0, 0, 0), shapes=()):
        self.Name = name
        self.volume_mm3 = volume_mm3
        self.Shapes = Coll([SimpleNamespace(Name=s) for s in shapes])
        self.Sketches = Coll()
        self.lo, self.hi = list(lo), list(hi)
        self.topology = {"Vertex": _box_vertices(self.lo, self.hi)} if shapes else {}

    def rebox(self, lo, hi):
        self.lo, self.hi = list(lo), list(hi)
        self.topology = {"Vertex": _box_vertices(self.lo, self.hi)}


class Measurable:
    def __init__(self, body):
        self.body = body
        self.GetVolume = lambda: body.volume_mm3 / 1e9
        self.GetArea = lambda: 0.01
        self.GetCOfG = None

    def _outs_GetCOfG(self):
        return [(a + b) / 2 for a, b in zip(self.body.lo, self.body.hi)]


class MeasurableService:
    """Volumes for bodies reachable as `target` (a body, or a Reference
    tuple ("ref", body)); `refuse_raw` refuses the raw body object like the
    pasted body of field report 2026-11-03."""

    def __init__(self, refuse_raw=False):
        self.refuse_raw = refuse_raw
        self.targets = []

    def GetMeasurable(self, target, code):
        self.targets.append(target)
        if isinstance(target, tuple) and target[0] == "vertex":
            x, y, z = target[1:]
            return SimpleNamespace(GetPoint=None, _outs_GetPoint=lambda: [x, y, z])
        body = target[1] if isinstance(target, tuple) and target[0] == "ref" else target
        if not isinstance(body, Body) or (self.refuse_raw and body is target):
            raise RuntimeError("The method GetMeasurable failed")
        return Measurable(body)


class HybridBodies(Coll):
    def Add(self):
        geoset = SimpleNamespace(Name="Geometrical Set.1", HybridShapes=Coll(), HybridBodies=Coll())
        geoset.AppendHybridShape = geoset.HybridShapes.append
        self.append(geoset)
        return geoset


def make_part_with(bodies, *, refuse_raw=False):
    part = make_part()
    part.MainBody = bodies[0]
    part.Bodies = Coll(bodies)
    part.HybridBodies = HybridBodies()
    part.CreateReferenceFromObject = lambda obj: ("ref", obj)
    part.updated = []
    part.UpdateObject = part.updated.append
    app = App3DX()
    service = MeasurableService(refuse_raw=refuse_raw)
    app.open_editor(part, "Shape", {"MeasurableService": service})
    return part, app, service


# ── measurement ──


def test_inertia_on_named_body_reports_volume_and_source():
    main = Body("PartBody")
    body2 = Body("Body.2", 2000.0, (0, 0, 0), (20, 10, 10), shapes=("Pad.1",))
    _part, app, _service = make_part_with([main, body2], refuse_raw=True)
    tools = MeasurementTools(connected(app))
    result = json.loads(tools.execute("catia_get_inertia", {"body_name": "Body.2", "density": 1000}))
    assert result["body"] == "Body.2"
    assert result["volume_mm3"] == 2000.0
    # The raw body was refused; the Reference to it worked.
    assert result["volume_source"] == "MeasurableService on the reference to body"
    assert result["mass_kg"] == pytest.approx(0.002)


def test_volume_falls_back_to_the_last_shape():
    body = Body("Body.2", 500.0, shapes=("Pad.1", "Pocket.1"))
    part = SimpleNamespace(CreateReferenceFromObject=lambda obj: ("ref", obj))
    app = App3DX()
    last = body.Shapes.Item(2)

    class Service(MeasurableService):
        def GetMeasurable(self, target, code):
            if target is last:
                return Measurable(body)
            raise RuntimeError("no")

    app.open_editor(part, "Shape", {"MeasurableService": Service()})
    tools = MeasurementTools(connected(app))
    value, source = tools.measure.volume_with_source(part, body)
    assert value == 500.0
    assert source == "MeasurableService on the last shape 'Pocket.1'"


def test_empty_default_body_names_the_bodies_with_geometry_instead_of_zeros():
    main = Body("PartBody")
    body2 = Body("Body.2", 2000.0, (0, 0, 0), (20, 10, 10), shapes=("Pad.1",))
    _part, app, _ = make_part_with([main, body2])
    tools = MeasurementTools(connected(app))
    result = json.loads(tools.execute("catia_get_inertia", {}))
    assert result["empty_body"] is True
    assert "Body.2" in result["hint"] and "'all'" in result["hint"]
    assert "volume_mm3" not in result
    with pytest.raises(ObjectNotFound, match="Body.2"):
        tools.execute("catia_get_bounding_box", {})


def test_inertia_all_bodies_sums_volume_and_mass():
    main = Body("PartBody", 1000.0, (0, 0, 0), (10, 10, 10), shapes=("Pad.1",))
    empty = Body("Body.3")
    body2 = Body("Body.2", 3000.0, (10, 0, 0), (40, 10, 10), shapes=("Pad.2",))
    _part, app, _ = make_part_with([main, empty, body2])
    tools = MeasurementTools(connected(app))
    result = json.loads(tools.execute("catia_get_inertia", {"body_name": "all", "density": 2000}))
    assert [b["body"] for b in result["bodies"]] == ["PartBody", "Body.2"]
    total = result["total"]
    assert total["body_count"] == 2
    assert total["volume_mm3"] == 4000.0
    assert total["mass_kg"] == pytest.approx(0.008)
    # Volume-weighted COG: (5 * 1000 + 25 * 3000) / 4000 = 20.
    assert total["center_of_gravity_mm"]["x"] == 20.0


def test_bounding_box_named_and_all_bodies():
    main = Body("PartBody", 1000.0, (0, 0, 0), (10, 10, 10), shapes=("Pad.1",))
    body2 = Body("Body.2", 3000.0, (10, -5, 0), (40, 10, 12), shapes=("Pad.2",))
    _part, app, _ = make_part_with([main, body2])
    tools = MeasurementTools(connected(app))
    named = json.loads(tools.execute("catia_get_bounding_box", {"body_name": "Body.2"}))
    assert named["body"] == "Body.2"
    assert named["dimensions"] == {"length_x": 30.0, "length_y": 15.0, "length_z": 12.0}
    every = json.loads(tools.execute("catia_get_bounding_box", {"body_name": "all"}))
    assert every["dimensions"] == {"length_x": 40.0, "length_y": 15.0, "length_z": 12.0}
    assert [b["body"] for b in every["bodies"]] == ["PartBody", "Body.2"]
    with pytest.raises(ObjectNotFound):
        tools.execute("catia_get_bounding_box", {"body_name": "Nope"})


def test_measurement_schemas_are_valid_and_read_only():
    tools = MeasurementTools(connected(App3DX()))
    for definition in tools.get_tool_definitions():
        Draft202012Validator.check_schema(definition["inputSchema"])
        if definition["name"] in ("catia_get_inertia", "catia_get_bounding_box"):
            assert definition["annotations"]["readOnlyHint"] is True
            assert "body_name" in definition["inputSchema"]["properties"]


# ── put_property_raw ──


class FakeOle:
    def __init__(self, dispid=17):
        self.dispid = dispid
        self.calls = []

    def GetIDsOfNames(self, name):
        self.calls.append(("GetIDsOfNames", name))
        return self.dispid

    def Invoke(self, *args):
        self.calls.append(("Invoke", *args))


def test_put_property_raw_invokes_propertyput_with_the_value_dispatch():
    target = SimpleNamespace(_oleobj_=FakeOle())
    value_ole = object()
    put_property_raw(target, "AxisOrigin", SimpleNamespace(_oleobj_=value_ole))
    assert DISPATCH_PROPERTYPUT == 4
    assert target._oleobj_.calls == [
        ("GetIDsOfNames", "AxisOrigin"),
        ("Invoke", 17, 0, 4, False, value_ole),
    ]
    scalar = SimpleNamespace(_oleobj_=FakeOle(dispid=(5,)))
    put_property_raw(scalar, "Zoom", 2.5)
    assert scalar._oleobj_.calls[-1] == ("Invoke", 5, 0, 4, False, 2.5)
    with pytest.raises(ValueError):
        put_property_raw(target, "Bad Name", 1)
    with pytest.raises(TypeError):
        put_property_raw(object(), "AxisOrigin", 1)


# ── catia_scale_body ──


class Affinity:
    def __init__(self, ratios):
        self.Name = "Affinity.1"
        self._oleobj_ = FakeOle(dispid=42)
        self.XRatio = SimpleNamespace(Value=ratios[0])
        self.YRatio = SimpleNamespace(Value=ratios[1])
        self.ZRatio = SimpleNamespace(Value=ratios[2])
        self.ratios = ratios


def _scaling_part(scales=True):
    main = Body("PartBody")
    layer = Body("Body.2", 1000.0, (0, 0, 0), (100, 60, 20.0), shapes=("Pad.1",))
    part, app, _ = make_part_with([main, layer])
    created = []

    def add_affinity(rx, ry, rz):
        aff = Affinity([rx, ry, rz])
        created.append(aff)
        return aff

    part.ShapeFactory = SimpleNamespace(AddNewAffinity2=add_affinity)
    points = []

    def add_point(x, y, z):
        point = SimpleNamespace(Name=f"Point.{len(points) + 1}", coords=(x, y, z))
        points.append(point)
        return point

    part.HybridShapeFactory = SimpleNamespace(AddNewPointCoord=add_point)
    point_refs = {}

    def reference(obj):
        if obj in points:
            ref = SimpleNamespace(_oleobj_=f"ole-{obj.Name}")
            point_refs[obj.Name] = ref
            return ref
        return ("ref", obj)

    part.CreateReferenceFromObject = reference

    def update(obj):
        part.updated.append(obj)
        if isinstance(obj, Affinity) and scales:
            origin = points[0].coords if points else (0, 0, 0)
            r = obj.ratios
            layer.rebox(
                [origin[i] + (layer.lo[i] - origin[i]) * r[i] for i in range(3)],
                [origin[i] + (layer.hi[i] - origin[i]) * r[i] for i in range(3)],
            )

    part.UpdateObject = update
    return part, app, layer, created, points


def test_scale_body_uses_affinity2_and_raw_axis_origin():
    part, app, layer, created, points = _scaling_part()
    tools = PartDesignTools(connected(app))
    ratio = 20.05 / 20.0
    out = tools.execute("catia_scale_body", {"body_name": "Body.2", "axis": "z", "ratio": ratio})
    (affinity,) = created
    assert affinity.ratios == [1.0, 1.0, ratio]
    assert part.InWorkObject is layer
    # Origin point created at (0, 0, 0) and set through DISPATCH_PROPERTYPUT.
    assert points[0].coords == (0.0, 0.0, 0.0)
    assert affinity._oleobj_.calls == [
        ("GetIDsOfNames", "AxisOrigin"),
        ("Invoke", 42, 0, 4, False, "ole-Point.1"),
    ]
    assert "Verified by bounding box" in out
    assert "20.0500" in out


def test_scale_body_with_coordinate_origin_and_ratios():
    _part, app, _layer, _created, points = _scaling_part()
    tools = PartDesignTools(connected(app))
    out = tools.execute(
        "catia_scale_body",
        {"body_name": "Body.2", "ratios": [1.0, 2.0, 1.0], "origin": [0, 30, 0]},
    )
    assert points[0].coords == (0.0, 30.0, 0.0)
    assert "100.0000 x 120.0000 x 20.0000" in out


def test_scale_body_reports_a_failed_bounding_box_check():
    _part, app, _layer, _created, _points = _scaling_part(scales=False)
    tools = PartDesignTools(connected(app))
    with pytest.raises(CATIAToolError, match="Bounding box check FAILED on z") as info:
        tools.execute("catia_scale_body", {"body_name": "Body.2", "axis": "z", "ratio": 1.1})
    assert info.value.modified is True


def test_scale_body_validates_before_changing_anything():
    _part, app, _layer, created, points = _scaling_part()
    tools = PartDesignTools(connected(app))
    for args in (
        {"body_name": "Body.2"},
        {"body_name": "Body.2", "axis": "z", "ratio": 0},
        {"body_name": "Body.2", "axis": "z", "ratio": 1.0},
        {"body_name": "Body.2", "axis": "z", "ratio": 2, "ratios": [1, 1, 2]},
        {"body_name": "Body.2", "axis": "z", "ratio": 2, "origin": [1, 2]},
        {"axis": "z", "ratio": 2},  # default body is empty
    ):
        with pytest.raises(InvalidArgument):
            tools.execute("catia_scale_body", args)
    assert created == [] and points == []


def test_thickness_and_scale_descriptions_carry_the_field_advice():
    defs = {d["name"]: d for d in PartDesignTools(connected(App3DX())).get_tool_definitions()}
    assert "tangent" in defs["catia_thickness"]["description"]
    assert "catia_scale_body" in defs["catia_thickness"]["description"]
    Draft202012Validator.check_schema(defs["catia_scale_body"]["inputSchema"])


# ── catia_screenshot ──


class Viewer:
    """CaptureToFile writes the next payload (the last one repeats)."""

    def __init__(self, payloads, two_d=False):
        self.payloads = list(payloads)
        self.captures = 0
        self.updates = 0
        self.reframes = 0
        self.mtime = 1_000_000_000_000
        if two_d:
            self.Viewpoint2D = SimpleNamespace(origins=[], Zoom=1.0)
            self.Viewpoint2D.PutOrigin = self.Viewpoint2D.origins.append

    def CaptureToFile(self, fmt, path):
        data = self.payloads[min(self.captures, len(self.payloads) - 1)]
        self.captures += 1
        with open(path, "wb") as f:
            f.write(data)
        self.mtime += 10**9
        os.utime(path, ns=(self.mtime, self.mtime))

    def Update(self):
        self.updates += 1

    def Reframe(self):
        self.reframes += 1


class RefreshApp(App3DX):
    def __init__(self):
        super().__init__()
        self.refresh_log = []

    @property
    def RefreshDisplay(self):
        return self.refresh_log[-1] if self.refresh_log else True

    @RefreshDisplay.setter
    def RefreshDisplay(self, value):
        self.refresh_log.append(value)


def _shot_tools(monkeypatch, viewer, app=None):
    monkeypatch.setattr(export_module.time, "sleep", lambda s: None)
    app = app or App3DX()
    app.ActiveWindow = SimpleNamespace(ActiveViewer=viewer)
    return ExportTools(connected(app)), app


def _info(content):
    return json.loads(content[-1].text)


def test_screenshot_refreshes_before_capture(monkeypatch, tmp_path):
    viewer = Viewer([b"A"])
    tools, app = _shot_tools(monkeypatch, viewer, RefreshApp())
    info = _info(tools.execute(
        "catia_screenshot", {"file_path": str(tmp_path / "a.jpg"), "return_image": False}
    ))
    assert viewer.updates == 1 and viewer.reframes == 1
    assert app.refresh_log == [False, True]
    assert info["refresh"] == ["Viewer.Update", "RefreshDisplay toggled"]
    assert "possibly_stale" not in info


def test_identical_capture_with_fit_false_is_retried_then_flagged(monkeypatch, tmp_path):
    viewer = Viewer([b"same"])
    tools, _ = _shot_tools(monkeypatch, viewer)
    args = {"file_path": str(tmp_path / "s.jpg"), "return_image": False, "fit": False}
    first = _info(tools.execute("catia_screenshot", args))
    assert "possibly_stale" not in first and viewer.captures == 1
    second = _info(tools.execute("catia_screenshot", args))
    assert viewer.captures == 3  # captured, refreshed, recaptured once
    assert second["recaptured"] is True and second["possibly_stale"] is True
    assert any("byte-identical" in w for w in second["warnings"])


def test_identical_capture_cured_by_the_retry_is_not_flagged(monkeypatch, tmp_path):
    viewer = Viewer([b"old", b"old", b"new"])
    tools, _ = _shot_tools(monkeypatch, viewer)
    args = {"file_path": str(tmp_path / "s.jpg"), "return_image": False, "fit": False}
    tools.execute("catia_screenshot", args)
    info = _info(tools.execute("catia_screenshot", args))
    assert info["recaptured"] is True and "possibly_stale" not in info
    assert (tmp_path / "s.jpg").read_bytes() == b"new"


def test_identical_capture_with_fit_true_is_not_retried(monkeypatch, tmp_path):
    viewer = Viewer([b"same"])
    tools, _ = _shot_tools(monkeypatch, viewer)
    args = {"file_path": str(tmp_path / "s.jpg"), "return_image": False}
    tools.execute("catia_screenshot", args)
    info = _info(tools.execute("catia_screenshot", args))
    assert viewer.captures == 2 and "recaptured" not in info


def test_screenshot_2d_origin_and_zoom(monkeypatch, tmp_path):
    viewer = Viewer([b"D"], two_d=True)
    tools, _ = _shot_tools(monkeypatch, viewer)
    info = _info(tools.execute(
        "catia_screenshot",
        {"file_path": str(tmp_path / "d.jpg"), "return_image": False,
         "origin": [210, 148.5], "zoom": 3},
    ))
    assert viewer.Viewpoint2D.origins == [[210.0, 148.5]]
    assert viewer.Viewpoint2D.Zoom == 3.0
    assert info["viewpoint_2d"] == {"origin": [210.0, 148.5], "zoom": 3.0}
    assert viewer.reframes == 0  # fit defaults to false with origin/zoom


def test_screenshot_2d_arguments_are_validated(monkeypatch, tmp_path):
    tools, _ = _shot_tools(monkeypatch, Viewer([b"D"], two_d=True))
    base = {"file_path": str(tmp_path / "d.jpg"), "return_image": False}
    for bad in ({"zoom": 0}, {"origin": [1]}, {"zoom": 2, "fit": True}, {"zoom": 2, "view": "top"}):
        with pytest.raises(InvalidArgument):
            tools.execute("catia_screenshot", {**base, **bad})
    tools3d, _ = _shot_tools(monkeypatch, Viewer([b"D"]))
    with pytest.raises(UnsupportedCapability):
        tools3d.execute("catia_screenshot", {**base, "zoom": 2})
    schema = next(
        d for d in tools.get_tool_definitions() if d["name"] == "catia_screenshot"
    )["inputSchema"]
    Draft202012Validator.check_schema(schema)


# ── review fixes ──


def test_all_bodies_skips_boolean_operands_to_avoid_double_counting():
    main = Body("PartBody", 4000.0, (0, 0, 0), (40, 10, 10), shapes=("Pad.1", "Add.1"))
    operand = Body("Body.2", 3000.0, (10, 0, 0), (40, 10, 10), shapes=("Pad.2",))
    operand.InBooleanOperation = True
    _part, app, _ = make_part_with([main, operand])
    tools = MeasurementTools(connected(app))
    result = json.loads(tools.execute("catia_get_inertia", {"body_name": "all"}))
    assert result["total"]["volume_mm3"] == 4000.0
    assert result["skipped_boolean_operands"] == ["Body.2"]
    box = json.loads(tools.execute("catia_get_bounding_box", {"body_name": " ALL "}))
    assert box["skipped_boolean_operands"] == ["Body.2"]


def test_scale_body_with_capped_vertex_sweep_is_not_verified_not_failed(monkeypatch):
    import catia_mcp.measure as measure_module

    monkeypatch.setattr(measure_module, "BOX_VERTEX_CAP", 4)
    _part, app, _layer, created, _points = _scaling_part(scales=False)
    tools = PartDesignTools(connected(app))
    out = tools.execute("catia_scale_body", {"body_name": "Body.2", "axis": "z", "ratio": 1.1})
    assert created and "NOT verified" in out


def test_refresh_display_is_re_enabled_when_the_toggle_fails(monkeypatch, tmp_path):
    class HalfApp(RefreshApp):
        @property
        def RefreshDisplay(self):
            return True

        @RefreshDisplay.setter
        def RefreshDisplay(self, value):
            self.refresh_log.append(value)
            if value is False:
                raise RuntimeError("refused")

    viewer = Viewer([b"A"])
    tools, app = _shot_tools(monkeypatch, viewer, HalfApp())
    info = _info(tools.execute(
        "catia_screenshot", {"file_path": str(tmp_path / "a.jpg"), "return_image": False}
    ))
    assert app.refresh_log == [False, True]
    assert info["refresh"] == ["Viewer.Update"]
