"""Measurement backend for CATIA V5 (SPAWorkbench) and 3DEXPERIENCE
(MeasurableService / InertiaService).

CATIA V5 (field-proven on V5R20): Document.GetWorkbench("SPAWorkbench")
.GetMeasurable(Reference). Volume and Area come back in MKS (m3, m2); the
array-filling methods (GetCOG, GetPoint, GetPlane) return coordinates in the
document length unit (mm) and need the SystemService.Evaluate detour.

3DEXPERIENCE: measurement moved to editor services. Field reports show
  Editor.GetService("MeasurableService").GetMeasurable(item, CATMeasurableType)
returning a typed MeasurableInContext (GetPoint x, y, z with scalar
out-parameters, in mm), and
  Editor.GetService("InertiaService").GetInertiaElement(item)
returning an Inertia object (GetMass, GetCOGPosition x, y, z,
GetInertiaMatrix array). CATMeasurableType values differ between releases,
so they are read from the installation's type library, with a bounded probe
as fallback. GetMeasurable returns whichever measurable type is asked for,
whatever the element (live-observed on R428), so the type is picked by its
enum name; trying codes is only the fallback when the enum is unreadable.

Minimum distance has no V5-style GetMinimumDistance there: it is
MeasurableBetween.DistanceMinTo(other, otherAxisSystem, oDistance, 6 point
coordinates), the axis system being 12 values (origin, 3 axes).

Units, live-checked on 3DEXPERIENCE R428 SP6 against a 100 x 60 x 40 mm
block: volume and area come back in MKS (converted like V5); coordinates and
DistanceMinTo come back in mm.

InertiaService GetCOGPosition: the R428 SP6 smoke run (smoke_report_before_
fixes.json) returned [0.0, 0.0, 0.02] for a part whose centre of gravity is
at z = 20 mm, i.e. metres. It is converted to mm by cross-checking against
the measurable COG when both exist (the scale that agrees within 1% wins),
else by assuming metres as observed.
"""

from __future__ import annotations

import inspect
from typing import Any

from catia_mcp.automation import (
    call_fill_array,
    call_out_values,
    evaluate,
    iter_items,
    name_exists,
    safe_get,
)
from catia_mcp.connection import CATIAConnection
from catia_mcp.errors import UnsupportedCapability
from catia_mcp.introspect import find_enum

M_TO_MM = 1000.0
M2_TO_MM2 = 1e6
M3_TO_MM3 = 1e9

UNIT_NOTE_3DX = (
    "3DEXPERIENCE: volume/area converted from MKS (m3/m2) as on V5; coordinates and "
    "distances are mm. Live-checked on R428 SP6 against a known block; other releases "
    "may differ."
)

# Member names that identify each kind of typed measurable on 3DEXPERIENCE.
# The key is also matched against the CATMeasurableType names
# (CAAMeasurableVolume, CAAMeasurableBetween, ... on R428).
_KIND_MEMBERS = {
    "volume": ("GetVolume", "Volume"),
    "surface": ("GetArea", "Area"),
    "point": ("GetPoint",),
    "plane": ("GetPlane",),
    "curve": ("GetLength", "Length"),
    "between": ("DistanceMinTo",),
}

# Axis system of an element measured in the part's own frame: origin, X, Y, Z.
_IDENTITY_AXIS = [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]

_VBS_INERTIA_V5 = (
    "Public Function catia_mcp_inertia(oBody)\n"
    "    Dim oSPA, oInertia, i\n"
    "    Dim m(8)\n"
    "    Dim a(9)\n"
    "    Set oSPA = CATIA.ActiveDocument.GetWorkbench(\"SPAWorkbench\")\n"
    "    Set oInertia = oSPA.Inertias.Add(oBody)\n"
    "    oInertia.GetInertiaMatrix m\n"
    "    a(0) = oInertia.Mass\n"
    "    For i = 0 To 8\n"
    "        a(i + 1) = m(i)\n"
    "    Next\n"
    "    catia_mcp_inertia = a\n"
    "End Function\n"
)


def significant(value: float, digits: int = 6) -> float:
    """Round to `digits` significant figures. Inertia moments in kg.m2 are
    small (1e-4 for a 100 mm steel block), so fixed decimals would erase them."""
    return float(f"{float(value):.{digits}g}")


