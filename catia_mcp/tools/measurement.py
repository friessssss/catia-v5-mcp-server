"""Measurement and analysis tools for CATIA V5 and CATIA 3DEXPERIENCE.

Distance, inertia, bounding box, element measures, and part parameters.
Platform specifics (V5 SPAWorkbench vs 3DEXPERIENCE MeasurableService /
InertiaService) live in catia_mcp.measure.Measurer.
"""

from __future__ import annotations

import json
import re
from typing import Any

from catia_mcp.automation import call_fill_array, com_name, safe_get
from catia_mcp.connection import CATIAConnection
from catia_mcp.errors import InvalidArgument, ObjectNotFound
from catia_mcp.measure import (
    M2_TO_MM2,
    M3_TO_MM3,
    M_TO_MM,
    UNIT_NOTE_3DX,
    Measurer,
    bodies_with_solid,
    body_has_solid,
    body_vertex_box,
    box_dims,
    empty_body_hint,
    inertia_cog_to_mm,
    significant,
)
from catia_mcp.references import body_by_name, search_final_topology

_ALL_BODIES = "all"


def _is_all(body_name: Any) -> bool:
    return isinstance(body_name, str) and body_name.strip().lower() == _ALL_BODIES

_BODY_NAME_SCHEMA = {
    "type": "string",
    "description": (
        "Body to measure (e.g. 'Body.2'). Default: the active body (catia_set_active_body, "
        "else the main body). 'all' = every body with solid content."
    ),
}

_READ_ONLY = {"readOnlyHint": True, "openWorldHint": False}

# CATIA V5's SPAWorkbench Measurable API returns every value in MKS units
# (meters, m2, m3, kg.m2) regardless of the document's display units.
# Feature creation APIs, by contrast, take millimeters. All measurement
# results are therefore converted to mm-based units here, at the server
# boundary, so agents always see consistent mm/mm2/mm3 values.
_M_TO_MM = M_TO_MM
_M2_TO_MM2 = M2_TO_MM2
_M3_TO_MM3 = M3_TO_MM3


def read_measurable_array(app: Any, measurable: Any, method: str, count: int) -> list[float]:
    """Run `measurable.<method> a` inside CATIA via SystemService.Evaluate and
    return the filled array (field-proven on V5R20 for GetCOG/GetPlane).

    Coordinates returned this way (GetCOG, GetPoint, GetPlane) are observed in
    the document's length unit (mm), unlike Area/Volume which are MKS.
    """
    return call_fill_array(app, measurable, method, count)


