"""Sketcher tools for CATIA V5 and CATIA 3DEXPERIENCE.

2D sketch creation and editing: lines, circles, rectangles, arcs, splines, constraints.
All dimensions are in millimeters. CATIA COM API uses millimeters natively.
The Sketcher automation API (Sketches.Add, OpenEdition, Factory2D,
Constraints) is the same on both platforms once the Part is resolved.
"""

from __future__ import annotations

import json
from typing import Any

from catia_mcp.connection import CATIAConnection
from catia_mcp.errors import CATIAToolError, InvalidArgument, ObjectNotFound
from catia_mcp.references import element_or_face_reference

# Plane name mapping
PLANE_MAP = {
    "xy": "PlaneXY",
    "yz": "PlaneYZ",
    "zx": "PlaneZX",
    "xz": "PlaneZX",  # alias
}


class SketcherTools:
    """Tools for 2D sketch operations in CATIA V5 and 3DEXPERIENCE."""

    def __init__(self, connection: CATIAConnection) -> None:
        self.conn = connection
        self._active_sketch: Any | None = None
        self._active_factory: Any | None = None
        self._sketch_part: Any | None = None

    def reset_session_state(self) -> None:
        """Drop the cached sketch, factory and part proxies after a CATIA
        reconnect: they point into the previous CATIA process."""
        self._clear_local_state()
        self.conn.open_sketch = None

    def _clear_local_state(self) -> None:
        self._active_sketch = None
        self._active_factory = None
        self._sketch_part = None

    def _open_elsewhere_closed(self) -> bool:
        """True when this tool still holds a sketch that the shared
        connection no longer lists as open.

        The open sketch is published on the connection (``open_sketch``) so
        Part Design tools can close it before building a feature on it;
        when they do, or after a reconnect, the cached factory here is stale.
        """
        return (
            self._active_sketch is not None
            and getattr(self.conn, "open_sketch", None) is not self._active_sketch
        )

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "catia_create_sketch",
                "description": (
                    "Create a new 2D sketch on a reference plane (xy, yz, or zx), on a "
                    "named plane element, or on a planar face of the solid. "
                    "The sketch is created in the active body and opened for editing. "
                    "You must close it with catia_close_sketch before creating 3D features."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "plane": {
                            "type": "string",
                            "description": "Reference plane: 'xy' (front), 'yz' (right), 'zx' (top)",
                            "enum": ["xy", "yz", "zx"],
                            "default": "xy",
                        },
                        "support": {
                            "type": "string",
                            "description": (
                                "Optional support overriding 'plane': a plane element name "
                                "(e.g. 'Plane.1' from catia_gsd_plane_offset) or a planar "
                                "face 'Face.N' from catia_list_faces"
                            ),
                        },
                    },
                },
            },
            {
                "name": "catia_close_sketch",
                "description": (
                    "Close the active sketch and return to Part Design. "
                    "Must be called after finishing sketch geometry before applying 3D features."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "catia_sketch_line",
                "description": (
                    "Draw a line in the active sketch from (x1, y1) to (x2, y2). "
                    "Coordinates in mm."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "x1": {"type": "number", "description": "Start X coordinate (mm)"},
                        "y1": {"type": "number", "description": "Start Y coordinate (mm)"},
                        "x2": {"type": "number", "description": "End X coordinate (mm)"},
                        "y2": {"type": "number", "description": "End Y coordinate (mm)"},
                    },
                    "required": ["x1", "y1", "x2", "y2"],
                },
            },
            {
                "name": "catia_sketch_rectangle",
                "description": (
                    "Draw a rectangle in the active sketch defined by two opposite corners. "
                    "Creates 4 lines forming a closed profile and, by default, length "
                    "constraints for its width and height. Coordinates in mm."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "x1": {"type": "number", "description": "First corner X (mm)"},
                        "y1": {"type": "number", "description": "First corner Y (mm)"},
                        "x2": {"type": "number", "description": "Opposite corner X (mm)"},
                        "y2": {"type": "number", "description": "Opposite corner Y (mm)"},
                        "dimensions": {
                            "type": "boolean",
                            "description": (
                                "Add driving length constraints on one horizontal and one "
                                "vertical edge (width and height), so drawings can generate "
                                "the dimensions. Default true."
                            ),
                            "default": True,
                        },
                    },
                    "required": ["x1", "y1", "x2", "y2"],
                },
            },
            {
                "name": "catia_sketch_centered_rectangle",
                "description": (
                    "Draw a rectangle centered at (cx, cy) with given width and height. "
                    "By default adds length constraints for the width and height, so a "
                    "drawing can generate them. Coordinates and dimensions in mm."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "cx": {"type": "number", "description": "Center X (mm)", "default": 0},
                        "cy": {"type": "number", "description": "Center Y (mm)", "default": 0},
                        "width": {"type": "number", "description": "Width in mm"},
                        "height": {"type": "number", "description": "Height in mm"},
                        "dimensions": {
                            "type": "boolean",
                            "description": (
                                "Add driving length constraints on one horizontal and one "
                                "vertical edge (width and height), so drawings can generate "
                                "the dimensions. Default true."
                            ),
                            "default": True,
                        },
                    },
                    "required": ["width", "height"],
                },
            },
            {
                "name": "catia_sketch_circle",
                "description": "Draw a circle in the active sketch. Coordinates and radius in mm.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "cx": {"type": "number", "description": "Center X (mm)", "default": 0},
                        "cy": {"type": "number", "description": "Center Y (mm)", "default": 0},
                        "radius": {"type": "number", "description": "Radius in mm"},
                    },
                    "required": ["radius"],
                },
            },
            {
                "name": "catia_sketch_arc",
                "description": (
                    "Draw a circular arc defined by center, radius, and start/end angles (degrees). "
                    "Angles are measured counter-clockwise from the positive X axis."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "cx": {"type": "number", "description": "Center X (mm)"},
                        "cy": {"type": "number", "description": "Center Y (mm)"},
                        "radius": {"type": "number", "description": "Radius (mm)"},
                        "start_angle": {"type": "number", "description": "Start angle (degrees)"},
                        "end_angle": {"type": "number", "description": "End angle (degrees)"},
                    },
                    "required": ["cx", "cy", "radius", "start_angle", "end_angle"],
                },
            },
            {
                "name": "catia_sketch_spline",
                "description": (
                    "Draw a spline through a list of control points. "
                    "Each point is [x, y] in mm."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "points": {
                            "type": "array",
                            "items": {
                                "type": "array",
                                "items": {"type": "number"},
                                "minItems": 2,
                                "maxItems": 2,
                            },
                            "description": "List of [x, y] control points in mm",
                            "minItems": 2,
                        },
                        "closed": {
                            "type": "boolean",
                            "description": "Whether to close the spline (default: false)",
                            "default": False,
                        },
                    },
                    "required": ["points"],
                },
            },
            {
                "name": "catia_sketch_point",
                "description": "Create a point in the active sketch. Coordinates in mm.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "x": {"type": "number", "description": "X coordinate (mm)"},
                        "y": {"type": "number", "description": "Y coordinate (mm)"},
                    },
                    "required": ["x", "y"],
                },
            },
            {
                "name": "catia_sketch_constraint",
                "description": (
                    "Add a dimensional constraint to the active sketch. "
                    "Supported types: distance, radius, angle, coincidence, tangent, "
                    "perpendicular, parallel, horizontal, vertical, fix."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "description": "Constraint type",
                            "enum": [
                                "distance", "radius", "angle",
                                "coincidence", "tangent", "perpendicular",
                                "parallel", "horizontal", "vertical", "fix",
                            ],
                        },
                        "value": {
                            "type": "number",
                            "description": "Constraint value (mm or degrees). Required for distance, radius, angle.",
                        },
                        "geometry_index_1": {
                            "type": "integer",
                            "description": "Index of first geometry element (1-based, from sketch geometry list)",
                        },
                        "geometry_index_2": {
                            "type": "integer",
                            "description": "Index of second geometry element (for relational constraints)",
                        },
                    },
                    "required": ["type"],
                },
            },
            {
                "name": "catia_sketch_get_geometry",
                "description": "List all geometry elements in the active sketch with their indices and types.",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                },
            },
        ]

    def execute(self, tool_name: str, arguments: dict[str, Any]) -> str:
        match tool_name:
            case "catia_create_sketch":
                return self._create_sketch(arguments.get("plane", "xy"), arguments.get("support"))
            case "catia_close_sketch":
                return self._close_sketch()
            case "catia_sketch_line":
                return self._draw_line(
                    arguments["x1"], arguments["y1"],
                    arguments["x2"], arguments["y2"],
                )
            case "catia_sketch_rectangle":
                return self._draw_rectangle(
                    arguments["x1"], arguments["y1"],
                    arguments["x2"], arguments["y2"],
                    dimensions=bool(arguments.get("dimensions", True)),
                )
            case "catia_sketch_centered_rectangle":
                return self._draw_centered_rectangle(
                    arguments.get("cx", 0), arguments.get("cy", 0),
                    arguments["width"], arguments["height"],
                    dimensions=bool(arguments.get("dimensions", True)),
                )
            case "catia_sketch_circle":
                return self._draw_circle(
                    arguments.get("cx", 0), arguments.get("cy", 0),
                    arguments["radius"],
                )
            case "catia_sketch_arc":
                return self._draw_arc(
                    arguments["cx"], arguments["cy"], arguments["radius"],
                    arguments["start_angle"], arguments["end_angle"],
                )
            case "catia_sketch_spline":
                return self._draw_spline(
                    arguments["points"], arguments.get("closed", False),
                )
            case "catia_sketch_point":
                return self._draw_point(arguments["x"], arguments["y"])
            case "catia_sketch_constraint":
                return self._add_constraint(arguments)
            case "catia_sketch_get_geometry":
                return self._get_geometry()
            case _:
                raise ValueError(f"Unknown sketcher tool: {tool_name}")

    def _ensure_sketch_open(self) -> None:
        if self._open_elsewhere_closed():
            self._clear_local_state()
            raise ObjectNotFound(
                "No active sketch: the last sketch was closed (automatically by a Part "
                "Design feature, or by a CATIA reconnect). Use catia_create_sketch to "
                "open a new sketch."
            )
        if self._active_sketch is None:
            raise ObjectNotFound(
                "No active sketch. Use catia_create_sketch first to open a sketch."
            )

    def _create_sketch(self, plane: str = "xy", support: str | None = None) -> str:
        self.conn.ensure_connected()
        part = self.conn.get_active_part()
        body = self.conn.get_active_part_body()

        if support:
            ref = element_or_face_reference(self.conn, part, support)
        else:
            # Get the reference plane
            origin = part.OriginElements
            plane_key = plane.lower()
            if plane_key not in PLANE_MAP:
                raise InvalidArgument(f"Unknown plane '{plane}'. Use 'xy', 'yz', or 'zx'.")

            plane_attr = PLANE_MAP[plane_key]
            ref_plane = getattr(origin, plane_attr)
            ref = part.CreateReferenceFromObject(ref_plane)

        # Create the sketch on the plane
        sketches = body.Sketches
        sketch = sketches.Add(ref)

        # Open the sketch for editing. V5-6R2020: the first OpenEdition can
        # return None; close/recreate the sketch and retry once before
        # giving up.
        self._active_sketch = sketch
        self._sketch_part = part
        try:
            factory = sketch.OpenEdition()
        except Exception:
            factory = None
        if factory is None:
            try:
                sketch.CloseEdition()
            except Exception:
                pass
            sketch = sketches.Add(ref)
            factory = sketch.OpenEdition()
            if factory is None:
                raise RuntimeError(
                    "Failed to open sketch edition: OpenEdition returned None twice."
                )
            self._active_sketch = sketch
        self._active_factory = factory
        # Shared with the Part Design tools, which close a sketch left open
        # before building a feature (see PartDesignTools._close_open_sketch).
        self.conn.open_sketch = sketch

        try:
            sketch_name = str(sketch.Name)
        except Exception:
            sketch_name = "sketch"
        if support:
            return (
                f"Sketch '{sketch_name}' created on '{support}' in body '{body.Name}'. "
                "Ready for geometry."
            )
        plane_names = {"xy": "XY (front)", "yz": "YZ (right)", "zx": "ZX (top)"}
        return (
            f"Sketch '{sketch_name}' created on {plane_names.get(plane_key, plane)} plane. "
            "Ready for geometry."
        )

    def _close_sketch(self) -> str:
        if self._open_elsewhere_closed():
            try:
                name = str(self._active_sketch.Name)
            except Exception:
                name = "sketch"
            self._clear_local_state()
            return (
                f"Sketch '{name}' was already closed (automatically by a Part Design "
                "feature or a CATIA reconnect); nothing to do."
            )
        if self._active_sketch is None:
            # The cached handle does not survive a server restart while CATIA
            # keeps the sketch open in edition. Re-adopt the document's
            # in-work object when it looks like a sketch instead of failing.
            try:
                part = self.conn.get_active_part()
                candidate = part.InWorkObject
                getattr(candidate, "CloseEdition")
                self._active_sketch = candidate
                self.conn.open_sketch = candidate
            except Exception:
                pass
        self._ensure_sketch_open()
        sketch = self._active_sketch
        try:
            name = str(sketch.Name)
        except Exception:
            name = "sketch"
        try:
            sketch.CloseEdition()
            owner = getattr(self, "_sketch_part", None) or self.conn.get_active_part()
            owner.UpdateObject(sketch)
        finally:
            # Clear cached state even when the close fails, so a broken
            # handle cannot poison subsequent calls.
            self._clear_local_state()
            self.conn.open_sketch = None
        self.conn.refresh_display()
        return (
            f"Sketch '{name}' closed. You can now apply Part Design features "
            "(pad, pocket, user pattern on its points, etc.)."
        )

    def _draw_line(self, x1: float, y1: float, x2: float, y2: float) -> str:
        self._ensure_sketch_open()
        factory = self._active_factory
        factory.CreateLine(x1, y1, x2, y2)
        return f"Line created from ({x1}, {y1}) to ({x2}, {y2}) mm"

    def _draw_rectangle(
        self, x1: float, y1: float, x2: float, y2: float, *, dimensions: bool = True
    ) -> str:
        self._ensure_sketch_open()
        factory = self._active_factory

        # Create 4 lines forming a closed rectangle
        bottom = factory.CreateLine(x1, y1, x2, y1)
        right = factory.CreateLine(x2, y1, x2, y2)
        factory.CreateLine(x2, y2, x1, y2)  # top
        factory.CreateLine(x1, y2, x1, y1)  # left

        result = (
            f"Rectangle created from ({x1}, {y1}) to ({x2}, {y2}) mm "
            f"[{abs(x2-x1):.1f} x {abs(y2-y1):.1f} mm]"
        )
        if not dimensions:
            return result
        added = self._dimension_lines(
            [("width", bottom, abs(x2 - x1)), ("height", right, abs(y2 - y1))]
        )
        return f"{result}; constraints: {', '.join(added)}"

    def _dimension_lines(self, edges: list[tuple[str, Any, float]]) -> list[str]:
        """Driving length constraint on each (label, line, length).

        Field-proven on 3DEXPERIENCE R428 SP6 (2026-11-02 field report):
        Constraints.AddMonoEltCst(5, reference of the line) (5 =
        catCstTypeLength), then cst.Dimension.Value = length. The field
        recipe called Sketch.OpenEdition() first because it ran on a closed
        sketch; here the sketch is still in edition from catia_create_sketch.
        Mode = 0 (driving) is the V5-6R2020 recipe of catia_sketch_constraint.
        The rectangle lines already exist when this runs, so a failure is
        reported as a modified model.
        """
        sketch = self._active_sketch
        part = getattr(self, "_sketch_part", None) or self.conn.get_active_part()
        added: list[str] = []
        try:
            constraints = sketch.Constraints
            for label, line, length in edges:
                if length <= 0:
                    continue
                cst = constraints.AddMonoEltCst(self._CST_LENGTH, part.CreateReferenceFromObject(line))
                try:
                    cst.Mode = 0
                except Exception:
                    pass
                cst.Dimension.Value = length
                try:
                    name = str(cst.Name)
                except Exception:
                    name = "Length"
                added.append(f"{label} {length:g} mm ('{name}')")
        except Exception as error:
            done = f" ({', '.join(added)} added)" if added else ""
            raise CATIAToolError(
                f"The rectangle lines were drawn, but adding its length constraints "
                f"failed{done}: {error}. Do not redraw it: add the missing dimension with "
                "catia_sketch_constraint (type 'distance' on one line), or ignore it if "
                "the drawing does not need it.",
                modified=None,
            ) from error
        return added

    def _draw_centered_rectangle(
        self, cx: float, cy: float, width: float, height: float, *, dimensions: bool = True
    ) -> str:
        hw, hh = width / 2, height / 2
        return self._draw_rectangle(cx - hw, cy - hh, cx + hw, cy + hh, dimensions=dimensions)

    def _draw_circle(self, cx: float, cy: float, radius: float) -> str:
        self._ensure_sketch_open()
        factory = self._active_factory
        factory.CreateClosedCircle(cx, cy, radius)
        return f"Circle created at ({cx}, {cy}) with radius {radius} mm"

    def _draw_arc(
        self, cx: float, cy: float, radius: float,
        start_angle: float, end_angle: float,
    ) -> str:
        self._ensure_sketch_open()
        factory = self._active_factory
        import math
        # Factory2D has no CreateArc in CATIA V5 (field-verified against the
        # R20 API reference). An arc is an open circle: CreateCircle takes
        # optional start/end parameters in radians that leave it open.
        start_rad = math.radians(start_angle)
        end_rad = math.radians(end_angle)
        # CreateCircle requires endParam strictly greater than startParam;
        # normalize sweeps that wrap past 0 degrees.
        while end_rad <= start_rad:
            end_rad += 2 * math.pi
        factory.CreateCircle(cx, cy, radius, start_rad, end_rad)
        return (
            f"Arc created at ({cx}, {cy}), radius={radius} mm, "
            f"from {start_angle}° to {end_angle}°"
        )

    def _draw_spline(self, points: list[list[float]], closed: bool = False) -> str:
        self._ensure_sketch_open()
        factory = self._active_factory

        # Create a spline using control points
        # CATIA V5 Sketch.OpenEdition() returns a Factory2D
        # Factory2D.CreateSpline expects an array of 2D points
        spline_pts = []
        for pt in points:
            ctrl_pt = factory.CreatePoint(pt[0], pt[1])
            spline_pts.append(ctrl_pt)

        factory.CreateSpline(spline_pts)

        if closed and len(points) >= 3:
            # Close the spline by adding a line from last to first point
            factory.CreateLine(points[-1][0], points[-1][1], points[0][0], points[0][1])

        pts_str = ", ".join(f"({p[0]}, {p[1]})" for p in points)
        return f"Spline created through {len(points)} points: {pts_str}" + (
            " (closed)" if closed else ""
        )

    def _draw_point(self, x: float, y: float) -> str:
        self._ensure_sketch_open()
        factory = self._active_factory
        point = factory.CreatePoint(x, y)
        # CreatePoint defaults to construction geometry, which user patterns
        # (AddFeatureToLocatePositions) ignore. Field-validated on V5R20.
        try:
            point.Construction = False
        except Exception:
            pass
        return f"Point created at ({x}, {y}) mm"

    # CatConstraintType values from the R20 automation reference
    # (MecModInterfaces, enum CatConstraintType): Reference=0, Distance=1,
    # On=2, Concentricity=3, Tangency=4, Length=5, Angle=6, PlanarAngle=7,
    # Parallelism=8, AxisParallelism=9, Horizontality=10, Perpendicularity=11,
    # AxisPerpendicularity=12, Verticality=13, Radius=14, Symmetry=15.
    _CST_REFERENCE = 0
    _CST_DISTANCE = 1
    _CST_ON = 2
    _CST_TANGENCY = 4
    _CST_LENGTH = 5
    _CST_ANGLE = 6
    _CST_PARALLELISM = 8
    _CST_HORIZONTALITY = 10
    _CST_PERPENDICULARITY = 11
    _CST_VERTICALITY = 13
    _CST_RADIUS = 14

    def _add_constraint(self, args: dict[str, Any]) -> str:
        self._ensure_sketch_open()
        sketch = self._active_sketch
        constraint_type = args["type"]
        value = args.get("value")
        idx1 = args.get("geometry_index_1")
        idx2 = args.get("geometry_index_2")

        constraints = sketch.Constraints
        geom = sketch.GeometricElements
        part = self.conn.get_active_part()

        # AddMonoEltCst/AddBiEltCst take Reference arguments; passing raw
        # geometry elements raises DISP_E_TYPEMISMATCH (field-verified).
        def _ref(index: int) -> Any:
            return part.CreateReferenceFromObject(geom.Item(index))

        # Dimensional constraints (need a geometry reference + value)
        if constraint_type in ("distance", "radius", "angle"):
            if value is None:
                raise InvalidArgument(f"Constraint type '{constraint_type}' requires a 'value' parameter.")
            if idx1 is None:
                raise InvalidArgument(f"Constraint type '{constraint_type}' requires 'geometry_index_1'.")

            if constraint_type == "distance" and idx2 is not None:
                cst = constraints.AddBiEltCst(self._CST_DISTANCE, _ref(idx1), _ref(idx2))
            elif constraint_type == "distance":
                # A single-element distance is a length dimension in CATIA.
                cst = constraints.AddMonoEltCst(self._CST_LENGTH, _ref(idx1))
            elif constraint_type == "radius":
                cst = constraints.AddMonoEltCst(self._CST_RADIUS, _ref(idx1))
            else:  # angle
                if idx2 is None:
                    raise InvalidArgument("Angle constraint requires 'geometry_index_2'.")
                cst = constraints.AddBiEltCst(self._CST_ANGLE, _ref(idx1), _ref(idx2))
            try:
                # V5-6R2020: dimensional constraints must be driving to take
                # the value (CatConstraintMode: driving = 0).
                cst.Mode = 0
            except Exception:
                pass
            cst.Dimension.Value = value

            unit = "deg" if constraint_type == "angle" else "mm"
            return f"{constraint_type.capitalize()} constraint '{cst.Name}' added: {value} {unit}"

        # Geometric constraints (no value needed)
        cst_type_map = {
            "coincidence": self._CST_ON,
            "tangent": self._CST_TANGENCY,
            "perpendicular": self._CST_PERPENDICULARITY,
            "parallel": self._CST_PARALLELISM,
            "horizontal": self._CST_HORIZONTALITY,
            "vertical": self._CST_VERTICALITY,
            "fix": self._CST_REFERENCE,
        }

        cst_code = cst_type_map.get(constraint_type)
        if cst_code is None:
            raise InvalidArgument(f"Unknown constraint type: {constraint_type}")

        if constraint_type in ("horizontal", "vertical", "fix"):
            if idx1 is None:
                raise InvalidArgument(f"Constraint '{constraint_type}' requires 'geometry_index_1'.")
            cst = constraints.AddMonoEltCst(cst_code, _ref(idx1))
            if constraint_type == "fix":
                try:
                    # Fix-in-space needs the reference type set explicitly
                    # (CatConstraintReferenceType: fix in space = 1).
                    cst.ReferenceType = 1
                except Exception:
                    pass
        else:
            if idx1 is None or idx2 is None:
                raise InvalidArgument(
                    f"Constraint '{constraint_type}' requires both 'geometry_index_1' and 'geometry_index_2'."
                )
            cst = constraints.AddBiEltCst(cst_code, _ref(idx1), _ref(idx2))

        return f"{constraint_type.capitalize()} constraint '{cst.Name}' added"

    def _get_geometry(self) -> str:
        self._ensure_sketch_open()
        sketch = self._active_sketch
        geom = sketch.GeometricElements

        elements = []
        for i in range(1, geom.Count + 1):
            elem = geom.Item(i)
            info = {
                "index": i,
                "name": elem.Name,
            }
            # Try to get the geometry type
            try:
                info["type"] = elem.GeometricType
            except Exception:
                pass
            elements.append(info)

        if not elements:
            return "No geometry elements in the active sketch"
        return json.dumps(elements, indent=2)