# Candidate scales from the raw InertiaService COG to mm, most likely first
# (metres per the R428 SP6 smoke run, then already-mm).
_COG_SCALES = ((M_TO_MM, "m"), (1.0, "mm"))


def inertia_cog_to_mm(
    raw: list[float], reference_mm: list[float] | None = None
) -> tuple[list[float], str]:
    """Convert an InertiaService COG to mm. Returns (cog_mm, how the unit was
    decided). With a measurable COG in mm to compare against, the scale whose
    result agrees within 1% (of the COG distance from the origin; 1e-3 mm
    absolute floor) is used; otherwise metres are assumed (field evidence)."""
    values = [float(v) for v in raw]
    if reference_mm is not None and len(reference_mm) == 3:
        ref = [float(v) for v in reference_mm]
        norm = max(abs(v) for v in ref)
        tolerance = max(0.01 * norm, 1e-3)
        for scale, unit in _COG_SCALES:
            scaled = [v * scale for v in values]
            if all(abs(a - b) <= tolerance for a, b in zip(scaled, ref)):
                return scaled, f"raw {unit}; agrees with the measurable COG within 1%"
        return [v * M_TO_MM for v in values], (
            "raw assumed m (R428 SP6 field evidence); does NOT agree with the measurable "
            "COG in either m or mm, so treat it with care"
        )
    return [v * M_TO_MM for v in values], (
        "raw assumed m (R428 SP6 field evidence: 0.02 for a COG at 20 mm); no measurable "
        "COG to cross-check"
    )