class MeasurementTools:
    """Tools for measurement and analysis in CATIA V5 and 3DEXPERIENCE."""

    def __init__(self, connection: CATIAConnection) -> None:
        self.conn = connection
        self.measure = Measurer(connection)
        self._skipped_operands: list[str] = []

    def reset_session_state(self) -> None:
        """Drop per-session caches (CATMeasurableType codes / enum). The
        server calls this after CATIA reconnects."""
        self.measure.reset_session_state()

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "catia_measure_distance",
                "description": (
                    "Measure the minimum distance between two geometry elements. "
                    "Returns distance in mm. Elements resolve by tree name "
                    "(features, sketches, e.g. 'Pad.1', 'Sketch.2') or by "
                    "indexed topology ('Face.N' / 'Edge.N' as enumerated by "
                    "catia_list_faces / catia_list_edges on the final solid)."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "element1": {
                            "type": "string",
                            "description": "Name of first element (feature, face, edge, point)",
                        },
                        "element2": {
                            "type": "string",
                            "description": "Name of second element",
                        },
                    },
                    "required": ["element1", "element2"],
                },
            },
            {
                "name": "catia_get_inertia",
                "description": (
                    "Get inertia properties of a body of the active part (default: the "
                    "active body; body_name picks another, 'all' measures every body "
                    "with solid content and sums volume and mass): volume (mm3 and cm3), "
                    "surface area (mm2 and cm2), center of gravity (mm), mass in kg "
                    "(if a density is passed), inertia matrix (kg.m2, from the material "
                    "density defined in CATIA). All values are converted server-side "
                    "from CATIA's internal MKS measurement units. An empty body is "
                    "reported as empty (naming the bodies that hold geometry), not as "
                    "zeros."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "density": {
                            "type": "number",
                            "description": "Material density in kg/m3 (optional, for mass calculation)",
                        },
                        "body_name": _BODY_NAME_SCHEMA,
                    },
                },
                "annotations": _READ_ONLY,
            },
            {
                "name": "catia_get_bounding_box",
                "description": (
                    "Get the bounding box of the active part from a vertex "
                    "sweep of the final shape, in mm. Exact for planar-faced "
                    "solids; curved faces may extend beyond the reported "
                    "bounds (stated in the result). Default: the active body; "
                    "body_name picks another, 'all' boxes every body with solid "
                    "content (per body and combined)."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {"body_name": _BODY_NAME_SCHEMA},
                },
                "annotations": _READ_ONLY,
            },
            {
                "name": "catia_get_parameters",
                "description": (
                    "List all user-defined and computed parameters of the active part. "
                    "Includes dimensions, formulas, and design tables."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "filter": {
                            "type": "string",
                            "description": "Optional name filter (partial match)",
                        },
                    },
                },
            },
            {
                "name": "catia_set_parameter",
                "description": (
                    "Set the value of a named parameter in the active part. "
                    "Useful for parametric design modifications."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Full parameter name (e.g., 'Part1\\\\Pad.1\\\\FirstLimit\\\\Length')",
                        },
                        "value": {
                            "type": "number",
                            "description": "New value for the parameter",
                        },
                    },
                    "required": ["name", "value"],
                },
            },
            {
                "name": "catia_update_part",
                "description": "Force update/rebuild of the active part. Recalculates all features.",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "catia_measure_element",
                "description": (
                    "Measure one element: volume, area, length, radius, center of "
                    "gravity or point coordinates, whichever apply. Elements resolve by "
                    "tree name ('Pad.1', 'Sketch.2', GSD names) or indexed topology "
                    "('Face.N' / 'Edge.N' / 'Vertex.N'). Values in mm, mm2, mm3."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "element": {"type": "string", "description": "Element to measure"},
                    },
                    "required": ["element"],
                },
            },
        ]

    def execute(self, tool_name: str, arguments: dict[str, Any]) -> str:
        match tool_name:
            case "catia_measure_distance":
                return self._measure_distance(arguments["element1"], arguments["element2"])
            case "catia_get_inertia":
                return self._get_inertia(arguments.get("density"), arguments.get("body_name"))
            case "catia_get_bounding_box":
                return self._get_bounding_box(arguments.get("body_name"))
            case "catia_get_parameters":
                return self._get_parameters(arguments.get("filter"))
            case "catia_set_parameter":
                return self._set_parameter(arguments["name"], arguments["value"])
            case "catia_update_part":
                return self._update_part()
            case "catia_measure_element":
                return self._measure_element(arguments["element"])
            case _:
                raise ValueError(f"Unknown measurement tool: {tool_name}")

    def _resolve_reference(self, part: Any, name: str) -> Any:
        """Resolve an element name to a CATIA Reference.

        Tree-named objects (Pad.1, Sketch.2) resolve through a Name= search.
        Topology uses the indexed form Face.N / Edge.N / Vertex.N over the
        body's final shape, matching the indices listed by catia_list_faces
        and catia_list_edges. Topology references come from the selection's
        Reference property: CreateReferenceFromObject rejects HSO-resolved
        topology cells (field-verified E_INVALIDARG on V5R20).
        """
        sel = self.conn.hso
        topo = re.match(r"^(Face|Edge|Vertex)\.(\d+)$", name)
        try:
            if topo:
                kind, idx = topo.group(1), int(topo.group(2))
                body = self.conn.get_active_part_body()
                if body.Shapes.Count == 0:
                    raise RuntimeError(
                        f"Cannot resolve '{name}': the active body has no "
                        "solid shape to enumerate topology from."
                    )
                count = search_final_topology(sel, body, kind, is_3dx=self.conn.is_3dx)
                if idx < 1 or idx > count:
                    raise RuntimeError(
                        f"'{name}' is out of range: the final shape exposes "
                        f"{count} {kind.lower()}(s). Use catia_list_faces / "
                        "catia_list_edges to enumerate valid indices."
                    )
                return sel.Item(idx).Reference

            sel.Clear()
            sel.Search(f"Name={name},all")
            if sel.Count == 0:
                raise RuntimeError(
                    f"Element '{name}' not found by tree name. Use feature "
                    "or sketch names (e.g. 'Pad.1'), or the indexed topology "
                    "form Face.N / Edge.N from catia_list_faces / "
                    "catia_list_edges."
                )
            obj = sel.Item(1).Value
            return part.CreateReferenceFromObject(obj)
        finally:
            try:
                sel.Clear()
            except Exception:
                pass

    def _measure_distance(self, elem1_name: str, elem2_name: str) -> str:
        self.conn.ensure_connected()
        part = self.conn.get_active_part()

        ref1 = self._resolve_reference(part, elem1_name)
        ref2 = self._resolve_reference(part, elem2_name)

        measured = self.measure.min_distance(ref1, ref2)
        points = ""
        if "point1_mm" in measured:
            p1 = ", ".join(f"{v:g}" for v in measured["point1_mm"])
            p2 = ", ".join(f"{v:g}" for v in measured["point2_mm"])
            points = f"; closest points ({p1}) and ({p2}) mm"
        note = f" ({UNIT_NOTE_3DX})" if self.conn.is_3dx else ""
        return (
            f"Minimum distance between '{elem1_name}' and '{elem2_name}': "
            f"{measured['distance_mm']:.4f} mm{points}{note}"
        )

    def _bodies(self, part: Any, body_name: str | None) -> tuple[list[Any], str | None]:
        """Bodies to measure and, for the default single body, an empty-body
        hint. 'all' = every top-level body with solid features."""
        self._skipped_operands = []
        if _is_all(body_name):
            bodies = bodies_with_solid(part)
            if not bodies:
                raise ObjectNotFound("No body of the active part holds solid features.")
            # A body consumed by a boolean (Add / Remove / Intersect ...) is
            # already part of its target body's result: summing it again
            # would double-count. Body.InBooleanOperation is V5 API;
            # UNVERIFIED on R428 (unreadable = kept).
            operands = [b for b in bodies if safe_get(b, "InBooleanOperation", False) is True]
            kept = [b for b in bodies if b not in operands] or bodies
            self._skipped_operands = [com_name(b) for b in operands if b not in kept]
            return kept, None
        if body_name:
            body = body_by_name(part, body_name)
        else:
            body = self.conn.get_active_part_body()
        return [body], empty_body_hint(part, body)

    def _get_inertia(self, density: float | None = None, body_name: str | None = None) -> str:
        self.conn.ensure_connected()
        part = self.conn.get_active_part()
        if density is not None and (not isinstance(density, (int, float)) or density <= 0):
            raise InvalidArgument("density must be a positive number (kg/m3).")
        bodies, hint = self._bodies(part, body_name)
        if hint:
            # Field report 2026-11-03: an empty main body beside the real
            # geometry read as zeros. Say so instead of measuring nothing.
            return json.dumps(
                {"body": com_name(bodies[0]), "empty_body": True, "hint": hint}, indent=2
            )
        if len(bodies) == 1 and not _is_all(body_name):
            result = {"body": com_name(bodies[0])}
            # An explicitly named body is never silently replaced by the
            # whole part; the default body keeps the earlier fallback.
            result.update(
                self._inertia_one(part, bodies[0], density, whole_part_fallback=not body_name)
            )
            if self.conn.is_3dx:
                result["units_note"] = UNIT_NOTE_3DX
            return json.dumps(result, indent=2)

        per_body = []
        for body in bodies:
            entry = {"body": com_name(body)}
            entry.update(self._inertia_one(part, body, density, whole_part_fallback=False))
            per_body.append(entry)
        result = {"bodies": per_body, "total": self._totals(per_body, density)}
        if self._skipped_operands:
            result["skipped_boolean_operands"] = self._skipped_operands
        if self.conn.is_3dx:
            result["units_note"] = UNIT_NOTE_3DX
        return json.dumps(result, indent=2)

    @staticmethod
    def _totals(per_body: list[dict[str, Any]], density: float | None) -> dict[str, Any]:
        """Sums over bodies; a total is given only when every body measured."""
        total: dict[str, Any] = {"body_count": len(per_body)}
        volumes = [b.get("volume_mm3") for b in per_body]
        if all(isinstance(v, (int, float)) for v in volumes):
            volume = sum(volumes)
            total["volume_mm3"] = round(volume, 4)
            total["volume_cm3"] = round(volume / 1000.0, 4)
            if density:
                mass = density * volume / M3_TO_MM3
                total["mass_kg"] = round(mass, 6)
                total["mass_g"] = round(mass * 1000, 3)
                total["density_kg_m3"] = density
            cogs = [b.get("center_of_gravity_mm") for b in per_body]
            if volume > 0 and all(isinstance(c, dict) for c in cogs):
                total["center_of_gravity_mm"] = {
                    axis: round(sum(c[axis] * v for c, v in zip(cogs, volumes)) / volume, 4)
                    for axis in ("x", "y", "z")
                }
                total["center_of_gravity_basis"] = "volume-weighted (uniform density assumed)"
        else:
            total["volume_mm3"] = "incomplete: not every body could be measured"
        masses = [b.get("mass_from_material_kg") for b in per_body]
        if all(isinstance(m, (int, float)) for m in masses):
            total["mass_from_material_kg"] = round(sum(masses), 6)
        return total

    def _inertia_one(
        self, part: Any, body: Any, density: float | None, *, whole_part_fallback: bool
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        volume_m3: float | None = None

        try:
            self.measure.last_volume_source = None
            volume_mm3 = self.measure.volume_mm3(part, body)
            volume_m3 = volume_mm3 / _M3_TO_MM3
            result["volume_mm3"] = round(volume_mm3, 4)
            result["volume_cm3"] = round(volume_m3 * 1e6, 4)
            if self.measure.last_volume_source:
                result["volume_source"] = self.measure.last_volume_source
        except Exception as e:
            result["volume_mm3"] = f"unavailable: {e}"

        try:
            area_mm2 = self.measure.area_mm2(part, body)
            result["area_mm2"] = round(area_mm2, 4)
            result["area_cm2"] = round(area_mm2 / 100.0, 4)
        except Exception as e:
            result["area_mm2"] = f"unavailable: {e}"

        cog: list[float] | None = None
        try:
            # GetCOG coordinates come back in the document's length unit
            # (mm), not MKS: field-observed on V5R20 for GetPlane/GetCOG.
            cog = self.measure.cog_mm(part, body)
            result["center_of_gravity_mm"] = {
                "x": round(cog[0], 4),
                "y": round(cog[1], 4),
                "z": round(cog[2], 4),
            }
        except Exception as e:
            result["center_of_gravity_mm"] = f"unavailable: {e}"

        if density and volume_m3 is not None:
            mass_kg = density * volume_m3
            result["mass_kg"] = round(mass_kg, 6)
            result["mass_g"] = round(mass_kg * 1000, 3)
            result["density_kg_m3"] = density

        try:
            # Inertia data lives on the inertia object (V5 SPAWorkbench
            # Inertias, 3DEXPERIENCE InertiaService), not on Measurable.
            if whole_part_fallback:
                inertia = self.measure.inertia(part, body)
            else:
                inertia = self.measure.inertia(part, body, whole_part_fallback=False)
            result["mass_from_material_kg"] = round(inertia["mass_kg"], 6)
            target = inertia.get("target")
            if target and target != "body":
                result["inertia_target"] = (
                    f"{target} (InertiaService refused the body itself: mass and "
                    "matrix are for the whole part, not this body)"
                )
            matrix = inertia.get("matrix")
            if matrix:
                # 6 significant figures: moments of a 100 mm part are ~1e-4
                # kg.m2, which 4 fixed decimals rounded to 0.0008 / 0.0.
                values = [significant(v) for v in matrix]
                result["inertia_matrix_kg_m2"] = [values[0:3], values[3:6], values[6:9]]
            else:
                result["inertia_matrix_kg_m2"] = f"unavailable: {inertia.get('matrix_error')}"
        except Exception as e:
            inertia = {}
            result["inertia_matrix_kg_m2"] = f"unavailable: {e}"
        if "cog_raw" in inertia:
            # Separate from the matrix block so a malformed COG cannot replace
            # an already computed matrix with "unavailable".
            try:
                inertia_cog, unit_basis = inertia_cog_to_mm(inertia["cog_raw"], cog)
                result["inertia_cog_mm"] = {
                    "x": round(inertia_cog[0], 4),
                    "y": round(inertia_cog[1], 4),
                    "z": round(inertia_cog[2], 4),
                }
                result["inertia_cog_unit_basis"] = unit_basis
            except Exception as e:
                result["inertia_cog_mm"] = f"unavailable: {e}"
            else:
                if cog is None and inertia.get("target", "body") == "body":
                    # The measurable COG failed: report the inertia COG in
                    # its place, labelled with where it came from.
                    result["center_of_gravity_measurable_error"] = result["center_of_gravity_mm"]
                    result["center_of_gravity_mm"] = dict(result["inertia_cog_mm"])
                    result["center_of_gravity_source"] = "InertiaService (" + unit_basis + ")"
        return result

    def _get_bounding_box(self, body_name: str | None = None) -> str:
        # Measurable has no bounding-box method in any V5 release, so the
        # box is computed from the final shape's vertices (exact for
        # planar-faced solids; curved faces can extend past their vertices,
        # which the output states explicitly).
        self.conn.ensure_connected()
        part = self.conn.get_active_part()
        bodies, hint = self._bodies(part, body_name)
        if hint:
            raise ObjectNotFound(hint)
        if not body_has_solid(bodies[0]):
            raise ObjectNotFound(
                f"Body '{com_name(bodies[0])}' has no solid shape yet; no bounding box."
            )
        boxes = []
        for body in bodies:
            box = body_vertex_box(self.measure, self.conn.hso, body, is_3dx=self.conn.is_3dx)
            boxes.append((com_name(body), box))

        def shaped(box: dict[str, Any]) -> dict[str, Any]:
            dims = box_dims(box)
            return {
                "min": {k: round(v, 4) for k, v in zip("xyz", box["min"])},
                "max": {k: round(v, 4) for k, v in zip("xyz", box["max"])},
                "dimensions": {
                    "length_x": round(dims[0], 4),
                    "length_y": round(dims[1], 4),
                    "length_z": round(dims[2], 4),
                },
            }

        vertices = sum(b["vertices"] for _, b in boxes)
        truncated = any(b["truncated"] for _, b in boxes)
        method = (
            f"vertex sweep over {vertices} vertices of the final shape; "
            "exact for planar-faced solids, curved faces may extend "
            "beyond these bounds"
            + (" (vertex count capped at 200 per body)" if truncated else "")
        )
        if len(boxes) == 1 and not _is_all(body_name):
            result = shaped(boxes[0][1])
            result["body"] = boxes[0][0]
            result["method"] = method
            return json.dumps(result, indent=2)
        union = {
            "min": [min(b["min"][i] for _, b in boxes) for i in range(3)],
            "max": [max(b["max"][i] for _, b in boxes) for i in range(3)],
        }
        result = shaped(union)
        result["bodies"] = [{"body": name, **shaped(box)} for name, box in boxes]
        if self._skipped_operands:
            result["skipped_boolean_operands"] = self._skipped_operands
        result["method"] = method
        return json.dumps(result, indent=2)

    def _measure_element(self, name: str) -> str:
        self.conn.ensure_connected()
        part = self.conn.get_active_part()
        ref = self._resolve_reference(part, name)
        result: dict[str, Any] = {"element": name}
        if self.conn.is_3dx:
            for kind, label, scale in (
                ("volume", "volume_mm3", M3_TO_MM3),
                ("surface", "area_mm2", M2_TO_MM2),
                ("curve", "length_mm", M_TO_MM),
            ):
                try:
                    measurable = self.measure.measurable_3dx(ref, kind)
                    names = {
                        "volume": ("GetVolume", "Volume"),
                        "surface": ("GetArea", "Area"),
                        "curve": ("GetLength", "Length"),
                    }[kind]
                    result[label] = round(self.measure.scalar(measurable, names) * scale, 4)
                except Exception:
                    continue
            try:
                result["point_mm"] = [round(v, 4) for v in self.measure.point_mm(ref)]
            except Exception:
                pass
            result["units_note"] = UNIT_NOTE_3DX
        else:
            measurable = self.measure.v5_measurable(ref)
            for attr, label, scale in (
                ("Volume", "volume_mm3", M3_TO_MM3),
                ("Area", "area_mm2", M2_TO_MM2),
                ("Length", "length_mm", M_TO_MM),
                ("Radius", "radius_mm", M_TO_MM),
                ("Perimeter", "perimeter_mm", M_TO_MM),
            ):
                try:
                    value = float(getattr(measurable, attr))
                except Exception:
                    continue
                result[label] = round(value * scale, 4)
            for method, label in (("GetCOG", "center_of_gravity_mm"), ("GetPoint", "point_mm")):
                try:
                    values = call_fill_array(self.conn.app, measurable, method, 3)
                    result[label] = [round(v, 4) for v in values]
                except Exception:
                    continue
            if any(k in result for k in ("length_mm", "radius_mm", "perimeter_mm")):
                result["units_note"] = (
                    "length, radius and perimeter converted from MKS (m) like the "
                    "field-confirmed area and volume; not field-verified themselves"
                )
        if not set(result) - {"element", "units_note"}:
            raise RuntimeError(
                f"No measure applies to '{name}' through the automation API (tried "
                "volume, area, length, radius, point)."
            )
        return json.dumps(result, indent=2)

    def _get_parameters(self, name_filter: str | None = None) -> str:
        self.conn.ensure_connected()
        part = self.conn.get_active_part()
        params = part.Parameters

        result = []
        for i in range(1, params.Count + 1):
            param = params.Item(i)
            name = param.Name

            if name_filter and name_filter.lower() not in name.lower():
                continue

            info: dict[str, Any] = {"name": name}
            try:
                info["value"] = param.Value
            except Exception:
                info["value"] = "N/A"
            try:
                info["comment"] = param.Comment
            except Exception:
                pass

            result.append(info)

        if not result:
            return "No parameters found" + (f" matching '{name_filter}'" if name_filter else "")
        return json.dumps(result, indent=2, ensure_ascii=False)

    def _set_parameter(self, name: str, value: float) -> str:
        self.conn.ensure_connected()
        part = self.conn.get_active_part()
        params = part.Parameters

        param = params.Item(name)
        old_value = param.Value
        param.Value = value
        part.Update()

        self.conn.refresh_display()
        return f"Parameter '{name}' changed: {old_value} -> {value}"

    def _update_part(self) -> str:
        self.conn.ensure_connected()
        part = self.conn.get_active_part()
        part.Update()
        self.conn.refresh_display()
        return "Part updated successfully"