class Measurer:
    """Platform-neutral measurements. Every value is returned in mm-based
    units (mm, mm2, mm3) or kg / kg.m2."""

    def __init__(self, connection: CATIAConnection) -> None:
        self.conn = connection
        self._codes: dict[str, int] = {}
        self._enum: dict[str, int] | None = None
        self._enum_read = False
        self.last_volume_source: str | None = None

    def reset_session_state(self) -> None:
        """Forget the CATMeasurableType codes/enum read from the previous
        CATIA session (call after a reconnect: a restarted or different
        CATIA may be another release with other enum values)."""
        self._codes = {}
        self._enum = None
        self._enum_read = False

    # ── V5 ──

    def spa_workbench(self) -> Any:
        """GetWorkbench is documented on Document, not Application, so try the
        active document first and keep the application call as fallback."""
        doc = self.conn.active_document
        try:
            return doc.GetWorkbench("SPAWorkbench")
        except AttributeError:
            return self.conn.app.GetWorkbench("SPAWorkbench")

    def v5_measurable(self, ref: Any) -> Any:
        """SPAWorkbench.GetMeasurable(ref), re-dispatched.

        On V5-6R2020 (B30), Measurable scalars obtained through pywin32
        late-bound dynamic dispatch (Volume, Area) read 0; re-wrapping the
        object with ``win32com.client.Dispatch`` binds its type information
        and yields real values (field-verified against exact primitive
        volumes). The re-wrap is a no-op where the dispatch already carries
        type info (e.g. V5R20).
        """
        measurable = self.spa_workbench().GetMeasurable(ref)
        try:
            import win32com.client

            return win32com.client.Dispatch(measurable)
        except Exception:
            return measurable

    # ── 3DEXPERIENCE typed measurables ──

    def _service(self) -> Any:
        return self.conn.editor_service("MeasurableService")

    def _candidate_codes(self, service: Any, kind: str) -> list[int]:
        if not self._enum_read:
            self._enum_read = True
            try:
                self._enum = find_enum([service, self.conn.app], "CATMeasurableType")
            except Exception:
                self._enum = None
        if self._enum:
            matching = [v for k, v in self._enum.items() if kind in k.lower()]
            if matching:
                return matching
            return sorted(set(self._enum.values()))
        return list(range(16))

    def measurable_3dx(self, target: Any, kind: str) -> Any:
        """GetMeasurable(target, code) for the first code whose measurable
        exposes the members of `kind`; the code is cached per kind."""
        service = self._service()
        members = _KIND_MEMBERS[kind]
        codes = self._candidate_codes(service, kind)
        if kind in self._codes:
            # The last working code first; others still apply to elements of
            # another geometry type (a distance from a face vs an edge).
            cached = self._codes[kind]
            codes = [cached] + [c for c in codes if c != cached]
        last_error: Exception | None = None
        for code in codes:
            try:
                measurable = service.GetMeasurable(target, code)
            except Exception as e:
                last_error = e
                continue
            if measurable is not None and any(name_exists(measurable, m) for m in members):
                self._codes[kind] = code
                return measurable
        raise UnsupportedCapability(
            f"MeasurableService.GetMeasurable gave no {kind} measurable for this element "
            f"(tried CATMeasurableType codes {codes}; last error: {last_error}). Check the "
            "element type, or inspect the API with catia_inspect_api "
            "path='editor.GetService(\"MeasurableService\")'."
        )

    def scalar(self, obj: Any, names: tuple[str, ...]) -> float:
        for name in names:
            try:
                value = getattr(obj, name)
            except AttributeError:
                continue
            if inspect.ismethod(value) or inspect.isfunction(value):
                try:
                    return float(value())
                except Exception:
                    # Method form with an out-parameter.
                    return float(call_out_values(self.conn.app, obj, name, 1)[0])
            return float(value)
        raise UnsupportedCapability(f"None of {', '.join(names)} is exposed by this object.")

    def _triple(self, obj: Any, method: str) -> list[float]:
        """x, y, z from either scalar out-parameters or a filled array."""
        app = self.conn.app
        try:
            values = call_out_values(app, obj, method, 3)
            return [float(v) for v in values]
        except Exception:
            return call_fill_array(app, obj, method, 3)

    def _targets(self, part: Any, obj: Any) -> list[Any]:
        """3DEXPERIENCE GetMeasurable takes an AnyObject: try the object, then
        a Reference to it."""
        targets = [obj]
        try:
            targets.append(part.CreateReferenceFromObject(obj))
        except Exception:
            pass
        return targets

    def _measurable_any_target(self, part: Any, obj: Any, kind: str) -> Any:
        error: Exception | None = None
        for target in self._targets(part, obj):
            try:
                return self.measurable_3dx(target, kind)
            except Exception as e:
                error = e
        raise error  # type: ignore[misc]

    # ── Public API ──

    def volume_mm3(self, part: Any, obj: Any) -> float:
        """Solid volume in mm3; `last_volume_source` then names the route."""
        value, self.last_volume_source = self.volume_with_source(part, obj)
        return value

    def _volume_targets(self, part: Any, obj: Any) -> list[tuple[str, Any]]:
        """Labelled 3DEXPERIENCE volume targets, in the order tried: the body
        object, a Reference to it, the body's last shape, a Reference to that.

        Field report 2026-11-03: GetVolume / MeasurableService failed on a
        body pasted into a part, so one target is not enough. Which of these
        works for such a body is UNVERIFIED live; the result names the one
        that did."""
        targets: list[tuple[str, Any]] = [("body object", obj)]
        try:
            targets.append(("reference to body", part.CreateReferenceFromObject(obj)))
        except Exception:
            pass
        last = None
        try:
            shapes = obj.Shapes
            if int(shapes.Count) > 0:
                last = shapes.Item(int(shapes.Count))
        except Exception:
            last = None
        if last is not None:
            targets.append((f"last shape '{safe_get(last, 'Name', '?')}'", last))
            try:
                targets.append(
                    (
                        f"reference to last shape '{safe_get(last, 'Name', '?')}'",
                        part.CreateReferenceFromObject(last),
                    )
                )
            except Exception:
                pass
        return targets

    def volume_with_source(self, part: Any, obj: Any) -> tuple[float, str]:
        """Solid volume in mm3 and which target/route gave it.

        3DEXPERIENCE: MeasurableService.GetMeasurable(target, code) (the
        recipe field-proven on R428 SP6 for faces, field report 2026-11-04:
        GetMeasurable(ref, 7) + GetPlane) on each of `_volume_targets` until
        one yields a volume. V5: SPAWorkbench measurable of a Reference to
        the object (field-proven on V5R20)."""
        if not self.conn.is_3dx:
            ref = part.CreateReferenceFromObject(obj)
            return self.v5_measurable(ref).Volume * M3_TO_MM3, "reference to body (SPAWorkbench)"
        errors: list[str] = []
        for label, target in self._volume_targets(part, obj):
            try:
                measurable = self.measurable_3dx(target, "volume")
                value = self.scalar(measurable, ("GetVolume", "Volume")) * M3_TO_MM3
            except Exception as e:
                errors.append(f"{label}: {e}")
                continue
            return value, f"MeasurableService on the {label}"
        raise UnsupportedCapability(
            "MeasurableService gave no volume for this body; tried " + "; ".join(errors)
        )

    def body_volume_mm3(self, part: Any, body: Any) -> float | None:
        """Measure a body's solid volume in mm3, or None if unavailable."""
        try:
            return self.volume_mm3(part, body)
        except Exception:
            return None

    def area_mm2(self, part: Any, obj: Any) -> float:
        if self.conn.is_3dx:
            measurable = self._measurable_any_target(part, obj, "volume")
            return self.scalar(measurable, ("GetArea", "Area")) * M2_TO_MM2
        ref = part.CreateReferenceFromObject(obj)
        return self.v5_measurable(ref).Area * M2_TO_MM2

    def cog_mm(self, part: Any, obj: Any) -> list[float]:
        if self.conn.is_3dx:
            measurable = self._measurable_any_target(part, obj, "volume")
            # R428 type information: MeasurableVolume.GetCOfG(oX, oY, oZ);
            # GetCOG (the V5 name) is kept for releases that expose it.
            method = "GetCOfG" if name_exists(measurable, "GetCOfG") else "GetCOG"
            return self._triple(measurable, method)
        ref = part.CreateReferenceFromObject(obj)
        return call_fill_array(self.conn.app, self.v5_measurable(ref), "GetCOG", 3)

    def point_mm(self, ref: Any) -> list[float]:
        """Coordinates of a vertex / point Reference, in mm."""
        if self.conn.is_3dx:
            return self._triple(self.measurable_3dx(ref, "point"), "GetPoint")
        return call_fill_array(self.conn.app, self.v5_measurable(ref), "GetPoint", 3)

    def plane(self, ref: Any) -> list[float]:
        """GetPlane components: origin (3), first axis (3), second axis (3)."""
        if self.conn.is_3dx:
            measurable = self.measurable_3dx(ref, "plane")
            try:
                return call_fill_array(self.conn.app, measurable, "GetPlane", 9)
            except Exception:
                return [float(v) for v in call_out_values(self.conn.app, measurable, "GetPlane", 9)]
        return call_fill_array(self.conn.app, self.v5_measurable(ref), "GetPlane", 9)

    def min_distance(self, ref1: Any, ref2: Any) -> dict[str, Any]:
        """Minimum distance in mm; on 3DEXPERIENCE also the closest point on
        each element (mm)."""
        if self.conn.is_3dx:
            app = self.conn.app
            measurable = self.measurable_3dx(ref1, "between")
            try:
                axis = call_fill_array(
                    app, self.measurable_3dx(ref2, "between"), "GetAxisSystemFromMeasurable", 12
                )
            except Exception:
                axis = list(_IDENTITY_AXIS)
            values = [
                float(v) for v in call_out_values(app, measurable, "DistanceMinTo", 7, ref2, axis)
            ]
            return {
                "distance_mm": values[0],
                "point1_mm": values[1:4],
                "point2_mm": values[4:7],
            }
        # Area/Volume are MKS (field-confirmed on V5); the minimum distance is
        # assumed MKS too and converted to mm. Not yet field-verified.
        distance = self.v5_measurable(ref1).GetMinimumDistance(ref2) * M_TO_MM
        return {"distance_mm": distance}

    def min_distance_mm(self, ref1: Any, ref2: Any) -> float:
        return self.min_distance(ref1, ref2)["distance_mm"]

    def inertia(self, part: Any, body: Any, whole_part_fallback: bool = True) -> dict[str, Any]:
        """Mass (kg, from the CATIA material) and inertia matrix (kg.m2).
        `target` names what was measured: "body", or on 3DEXPERIENCE "part" /
        "active object" when the body itself was refused (the whole part then,
        not that body; disable with whole_part_fallback=False)."""
        if not self.conn.is_3dx:
            raw = evaluate(self.conn.app, _VBS_INERTIA_V5, "catia_mcp_inertia", [body])
            values = [float(v) for v in raw]
            return {"mass_kg": values[0], "matrix": values[1:10], "target": "body"}
        service = self.conn.editor_service("InertiaService")
        element = None
        error: Exception | None = None
        candidates: list[tuple[str, Any]] = [("body", body)]
        if whole_part_fallback:
            candidates.append(("part", part))
            try:
                candidates.append(("active object", self.conn.active_object()))
            except Exception:
                pass
        used = ""
        for label, target in candidates:
            try:
                element = service.GetInertiaElement(target)
                if element is not None:
                    used = label
                    break
            except Exception as e:
                error = e
        if element is None:
            raise UnsupportedCapability(f"InertiaService.GetInertiaElement failed: {error}")
        result: dict[str, Any] = {
            "mass_kg": self.scalar(element, ("GetMass", "Mass")),
            "target": used,
        }
        try:
            result["matrix"] = call_fill_array(self.conn.app, element, "GetInertiaMatrix", 9)
        except Exception as e:
            result["matrix_error"] = str(e)
        try:
            # Raw: metres on R428 SP6 (see the module docstring). The caller
            # converts with inertia_cog_to_mm once it has a measurable COG to
            # cross-check against.
            result["cog_raw"] = self._triple(element, "GetCOGPosition")
        except Exception:
            pass
        return result


# ── Bodies ──


def body_has_solid(body: Any) -> bool:
    """True when the body holds at least one solid feature (Shapes)."""
    try:
        return int(body.Shapes.Count) > 0
    except Exception:
        return False


def bodies_with_solid(part: Any) -> list[Any]:
    """Top-level bodies of `part` holding solid features, in tree order."""
    try:
        return [b for b in iter_items(part.Bodies) if body_has_solid(b)]
    except Exception:
        return []


def empty_body_hint(part: Any, body: Any) -> str | None:
    """When `body` is empty but other bodies hold geometry, a sentence saying
    so (field report 2026-11-03: geometry pasted into a part landed in
    'Body.2' beside an empty main body, and inertia / bounding box read the
    empty one). None otherwise."""
    if body_has_solid(body):
        return None
    name = safe_get(body, "Name", "?")
    others = [str(safe_get(b, "Name", "?")) for b in bodies_with_solid(part)]
    others = [o for o in others if o != str(name)]
    if not others:
        return None
    return (
        f"Body '{name}' is empty (no solid features); the geometry is in "
        f"{', '.join(repr(o) for o in others)}. Pass body_name={others[0]!r}, or "
        "body_name='all' to measure every body with solid content."
    )


BOX_VERTEX_CAP = 200


def body_vertex_box(measurer: Measurer, sel: Any, body: Any, *, is_3dx: bool) -> dict[str, Any]:
    """Axis-aligned box of a body's final shape from a vertex sweep, in mm.

    Exact for planar-faced solids; curved faces can extend past their
    vertices. Raises UnsupportedCapability when the shape has no vertices.
    The caller clears nothing: `sel` is cleared here."""
    from catia_mcp.references import search_final_topology

    refs = []
    try:
        count = search_final_topology(sel, body, "Vertex", is_3dx=is_3dx)
        if count == 0:
            raise UnsupportedCapability(
                f"The final shape of body '{safe_get(body, 'Name', '?')}' exposes no "
                "vertices (fully curved solid); a bounding box cannot be derived "
                "through the automation API."
            )
        truncated = count > BOX_VERTEX_CAP
        for i in range(1, min(count, BOX_VERTEX_CAP) + 1):
            refs.append(sel.Item(i).Reference)
    finally:
        try:
            sel.Clear()
        except Exception:
            pass
    points = [measurer.point_mm(ref) for ref in refs]
    xs, ys, zs = ([p[i] for p in points] for i in range(3))
    return {
        "min": [min(xs), min(ys), min(zs)],
        "max": [max(xs), max(ys), max(zs)],
        "vertices": len(refs),
        "truncated": truncated,
    }


def box_dims(box: dict[str, Any]) -> list[float]:
    return [box["max"][i] - box["min"][i] for i in range(3)]
