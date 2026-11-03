"""Part Design tools for CATIA V5 and CATIA 3DEXPERIENCE.

3D feature creation: Pad, Pocket, Fillet, Chamfer, Shaft, Groove, Hole,
RectPattern, CircPattern, UserPattern, Mirror, Rib, Slot, Shell, Thickness,
Draft, plus bodies, boolean operations and feature management.

The ShapeFactory API is the same on both platforms once the Part is
resolved (connection.get_active_part). Platform differences are confined to
measurement (measure.Measurer), topology search (references), the
circular-pattern axis recipe and the user-pattern refusal of Hole seeds.
"""

from __future__ import annotations

import json
import math
from typing import Any

from catia_mcp.automation import call_fill_array, com_name, iter_items, name_exists
from catia_mcp.connection import CATIAConnection
from catia_mcp.errors import CATIAToolError, InvalidArgument, ObjectNotFound, UnsupportedCapability
from catia_mcp.introspect import type_name
from catia_mcp.measure import Measurer
from catia_mcp.references import (
    body_by_name,
    find_element,
    iter_geosets,
    search_final_topology,
    topology_index,
    topology_reference,
    topology_references,
)

# CatHoleThreadStandard codes for Hole.CreateStandardThreadDesignTable.
# 1 = metric coarse (thick pitch) is field-proven on R428 SP6 (2026-11-02
# field report: CreateStandardThreadDesignTable(1) + "M6" gave M6x1.0 with
# the 4.917 mm tap drill). 0 = metric fine (thin pitch) follows the enum
# order of the automation reference; UNC = 2 and UNF = 3 are UNVERIFIED
# guesses. The designation read-back catches a wrong table.
_THREAD_STANDARDS = {
    "metric_coarse": 1,
    "metric_fine": 0,
    "unc": 2,
    "unf": 3,
}

# CatHoleThreadingMode (B30 type libraries): threaded = 0, smooth = 1.
_THREADED = 0
_SMOOTH = 1

# CatLimitMode: catUpToLastLimit = 2 (bottom limit "up to last"). UNVERIFIED
# live for holes.
_UP_TO_LAST = 2


def _set_prop(obj: Any, name: str, value: Any) -> bool:
    """Set a COM property with a parameter-object fallback.

    Direct property PUTs can fail under dynamic dispatch on objects returned
    by factory methods (field-verified on V5-6R2020); properties exposed as
    parameter objects are then set through their ``Value`` attribute.
    """
    try:
        setattr(obj, name, value)
        return True
    except Exception:
        try:
            getattr(obj, name).Value = value
            return True
        except Exception:
            return False


def _force_solid_inwork(part: Any, sketch: Any) -> None:
    """Make the body owning ``sketch`` the in-work object.

    Freshly created Part documents start with a Geometrical Set as the
    in-work object; Pad/Pocket/Hole/Shaft/Groove then fail with E_FAIL
    (field-verified on V5-6R2020). ``Sketch.Parent`` is the Sketches
    collection, so climb one more level to reach the owning Body.
    """
    try:
        target = part.MainBody
        try:
            parent = sketch.Parent
            if getattr(parent, "Name", "") in ("Sketches", "HybridSketches"):
                parent = parent.Parent
            if parent is not None:
                target = parent
        except Exception:
            pass
        part.InWorkObject = target
    except Exception:
        pass


class PartDesignTools:
    """Tools for 3D Part Design features in CATIA V5 and 3DEXPERIENCE."""

    def __init__(self, connection: CATIAConnection) -> None:
        self.conn = connection
        self.measure = Measurer(connection)
        # Name of a sketch the current call closed automatically (see
        # _close_open_sketch); reported with the result, then cleared.
        self._auto_closed_sketch: str | None = None

    def reset_session_state(self) -> None:
        """Drop every cached COM proxy after a CATIA reconnect.

        The Measurer caches the measurable-type enum and the codes that
        worked on the previous session; the open-sketch handle shared with
        the sketcher points into the old process.
        """
        self.measure = Measurer(self.conn)
        self._auto_closed_sketch = None
        self.conn.open_sketch = None

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "catia_pad",
                "description": (
                    "Create a Pad (extrusion) from the last sketch. "
                    "Extrudes a 2D profile into a 3D solid along the normal to the sketch plane. "
                    "The added volume is measured and reported; a pad that adds nothing "
                    "fails with FEATURE_NO_EFFECT (the pad stays in the tree). A sketch "
                    "still open in the sketcher is closed first and used as the profile."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "height": {
                            "type": "number",
                            "description": "Extrusion height/depth in mm",
                        },
                        "direction": {
                            "type": "string",
                            "description": "Extrusion direction: 'normal' (default), 'reverse', 'both'",
                            "enum": ["normal", "reverse", "both"],
                            "default": "normal",
                        },
                        "symmetric": {
                            "type": "boolean",
                            "description": "If true, extrude equally on both sides (total = height)",
                            "default": False,
                        },
                        "sketch_name": {
                            "type": "string",
                            "description": "Name of sketch to use. If not specified, uses the last created sketch.",
                        },
                    },
                    "required": ["height"],
                },
            },
            {
                "name": "catia_pocket",
                "description": (
                    "Create a Pocket (cut extrusion) from the last sketch. "
                    "Removes material by extruding a 2D profile inward. The removed "
                    "volume is measured and reported. Without 'direction', a pocket "
                    "that removes nothing is flipped once; a pocket that still removes "
                    "nothing, or removes nothing in an explicit 'direction', fails "
                    "with FEATURE_NO_EFFECT (the pocket stays in the tree)."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "depth": {
                            "type": "number",
                            "description": "Cut depth in mm",
                        },
                        "direction": {
                            "type": "string",
                            "description": "Cut direction: 'normal' (default), 'reverse'",
                            "enum": ["normal", "reverse"],
                            "default": "normal",
                        },
                        "symmetric": {
                            "type": "boolean",
                            "description": "If true, mirrored extent: cuts equally on both sides (reliable on V5-6R2020, where DirectionOrientation may not flip the cut side)",
                            "default": False,
                        },
                        "sketch_name": {
                            "type": "string",
                            "description": "Name of sketch to use. If not specified, uses the last sketch.",
                        },
                    },
                    "required": ["depth"],
                },
            },
            {
                "name": "catia_shaft",
                "description": (
                    "Create a Shaft (revolution) from the last sketch. "
                    "Revolves a 2D profile around an axis to create a solid of revolution. "
                    "The revolution axis is the sketch's own V direction (AbsoluteAxis); "
                    "do not draw an axis line (rejected on V5-6R2020)."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "angle": {
                            "type": "number",
                            "description": "Revolution angle in degrees (default: 360 for full revolution)",
                            "default": 360,
                        },
                        "sketch_name": {
                            "type": "string",
                            "description": "Name of sketch to use.",
                        },
                    },
                },
            },
            {
                "name": "catia_groove",
                "description": (
                    "Create a Groove (revolution cut). "
                    "Removes material by revolving a 2D profile around an axis."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "angle": {
                            "type": "number",
                            "description": "Revolution angle in degrees (default: 360)",
                            "default": 360,
                        },
                        "sketch_name": {
                            "type": "string",
                            "description": "Name of sketch to use.",
                        },
                    },
                },
            },
            {
                "name": "catia_fillet",
                "description": (
                    "Add a fillet (rounded edge) to one or more edges of the current solid. "
                    "Specify the radius and the edge names or feature to fillet. A fillet "
                    "CATIA cannot compute fails with FEATURE_UPDATE_FAILED and is removed "
                    "from the tree."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "radius": {
                            "type": "number",
                            "description": "Fillet radius in mm",
                        },
                        "edge_name": {
                            "type": "string",
                            "description": (
                                "Indexed edge to fillet ('Edge.N' from "
                                "catia_list_edges). Omit to fillet all edges "
                                "of the last feature."
                            ),
                        },
                    },
                    "required": ["radius"],
                },
            },
            {
                "name": "catia_chamfer",
                "description": (
                    "Add a chamfer (beveled edge) to an edge of the current solid. A chamfer "
                    "CATIA cannot compute fails with FEATURE_UPDATE_FAILED and is removed "
                    "from the tree."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "length": {
                            "type": "number",
                            "description": "Chamfer length in mm",
                        },
                        "angle": {
                            "type": "number",
                            "description": "Chamfer angle in degrees (default: 45)",
                            "default": 45,
                        },
                        "edge_name": {
                            "type": "string",
                            "description": "Indexed edge to chamfer ('Edge.N' from catia_list_edges). Omit to chamfer all edges of the last feature.",
                        },
                    },
                    "required": ["length"],
                },
            },
            {
                "name": "catia_hole",
                "description": (
                    "Create a Hole feature at a point in the active sketch. "
                    "Supports simple, tapered, counterbored, and countersunk holes. "
                    "The depth is set on the hole's bottom limit and read back; the "
                    "removed volume is measured and reported. A hole that removes "
                    "nothing is reversed once; if it still removes nothing the call "
                    "fails with FEATURE_NO_EFFECT (the hole stays in the tree). "
                    "Threaded holes use a standard thread table: with thread_standard "
                    "metric_coarse and designation 'M6', CATIA sets the tap drill "
                    "itself (diameter becomes 4.917 mm); the resulting diameter and "
                    "thread values are read back and reported. A smooth hole has any "
                    "thread settings CATIA carried over from the previous hole reset. "
                    "If CATIA rejects the hole, it is removed; CATIA deletes the "
                    "input sketch with it, so the error lists the sketch's points to "
                    "recreate it."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "diameter": {
                            "type": "number",
                            "description": (
                                "Hole diameter in mm. For a standard thread, the nominal "
                                "diameter (6 for M6); CATIA replaces it with the tap drill."
                            ),
                        },
                        "depth": {
                            "type": "number",
                            "description": "Hole depth in mm. Optional when through_all is true.",
                        },
                        "through_all": {
                            "type": "boolean",
                            "description": (
                                "Drill through the whole solid (bottom limit 'up to last') "
                                "instead of to 'depth'. Default false."
                            ),
                            "default": False,
                        },
                        "type": {
                            "type": "string",
                            "description": "Hole type: 'simple', 'counterbored', 'countersunk', 'tapered'",
                            "enum": ["simple", "counterbored", "countersunk", "tapered"],
                            "default": "simple",
                        },
                        "threaded": {
                            "type": "boolean",
                            "description": (
                                "Whether to add threading (default: false). Implied by "
                                "thread_standard, designation or thread_depth."
                            ),
                            "default": False,
                        },
                        "thread_standard": {
                            "type": "string",
                            "enum": list(_THREAD_STANDARDS),
                            "description": (
                                "Thread table for a threaded hole (default metric_coarse). "
                                "metric_coarse is field-proven on 3DEXPERIENCE R428; the "
                                "others are unverified."
                            ),
                        },
                        "designation": {
                            "type": "string",
                            "description": (
                                "Thread designation from the table, e.g. 'M6'. Default for "
                                "metric_coarse: 'M' + diameter. Required for metric_fine "
                                "(e.g. 'M8x1'), unc and unf."
                            ),
                        },
                        "thread_depth": {
                            "type": "number",
                            "description": (
                                "Thread depth in mm (default: the hole depth). Must not "
                                "exceed the hole depth."
                            ),
                        },
                        "sketch_name": {
                            "type": "string",
                            "description": "Sketch containing the hole center point",
                        },
                    },
                    "required": ["diameter"],
                },
            },
            {
                "name": "catia_rect_pattern",
                "description": (
                    "Create a Rectangular Pattern of the last feature. "
                    "Duplicates a feature in a grid along two directions."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "dir1_edge": {
                            "type": "string",
                            "description": (
                                "Edge giving direction 1 ('Edge.N' from "
                                "catia_list_edges). Pass with dir2_edge, two "
                                "ADJACENT edges sharing a corner (CATIA "
                                "rejects parallel directions). Omit both to "
                                "let CATIA pick defaults."
                            ),
                        },
                        "dir2_edge": {
                            "type": "string",
                            "description": "Edge giving direction 2 ('Edge.N', adjacent to dir1_edge)",
                        },
                        "dir1_count": {
                            "type": "integer",
                            "description": "Number of instances in first direction",
                        },
                        "dir1_spacing": {
                            "type": "number",
                            "description": "Spacing in first direction (mm)",
                        },
                        "dir2_count": {
                            "type": "integer",
                            "description": "Number of instances in second direction (default: 1)",
                            "default": 1,
                        },
                        "dir2_spacing": {
                            "type": "number",
                            "description": "Spacing in second direction (mm)",
                            "default": 0,
                        },
                        "feature_name": {
                            "type": "string",
                            "description": "Name of the feature to pattern. Defaults to last feature.",
                        },
                    },
                    "required": ["dir1_count", "dir1_spacing"],
                },
            },
            {
                "name": "catia_circ_pattern",
                "description": (
                    "Create a Circular Pattern of the last feature. "
                    "Duplicates a feature around a central axis."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "axis": {
                            "type": "string",
                            "enum": ["x", "y", "z"],
                            "description": "Rotation axis direction (default z)",
                        },
                        "center": {
                            "type": "array",
                            "items": {"type": "number"},
                            "description": "Point [x, y, z] in mm the rotation axis passes through (default origin)",
                        },
                        "count": {
                            "type": "integer",
                            "description": "Number of instances around the circle",
                        },
                        "angular_spacing": {
                            "type": "number",
                            "description": "Angular spacing in degrees (default: equal spacing = 360/count)",
                        },
                        "feature_name": {
                            "type": "string",
                            "description": "Feature to pattern. Defaults to last feature.",
                        },
                    },
                    "required": ["count"],
                },
            },
            {
                "name": "catia_user_pattern",
                "description": (
                    "Create a User Pattern: copies of a feature placed on the "
                    "points of a sketch (the most flexible pattern, e.g. "
                    "arbitrary bolt layouts). Draw the points with "
                    "catia_sketch_point in a dedicated sketch and close it; "
                    "pass its name from catia_close_sketch. Points must not "
                    "coincide with the seed feature's position. On 3DEXPERIENCE "
                    "a Hole seed is refused with UNSUPPORTED_CAPABILITY (CATIA "
                    "rejects it); cut circles with catia_pocket instead."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "count": {
                            "type": "integer",
                            "description": "Number of instances (number of locating points)",
                        },
                        "sketch_name": {
                            "type": "string",
                            "description": "REQUIRED: name of the points sketch as reported by catia_close_sketch (e.g. 'Sketch.3')",
                        },
                        "feature_name": {
                            "type": "string",
                            "description": "Feature to pattern. Defaults to last feature.",
                        },
                    },
                    "required": ["count", "sketch_name"],
                },
            },
            {
                "name": "catia_mirror",
                "description": (
                    "Mirror a feature or body about a plane. "
                    "Creates a symmetric copy of the geometry."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "plane": {
                            "type": "string",
                            "description": "Mirror plane: 'xy', 'yz', or 'zx'",
                            "enum": ["xy", "yz", "zx"],
                        },
                    },
                    "required": ["plane"],
                },
            },
            {
                "name": "catia_shell",
                "description": (
                    "Create a Shell feature: hollows out a solid leaving walls of specified thickness. "
                    "Optionally remove faces to create openings."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "thickness": {
                            "type": "number",
                            "description": "Wall thickness in mm",
                        },
                        "faces_to_remove": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "REQUIRED: indexed faces to remove and open "
                                "('Face.N' from catia_list_faces); the first "
                                "one anchors the shell."
                            ),
                        },
                    },
                    "required": ["thickness"],
                },
            },
            {
                "name": "catia_draft",
                "description": (
                    "Add a Draft Angle to faces for mold-release purposes. "
                    "Tapers faces by a given angle relative to a pulling direction."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "angle": {
                            "type": "number",
                            "description": "Draft angle in degrees",
                        },
                        "face_name": {
                            "type": "string",
                            "description": "REQUIRED: indexed face to draft ('Face.N' from catia_list_faces)",
                        },
                        "pulling_direction": {
                            "type": "string",
                            "description": "Pulling direction plane: 'xy', 'yz', 'zx'",
                            "enum": ["xy", "yz", "zx"],
                            "default": "xy",
                        },
                    },
                    "required": ["angle"],
                },
            },
            {
                "name": "catia_thickness",
                "description": (
                    "Add or remove thickness from faces of a solid. "
                    "Offsets faces inward or outward."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "offset": {
                            "type": "number",
                            "description": "Thickness offset in mm (positive = outward, negative = inward)",
                        },
                        "face_name": {
                            "type": "string",
                            "description": "REQUIRED: indexed face to thicken ('Face.N' from catia_list_faces)",
                        },
                    },
                    "required": ["offset"],
                },
            },
            {
                "name": "catia_delete_feature",
                "description": (
                    "Delete a feature by name (e.g. 'Pocket.2') in any body "
                    "of the active part, then update the part. Features "
                    "depending on it end up in error and are reported."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "feature_name": {
                            "type": "string",
                            "description": "Feature to delete (see catia_list_features)",
                        },
                    },
                    "required": ["feature_name"],
                },
            },
            {
                "name": "catia_list_features",
                "description": "List all features in the active Part Body with their names and types.",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "catia_list_edges",
                "description": (
                    "List the edges of the final solid shape as indexed names "
                    "(Edge.N). These indices feed catia_measure_distance and "
                    "catia_fillet (same enumeration: on 3DEXPERIENCE the solid "
                    "edges of the last feature, e.g. 12 for a padded box); for "
                    "chamfer targeting see its own contract."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "catia_list_faces",
                "description": (
                    "List the faces of a body's final solid shape as indexed "
                    "names (Face.N) for catia_measure_distance, shell, draft, "
                    "thickness and face-driven drawing views. Defaults to the "
                    "main body; pass body_name to enumerate another body "
                    "(use the same body_name in catia_drawing_add_view)."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "body_name": {
                            "type": "string",
                            "description": "Body to enumerate (default: the part's main body)",
                        },
                    },
                },
            },
            {
                "name": "catia_rib",
                "description": (
                    "Create a Rib: sweep a closed profile along a center curve, adding "
                    "material. Profile and center curve are sketch or curve element names "
                    "(close the sketches first). A rib that adds no material fails with "
                    "FEATURE_NO_EFFECT (the rib stays in the tree)."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "profile": {"type": "string", "description": "Profile sketch/curve name (closed)"},
                        "center_curve": {"type": "string", "description": "Center curve sketch/curve name"},
                    },
                    "required": ["profile", "center_curve"],
                },
            },
            {
                "name": "catia_slot",
                "description": (
                    "Create a Slot: sweep a closed profile along a center curve, removing "
                    "material. The removed volume is verified; a slot that removes no "
                    "material fails with FEATURE_NO_EFFECT (the slot stays in the tree)."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "profile": {"type": "string", "description": "Profile sketch/curve name (closed)"},
                        "center_curve": {"type": "string", "description": "Center curve sketch/curve name"},
                    },
                    "required": ["profile", "center_curve"],
                },
            },
            {
                "name": "catia_add_body",
                "description": (
                    "Add a new Body to the active part and make it the active body: new "
                    "sketches and features go into it until catia_set_active_body changes "
                    "it. Combine bodies with catia_boolean."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Optional body name"},
                    },
                },
            },
            {
                "name": "catia_set_active_body",
                "description": (
                    "Choose the body that receives new sketches and features (by name, "
                    "e.g. 'Body.2'). Omit body_name to go back to the part's main body."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "body_name": {"type": "string", "description": "Body name; omit for the main body"},
                    },
                },
            },
            {
                "name": "catia_boolean",
                "description": (
                    "Boolean operation of a body into a target body (default: the part's "
                    "main body): add (union), remove (subtract), intersect, or assemble. "
                    "Reports the target volume before and after."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "operation": {
                            "type": "string",
                            "enum": ["add", "remove", "intersect", "assemble"],
                            "description": "Boolean operation",
                        },
                        "body_name": {"type": "string", "description": "Body operated into the target"},
                        "target_body": {
                            "type": "string",
                            "description": "Body receiving the operation (default: main body)",
                        },
                    },
                    "required": ["operation", "body_name"],
                },
            },
            {
                "name": "catia_rename_feature",
                "description": (
                    "Rename a feature, sketch, body, geometrical set or GSD element of the "
                    "active part."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Current name (e.g. 'Pad.1')"},
                        "new_name": {"type": "string", "description": "New name"},
                    },
                    "required": ["name", "new_name"],
                },
            },
            {
                "name": "catia_set_feature_active",
                "description": (
                    "Activate or deactivate (suppress) a feature of the active part, then "
                    "update the part. Deactivated features stay in the tree."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Feature name (e.g. 'Pocket.2')"},
                        "active": {"type": "boolean", "description": "true = activate, false = deactivate"},
                    },
                    "required": ["name", "active"],
                },
            },
            {
                "name": "catia_set_visibility",
                "description": "Show or hide elements of the active part (bodies, features, sketches, GSD elements, geometrical sets).",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "names": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "description": "Element names",
                        },
                        "visible": {"type": "boolean", "description": "true = show, false = hide"},
                    },
                    "required": ["names", "visible"],
                },
            },
            {
                "name": "catia_set_color",
                "description": "Set the display color (RGB 0-255) of elements of the active part.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "names": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "description": "Element names (bodies, features, GSD elements)",
                        },
                        "rgb": {
                            "type": "array",
                            "items": {"type": "integer", "minimum": 0, "maximum": 255},
                            "minItems": 3,
                            "maxItems": 3,
                            "description": "[red, green, blue], 0-255",
                        },
                    },
                    "required": ["names", "rgb"],
                },
            },
        ]

    def execute(self, tool_name: str, arguments: dict[str, Any]) -> str:
        self._auto_closed_sketch = None
        try:
            result = self._dispatch(tool_name, arguments)
        except CATIAToolError as error:
            note = self._auto_close_note()
            if note:
                # The sketch was closed before the failure: CATIA changed.
                error.args = (f"{error} {note}",)
                if error.modified is False:
                    error.modified = None
            raise
        note = self._auto_close_note()
        return f"{result} {note}" if note else result

    def _auto_close_note(self) -> str:
        name, self._auto_closed_sketch = self._auto_closed_sketch, None
        if not name:
            return ""
        return (
            f"Note: sketch '{name}' was still open in the sketcher; it was closed "
            "automatically before building the feature (call catia_close_sketch "
            "after drawing to avoid this)."
        )

    def _close_open_sketch(self) -> Any | None:
        """Close the sketch the sketcher left in edition, if any.

        A solid feature built while a sketch is still in edition can use an
        unclosed profile. The sketcher records its open sketch on the shared
        connection (``connection.open_sketch``); close it the way
        catia_close_sketch does (CloseEdition, then UpdateObject) and return
        it so a call without ``sketch_name`` builds on the sketch that was
        just drawn. Needs live validation: closing a sketch from Part Design
        tools rather than catia_close_sketch is not field-proven.
        """
        sketch = getattr(self.conn, "open_sketch", None)
        if sketch is None:
            return None
        self.conn.open_sketch = None
        try:
            name = str(sketch.Name)
        except Exception:
            name = "sketch"
        try:
            sketch.CloseEdition()
        except Exception as e:
            # Still open in CATIA: keep publishing it, or the sketcher would
            # report "already closed" and refuse to close it on the retry.
            self.conn.open_sketch = sketch
            raise CATIAToolError(
                f"Sketch '{name}' was still open in the sketcher and could not be closed "
                f"automatically ({e}). Close it with catia_close_sketch, then retry.",
                modified=None,
            ) from e
        try:
            self.conn.get_active_part().UpdateObject(sketch)
        except Exception:
            pass
        self._auto_closed_sketch = name
        return sketch

    def _profile_sketch(self, sketch_name: str | None) -> Any:
        """The sketch a profile-based feature builds on: the named one, else
        the sketch just closed automatically, else the body's last sketch."""
        closed = self._close_open_sketch()
        if closed is not None and not sketch_name:
            return closed
        return self._get_last_sketch(sketch_name)

    def _dispatch(self, tool_name: str, arguments: dict[str, Any]) -> str:
        match tool_name:
            case "catia_pad":
                return self._pad(arguments)
            case "catia_pocket":
                return self._pocket(arguments)
            case "catia_shaft":
                return self._shaft(arguments)
            case "catia_groove":
                return self._groove(arguments)
            case "catia_fillet":
                return self._fillet(arguments)
            case "catia_chamfer":
                return self._chamfer(arguments)
            case "catia_hole":
                return self._hole(arguments)
            case "catia_rect_pattern":
                return self._rect_pattern(arguments)
            case "catia_user_pattern":
                return self._user_pattern(arguments)
            case "catia_circ_pattern":
                return self._circ_pattern(arguments)
            case "catia_mirror":
                return self._mirror(arguments)
            case "catia_shell":
                return self._shell(arguments)
            case "catia_draft":
                return self._draft(arguments)
            case "catia_thickness":
                return self._thickness(arguments)
            case "catia_delete_feature":
                return self._delete_feature(arguments)
            case "catia_list_features":
                return self._list_features()
            case "catia_list_faces":
                return self._list_faces(arguments.get("body_name"))
            case "catia_list_edges":
                return self._list_edges()
            case "catia_rib":
                return self._sweep_feature("rib", arguments)
            case "catia_slot":
                return self._sweep_feature("slot", arguments)
            case "catia_add_body":
                return self._add_body(arguments.get("name"))
            case "catia_set_active_body":
                return self._set_active_body(arguments.get("body_name"))
            case "catia_boolean":
                return self._boolean(arguments)
            case "catia_rename_feature":
                return self._rename(arguments["name"], arguments["new_name"])
            case "catia_set_feature_active":
                return self._set_feature_active(arguments["name"], bool(arguments["active"]))
            case "catia_set_visibility":
                return self._set_visibility(arguments["names"], bool(arguments["visible"]))
            case "catia_set_color":
                return self._set_color(arguments["names"], arguments["rgb"])
            case _:
                raise ValueError(f"Unknown part design tool: {tool_name}")

    def _get_last_sketch(self, sketch_name: str | None = None) -> Any:
        """Get a sketch by name or the last sketch in the body."""
        body = self.conn.get_active_part_body()
        sketches = body.Sketches

        if sketch_name:
            try:
                return sketches.Item(sketch_name)
            except Exception:
                names = ", ".join(str(sketches.Item(i).Name) for i in range(1, sketches.Count + 1))
                raise ObjectNotFound(
                    f"No sketch named '{sketch_name}' in the active body. "
                    f"Available: {names or 'none'}."
                ) from None

        # Get the last sketch
        if sketches.Count == 0:
            raise ObjectNotFound("No sketches found in the active body. Create a sketch first.")
        return sketches.Item(sketches.Count)

    def _get_last_shape(self, feature_name: str | None = None) -> Any:
        """Get a shape/feature by name or the last one in the body."""
        body = self.conn.get_active_part_body()
        shapes = body.Shapes

        if feature_name:
            try:
                return shapes.Item(feature_name)
            except Exception:
                names = ", ".join(str(shapes.Item(i).Name) for i in range(1, shapes.Count + 1))
                raise ObjectNotFound(
                    f"No feature named '{feature_name}' in the active body. "
                    f"Available: {names or 'none'}."
                ) from None

        if shapes.Count == 0:
            raise ObjectNotFound("No features found in the active body.")
        return shapes.Item(shapes.Count)

    def _pad(self, args: dict[str, Any]) -> str:
        self.conn.ensure_connected()
        height = args["height"]
        direction = args.get("direction", "normal")
        symmetric = bool(args.get("symmetric", False))
        if not height > 0:
            raise InvalidArgument(
                "height must be positive (mm); use direction='reverse' to extrude "
                "the other way."
            )
        part = self.conn.get_active_part()
        body = self.conn.get_active_part_body()
        sf = part.ShapeFactory

        sketch = self._profile_sketch(args.get("sketch_name"))
        # V5-6R2020: solid features fail with E_FAIL unless the sketch's
        # owning BODY is the in-work object (fresh documents start on a
        # Geometrical Set).
        _force_solid_inwork(part, sketch)
        before = self._volume_before_feature(part, body)

        pad = sf.AddNewPad(sketch, height)

        # Direct PUTs can fail on factory-returned objects under dynamic
        # dispatch (B30 evidence, see _set_prop); a pad whose extent did not
        # take would silently be one-sided in the wrong place.
        if symmetric or direction == "both":
            prop, value, wanted = "IsSymmetric", True, "symmetric extent"
        elif direction == "reverse":
            prop, value, wanted = "DirectionOrientation", 1, "reversed direction"  # catReverse
        else:
            prop = None
        if prop and not _set_prop(pad, prop, value):
            raise CATIAToolError(
                f"Pad '{pad.Name}' was created but CATIA rejected the {wanted} ({prop}), "
                "so it extrudes one-sided along the sketch normal. It remains in the "
                "tree: delete it with catia_delete_feature, then retry or set the "
                "extent in CATIA.",
                code="WRONG_GEOMETRY_EFFECT",
                modified=None,
            )

        self._safe_update(part, pad)
        self.conn.refresh_display()

        extent = f"{height} mm ({'symmetric' if symmetric else direction})"
        after = self._body_volume_mm3(part, body)
        if before is None or after is None:
            return (
                f"Pad '{pad.Name}' created: {extent} (volume verification "
                "unavailable on this installation)."
            )
        added = after - before
        # Re-measuring an unchanged solid can drift by rounding noise; a pad
        # buried in existing material must not pass as "Added 0.0 mm3".
        if added <= max(self._PAD_NO_EFFECT_MM3, self._PAD_NO_EFFECT_FRACTION * before):
            raise CATIAToolError(
                f"Pad '{pad.Name}' was created in the tree but added no material "
                f"(volume {before:.1f} -> {after:.1f} mm3); check that the extrusion "
                "does not lie entirely inside existing material. Delete it with "
                "catia_delete_feature.",
                code="FEATURE_NO_EFFECT",
                modified=None,
            )
        return (
            f"Pad '{pad.Name}' created: {extent}. "
            f"Added {added:.1f} mm3 ({before:.1f} -> {after:.1f})."
        )

    # A pad adding no more than this (absolute mm3, or fraction of the body
    # volume before it) had no effect: above re-measurement noise, below any
    # real extrusion. Not field-calibrated (needs live validation).
    _PAD_NO_EFFECT_MM3 = 1e-3
    _PAD_NO_EFFECT_FRACTION = 1e-7

    def _body_volume_mm3(self, part: Any, body: Any) -> float | None:
        """Measure the body's solid volume in mm3, or None if unavailable."""
        return self.measure.body_volume_mm3(part, body)

    def _volume_before_feature(self, part: Any, body: Any) -> float | None:
        """Body volume before an additive feature. A body without solid
        features holds no material (0 mm3) even where measuring an empty
        body fails, so the first pad of a body is verified too."""
        try:
            if body.Shapes.Count == 0:
                return 0.0
        except Exception:
            pass
        return self._body_volume_mm3(part, body)

    def _pocket(self, args: dict[str, Any]) -> str:
        self.conn.ensure_connected()
        part = self.conn.get_active_part()
        body = self.conn.get_active_part_body()
        sf = part.ShapeFactory

        sketch = self._profile_sketch(args.get("sketch_name"))
        # V5-6R2020: see _pad - the sketch's owning body must be in-work.
        _force_solid_inwork(part, sketch)
        depth = args["depth"]
        reverse = args.get("direction") == "reverse"
        symmetric = args.get("symmetric", False)

        before = self._body_volume_mm3(part, body)

        pocket = sf.AddNewPocket(sketch, depth)
        if symmetric:
            # Mirrored extent: cuts equally on both sides. Reliable on
            # V5-6R2020, where DirectionOrientation is accepted but may not
            # flip the cut side. Set through the parameter-object fallback:
            # direct PUTs can fail on factory-returned objects.
            if not _set_prop(pocket, "IsSymmetric", True):
                raise CATIAToolError(
                    f"Pocket '{pocket.Name}' was created but CATIA rejected the symmetric "
                    "extent, so it is one-sided. Delete it with catia_delete_feature or "
                    "retry without 'symmetric'.",
                    code="WRONG_GEOMETRY_EFFECT",
                    modified=None,
                )
        else:
            # A pocket cuts on one side of the profile's support plane only,
            # so the orientation is set explicitly in both cases
            # (field-verified: the wrong side produces a tree feature that
            # removes nothing).
            pocket.DirectionOrientation = 1 if reverse else 0
        part.UpdateObject(pocket)

        explicit_direction = "direction" in args
        flipped = False
        after = self._body_volume_mm3(part, body)
        if (
            before is not None
            and after is not None
            and after >= before
            and not explicit_direction
            and not symmetric
        ):
            # No material removed and the caller did not pin the side: flip
            # and re-verify instead of reporting a false success. An explicit
            # 'direction' is honored as-is and reported honestly below.
            pocket.DirectionOrientation = 0 if reverse else 1
            part.UpdateObject(pocket)
            new_after = self._body_volume_mm3(part, body)
            if new_after is not None and new_after < before:
                after = new_after
                flipped = True
            else:
                # The flip did not help either: restore the requested
                # orientation so the tree reflects the original intent.
                pocket.DirectionOrientation = 1 if reverse else 0
                part.UpdateObject(pocket)
                after = self._body_volume_mm3(part, body)

        self.conn.refresh_display()

        if before is not None and after is not None:
            removed = before - after
            if removed <= 0:
                raise CATIAToolError(
                    f"Pocket '{pocket.Name}' was created in the tree but removed no material"
                    + (" in the requested direction" if explicit_direction
                       else " in either direction")
                    + f" (volume stayed {before:.1f} mm3). Check that the profile "
                    "intersects the solid, then delete the pocket with catia_delete_feature.",
                    code="FEATURE_NO_EFFECT",
                    modified=None,
                )
            flip_note = " (direction auto-flipped to reach material)" if flipped else ""
            return (
                f"Pocket '{pocket.Name}' created: {depth} mm deep{flip_note}. "
                f"Removed {removed:.1f} mm3 ({before:.1f} -> {after:.1f})."
            )
        return (
            f"Pocket created: {depth} mm deep. Feature: '{pocket.Name}' "
            "(volume verification unavailable on this installation)."
        )

    @staticmethod
    def _check_revolution_angle(angle: Any) -> None:
        if not 0 < angle <= 360:
            raise InvalidArgument("angle must be in (0, 360] degrees.")

    def _shaft(self, args: dict[str, Any]) -> str:
        self.conn.ensure_connected()
        angle = args.get("angle", 360)
        self._check_revolution_angle(angle)
        part = self.conn.get_active_part()
        sf = part.ShapeFactory

        sketch = self._profile_sketch(args.get("sketch_name"))

        # V5-6R2020: see _pad - the sketch's owning body must be in-work.
        _force_solid_inwork(part, sketch)
        shaft = sf.AddNewShaft(sketch)
        # V5-6R2020: drive the revolution axis from the sketch's own reference
        # frame (AbsoluteAxis.VDirection); a drawn-line axis raises E_FAIL and
        # wrapping the sketch raises DISP_E_BADTYPE. FirstAngle goes through
        # the parameter-object fallback (direct PUTs fail on factory-returned
        # objects under dynamic dispatch).
        abs_axis = sketch.GeometricElements.Item("AbsoluteAxis")
        vdir = abs_axis.GetItem("VDirection")
        shaft.RevoluteAxis = part.CreateReferenceFromObject(vdir)
        if not _set_prop(shaft, "FirstAngle", angle):
            raise CATIAToolError(
                f"Shaft '{shaft.Name}' was created but its angle could not be set to "
                f"{angle} degrees. Delete it with catia_delete_feature before retrying.",
                code="WRONG_GEOMETRY_EFFECT",
                modified=None,
            )

        self._safe_update(part, shaft, full_update_fallback=True)
        self.conn.refresh_display()
        return f"Shaft (revolution) created: {angle}°. Feature: '{shaft.Name}'"

    def _groove(self, args: dict[str, Any]) -> str:
        self.conn.ensure_connected()
        angle = args.get("angle", 360)
        self._check_revolution_angle(angle)
        part = self.conn.get_active_part()
        sf = part.ShapeFactory

        sketch = self._profile_sketch(args.get("sketch_name"))

        # V5-6R2020: see _pad - the sketch's owning body must be in-work.
        _force_solid_inwork(part, sketch)
        groove = sf.AddNewGroove(sketch)
        # V5-6R2020: revolution axis from the sketch's AbsoluteAxis.VDirection
        # (see _shaft); FirstAngle through the parameter-object fallback.
        abs_axis = sketch.GeometricElements.Item("AbsoluteAxis")
        vdir = abs_axis.GetItem("VDirection")
        groove.RevoluteAxis = part.CreateReferenceFromObject(vdir)
        if not _set_prop(groove, "FirstAngle", angle):
            raise CATIAToolError(
                f"Groove '{groove.Name}' was created but its angle could not be set to "
                f"{angle} degrees. Delete it with catia_delete_feature before retrying.",
                code="WRONG_GEOMETRY_EFFECT",
                modified=None,
            )

        self._safe_update(part, groove, full_update_fallback=True)
        self.conn.refresh_display()
        return f"Groove (revolution cut) created: {angle}°. Feature: '{groove.Name}'"

    def _safe_update(self, part: Any, feature: Any, *, full_update_fallback: bool = False) -> None:
        """Update a feature; if CATIA rejects it, remove the broken feature
        from the tree so a failed attempt does not poison later features.

        ``full_update_fallback`` keeps the V5-6R2020 (B30) recipe of Shaft,
        Groove and Hole: when UpdateObject raises, a full ``part.Update()``
        is tried before the feature is declared broken.

        Raises CATIAToolError(FEATURE_UPDATE_FAILED) saying whether the
        feature was removed or still needs catia_delete_feature.
        """
        try:
            part.UpdateObject(feature)
            return
        except Exception as e:
            error = e
        if full_update_fallback:
            try:
                part.Update()
                return
            except Exception as e:
                error = e
        self._raise_update_failed(feature, error)

    def _remove_feature(self, feature: Any) -> bool:
        """Delete one feature through the selection; True when it went."""
        selection = None
        try:
            selection = self.conn.hso
            selection.Clear()
            selection.Add(feature)
            selection.Delete()
            return True
        except Exception:
            return False
        finally:
            if selection is not None:
                try:
                    selection.Clear()
                except Exception:
                    pass

    def _raise_update_failed(self, feature: Any, error: Exception) -> None:
        try:
            name = str(feature.Name)
        except Exception:
            name = "the feature"
        if self._remove_feature(feature):
            outcome = (
                "it was removed from the tree, so no broken feature is left behind. "
                "Check the inputs (edge/face choice, sizes, profile) and retry."
            )
        else:
            outcome = (
                "it could NOT be removed automatically and remains in the tree in "
                f"error: delete '{name}' with catia_delete_feature before continuing."
            )
        raise CATIAToolError(
            f"CATIA rejected '{name}' (update failed: {error}); {outcome}",
            code="FEATURE_UPDATE_FAILED",
            modified=None,
        ) from error

    def _topo_reference(self, kind: str, index: int) -> Any:
        """Resolve Face.N / Edge.N on the active body's final shape to a
        Reference (indices match catia_list_faces / catia_list_edges)."""
        return topology_reference(self.conn, kind, index)

    @staticmethod
    def _topo_index(name: str, kind: str) -> int:
        return topology_index(name, kind)

    def _fillet(self, args: dict[str, Any]) -> str:
        self.conn.ensure_connected()
        radius = args["radius"]
        if not radius > 0:
            raise InvalidArgument("radius must be positive (mm).")
        part = self.conn.get_active_part()
        sf = part.ShapeFactory

        edge_name = args.get("edge_name")
        if self.conn.is_3dx:
            edge_refs = self._fillet_edges_3dx(edge_name)
            if edge_refs:
                return self._fillet_3dx(part, sf, radius, edge_refs, edge_name)
            # No edge cell found at all: fall back to the whole-feature
            # reference below. It failed live on R428 SP6 (2026-11-02 field
            # report), so expect CATIA's own error; it is kept because it is
            # the only remaining route and nothing has been created yet.
        if edge_name:
            target = self._topo_reference("Edge", self._topo_index(edge_name, "Edge"))
            scope = edge_name
        else:
            # Whole-feature fallback: a Reference to the feature fillets its
            # edges with tangency propagation.
            target = part.CreateReferenceFromObject(self._get_last_shape())
            scope = "all edges of the last feature"

        fillet = sf.AddNewSolidEdgeFilletWithConstantRadius(
            target,
            1,       # catTangencyFilletEdgePropagation
            radius,
        )

        # A radius larger than the adjacent faces allow fails on update; the
        # broken fillet is removed instead of poisoning every later feature.
        self._safe_update(part, fillet)
        self.conn.refresh_display()
        return f"Fillet created: R{radius} mm on {scope}. Feature: '{fillet.Name}'"

    def _fillet_edges_3dx(self, edge_name: str | None) -> list[Any]:
        """Edge References for a 3DEXPERIENCE fillet, from the same topology
        search catia_list_edges enumerates (references.search_final_topology).

        Live on R428 SP6 (2026-11-02 field report), both the whole-feature
        reference (CreateReferenceFromObject(Pad)) and the old Edge.1 (from a
        body search that also returned sketch edges) made
        AddNewSolidEdgeFilletWithConstantRadius fail. Empty list: the search
        found no edge at all. A failing (non-probe) search propagates its COM
        error: nothing has been created yet.
        """
        if edge_name:
            index = self._topo_index(edge_name, "Edge")
            return [topology_reference(self.conn, "Edge", index)]
        return topology_references(self.conn, "Edge")

    def _fillet_3dx(
        self, part: Any, sf: Any, radius: float, refs: list[Any], edge_name: str | None
    ) -> str:
        """Field-proven on R428 SP6 (2026-11-02 field report): create the
        fillet on the first topology-search edge with tangency propagation
        (1 = catTangencyFilletEdgePropagation), add every other edge with
        AddObjectToFillet, then update."""
        fillet = sf.AddNewSolidEdgeFilletWithConstantRadius(refs[0], 1, radius)
        added = 1
        try:
            for ref in refs[1:]:
                fillet.AddObjectToFillet(ref)
                added += 1
        except Exception as error:
            # The fillet exists but holds only some edges: remove it rather
            # than leave a partial, un-updated feature in the tree.
            self._raise_update_failed(
                fillet,
                RuntimeError(f"AddObjectToFillet refused edge {added + 1} of {len(refs)}: {error}"),
            )
        self._safe_update(part, fillet)
        self.conn.refresh_display()
        scope = edge_name if edge_name else f"all {len(refs)} edges of the last feature"
        return f"Fillet created: R{radius} mm on {scope}. Feature: '{fillet.Name}'"

    def _chamfer(self, args: dict[str, Any]) -> str:
        self.conn.ensure_connected()
        length = args["length"]
        angle = args.get("angle", 45)
        if not length > 0:
            raise InvalidArgument("length must be positive (mm).")
        if not 0 < angle < 90:
            raise InvalidArgument("angle must be strictly between 0 and 90 degrees.")
        part = self.conn.get_active_part()
        sf = part.ShapeFactory

        edge_name = args.get("edge_name")
        if edge_name:
            target = self._topo_reference("Edge", self._topo_index(edge_name, "Edge"))
            scope = edge_name
        else:
            target = part.CreateReferenceFromObject(self._get_last_shape())
            scope = "all edges of the last feature"

        # AddNewChamfer(iObjectToChamfer, iPropagation, iMode, iOrientation,
        #               iLength1, iLength2OrAngle) - 6 parameters.
        chamfer = sf.AddNewChamfer(
            target,
            1,       # catTangencyChamferPropagation
            0,       # catLengthAngleChamfer mode
            0,       # catNoReverseChamfer orientation
            length,
            angle,
        )

        self._safe_update(part, chamfer)
        self.conn.refresh_display()
        return f"Chamfer created: {length} mm at {angle} deg on {scope}. Feature: '{chamfer.Name}'"

    # A hole removing less than this fraction of its nominal cylinder
    # (pi/4 * d^2 * depth) had no effect: far above the measurement noise of
    # an unchanged solid, far below what any hole entering the solid removes.
    _HOLE_NO_EFFECT_FRACTION = 1e-3

    def _hole_thread_args(self, args: dict[str, Any], diameter: float, depth: float | None,
                          through_all: bool) -> dict[str, Any] | None:
        """Validate the thread options before any CATIA call; None = smooth."""
        standard = args.get("thread_standard")
        designation = args.get("designation")
        thread_depth = args.get("thread_depth")
        threaded = bool(args.get("threaded", False)) or any(
            v is not None for v in (standard, designation, thread_depth)
        )
        if not threaded:
            return None
        standard = standard or "metric_coarse"
        if standard not in _THREAD_STANDARDS:
            raise InvalidArgument(
                f"Unknown thread_standard '{standard}'. Use one of: {', '.join(_THREAD_STANDARDS)}."
            )
        if designation is None:
            # "M<d>" is a coarse-table name; a fine table lists "M8x1" etc.
            if standard != "metric_coarse":
                raise InvalidArgument(
                    f"thread_standard '{standard}' needs a 'designation' as listed in "
                    "CATIA's thread table (e.g. 'M8x1' for metric_fine, '1/4-20' for unc)."
                )
            designation = f"M{diameter:g}"
        designation = str(designation).strip()
        if not designation:
            raise InvalidArgument("designation must not be empty.")
        if thread_depth is None:
            if depth is None:
                raise InvalidArgument(
                    "A through-all threaded hole needs 'thread_depth' (or 'depth')."
                )
            thread_depth = depth
        if not thread_depth > 0:
            raise InvalidArgument("thread_depth must be positive (mm).")
        if depth is not None and not through_all and thread_depth > depth + 1e-9:
            raise InvalidArgument(
                f"thread_depth {thread_depth:g} mm exceeds the hole depth {depth:g} mm."
            )
        return {
            "standard": standard,
            "code": _THREAD_STANDARDS[standard],
            "designation": designation,
            "depth": float(thread_depth),
        }

    _SNAPSHOT_POINTS = 20

    def _sketch_snapshot(self, sketch: Any) -> dict[str, Any]:
        """Name, axis system and points of a hole's input sketch, captured
        before AddNewHoleFromSketch absorbs it. Deleting a rejected hole
        deletes that sketch with it (2026-11-02 field report: the rollback
        removed hole and sketch, the retry got OBJECT_NOT_FOUND), so the
        failure message carries what is needed to recreate it.

        Point2D.GetCoordinates and Sketch.GetAbsoluteAxisData are SAFEARRAY
        out-parameter methods (read through SystemService.Evaluate, see
        automation.call_fill_array). UNVERIFIED live on these two methods;
        anything unreadable is simply left out.
        """
        snapshot: dict[str, Any] = {"name": None, "points": [], "axis": None}
        try:
            snapshot["name"] = str(sketch.Name)
        except Exception:
            pass
        app = getattr(self.conn, "app", None)
        try:
            snapshot["axis"] = call_fill_array(app, sketch, "GetAbsoluteAxisData", 9)
        except Exception:
            pass
        try:
            elements = list(iter_items(sketch.GeometricElements))
        except Exception:
            elements = []
        # Only elements that expose GetCoordinates (Point2D) go through
        # Evaluate: no script is run that is known to fail (axis, lines),
        # and a large sketch costs at most _SNAPSHOT_POINTS calls.
        for element in elements:
            if len(snapshot["points"]) >= self._SNAPSHOT_POINTS:
                break
            if com_name(element, "") == "AbsoluteAxis" or not name_exists(element, "GetCoordinates"):
                continue
            try:
                snapshot["points"].append(tuple(call_fill_array(app, element, "GetCoordinates", 2)))
            except Exception:
                pass
        return snapshot

    def _sketch_note(self, snapshot: dict[str, Any], body: Any) -> str:
        """What happened to the hole's input sketch after the hole was removed."""
        name = snapshot.get("name")
        if not name:
            return ""
        try:
            body.Sketches.Item(name)
            return f"Its input sketch '{name}' was kept; retry with sketch_name='{name}'."
        except Exception:
            pass
        note = (
            f"CATIA deleted its input sketch '{name}' with it (a hole absorbs its "
            "sketch); recreate it with catia_create_sketch and catia_sketch_point"
        )
        points = snapshot.get("points") or []
        if not points:
            return note + " (its point coordinates could not be read)."
        axis = snapshot.get("axis")
        described = []
        for x, y in points:
            text = f"({x:g}, {y:g})"
            if axis:
                o, h, v = axis[0:3], axis[3:6], axis[6:9]
                xyz = [o[i] + x * h[i] + y * v[i] for i in range(3)]
                text += " = 3D ({:g}, {:g}, {:g})".format(*xyz)
            described.append(text)
        where = ""
        if axis:
            where = (
                " on the plane with origin ({:g}, {:g}, {:g}), H ({:g}, {:g}, {:g}), "
                "V ({:g}, {:g}, {:g})".format(*axis)
            )
        return f"{note}{where}: sketch point(s) {', '.join(described)} mm."

    def _hole(self, args: dict[str, Any]) -> str:
        self.conn.ensure_connected()
        diameter = args["diameter"]
        depth = args.get("depth")
        through_all = bool(args.get("through_all", False))
        if not diameter > 0:
            raise InvalidArgument("diameter must be positive (mm).")
        if depth is None and not through_all:
            raise InvalidArgument("depth is required unless through_all is true.")
        if depth is not None and not depth > 0:
            raise InvalidArgument("diameter and depth must be positive (mm).")
        hole_type = args.get("type", "simple")
        # CatHoleType: 0 simple, 1 tapered, 2 counterbored, 3 countersunk.
        type_map = {"simple": 0, "tapered": 1, "counterbored": 2, "countersunk": 3}
        if hole_type not in type_map:
            raise InvalidArgument(
                f"Unknown hole type '{hole_type}'. Use one of: {', '.join(type_map)}."
            )
        thread = self._hole_thread_args(args, diameter, depth, through_all)
        part = self.conn.get_active_part()
        body = self.conn.get_active_part_body()
        sf = part.ShapeFactory

        sketch = self._profile_sketch(args.get("sketch_name"))
        snapshot = self._sketch_snapshot(sketch)
        before = self._body_volume_mm3(part, body)

        # V5-6R2020: see _pad - the sketch's owning body must be in-work.
        _force_solid_inwork(part, sketch)
        # AddNewHole takes a support FACE reference; the sketch-positioned
        # form is AddNewHoleFromSketch. Diameter is a read-only Length
        # parameter object whose .Value is assigned.
        hole = sf.AddNewHoleFromSketch(sketch, depth if depth is not None else 10.0)
        hole.Diameter.Value = diameter
        if through_all:
            # UNVERIFIED live: bottom limit "up to last" (CatLimitMode 2).
            applied = _set_prop(hole.BottomLimit, "LimitMode", _UP_TO_LAST)
            if applied:
                try:
                    applied = int(hole.BottomLimit.LimitMode) == _UP_TO_LAST
                except Exception:
                    pass
            if not applied:
                raise CATIAToolError(
                    f"Hole '{hole.Name}' was created but CATIA rejected the 'up to last' "
                    "bottom limit, so it is not through-all. It remains in the tree; "
                    "delete it with catia_delete_feature, then retry with a depth.",
                    code="WRONG_GEOMETRY_EFFECT",
                    modified=None,
                )
            applied_depth = None
        else:
            # 3DEXPERIENCE R428 ignores AddNewHoleFromSketch's depth (live: the
            # bottom limit read 10 after asking for 20), so it is set on the
            # bottom limit explicitly, as V5 recorded macros do, and read back.
            hole.BottomLimit.Dimension.Value = depth
            applied_depth = float(hole.BottomLimit.Dimension.Value)
            if abs(applied_depth - depth) > 1e-6:
                raise CATIAToolError(
                    f"Hole '{hole.Name}' kept a depth of "
                    f"{applied_depth:g} mm after {depth:g} mm was set on its bottom limit. "
                    "It remains in the tree; delete it with catia_delete_feature.",
                    code="WRONG_GEOMETRY_EFFECT",
                    modified=None,
                )
        hole.BottomType = 0  # catFlatBottom

        if thread is None:
            # 2026-11-02 field report: a new hole inherited the previous hole's
            # thread settings. A smooth hole is made smooth explicitly
            # (CatHoleThreadingMode smooth = 1), before the first update so a
            # carried-over thread cannot make that update fail. Only written
            # when it does not already read smooth (no-op otherwise).
            self._reset_smooth(hole)

        type_note = ""
        if hole_type != "simple":
            wanted = type_map[hole_type]
            applied = _set_prop(hole, "Type", wanted)
            if applied:
                # Read back: a PUT CATIA accepts but coerces is still a
                # different hole. Unreadable (parameter object): trust the PUT.
                try:
                    applied = int(hole.Type) == wanted
                except Exception:
                    pass
            if not applied:
                # Previously reported as success with a silent simple hole.
                raise CATIAToolError(
                    f"Hole '{hole.Name}' was created as a simple hole: CATIA rejected the "
                    f"requested type '{hole_type}'. It remains in the tree; delete it with "
                    "catia_delete_feature, then retry with type 'simple' or set the type "
                    "in CATIA.",
                    code="WRONG_GEOMETRY_EFFECT",
                    modified=None,
                )
            type_note = (
                f" Type: {hole_type} (head dimensions keep CATIA "
                "defaults; adjust in the tree if needed)."
            )

        # One update (it used to run twice); the full part.Update() fallback
        # is the V5-6R2020 recipe, and a hole CATIA still rejects is removed.
        # Only the hole is put in the selection; CATIA deletes the sketch it
        # absorbed with it, which the note reports (with the sketch points).
        self._hole_update(part, hole, body, snapshot)
        nominal_depth = depth if depth is not None else 1.0
        threshold = self._HOLE_NO_EFFECT_FRACTION * math.pi * diameter**2 * nominal_depth / 4.0
        after = self._body_volume_mm3(part, body)
        reversed_note = ""
        if before is not None and after is not None and before - after <= threshold:
            # Live R428: a hole sketched on the XY origin plane at the pad's
            # bottom face drilled outward, into air. Reverse once, re-measure.
            hole.Reverse()
            part.UpdateObject(hole)
            after = self._body_volume_mm3(part, body)
            reversed_note = " (direction reversed to reach material)"
            if after is not None and before - after <= threshold:
                raise CATIAToolError(
                    f"Hole '{hole.Name}' was created in the tree but removed no material "
                    f"in either direction (volume {before:.1f} -> {after:.1f} mm3; the hole "
                    "is left reversed). Check that the sketch point lies over the solid, "
                    "then delete the hole with catia_delete_feature.",
                    code="FEATURE_NO_EFFECT",
                    modified=None,
                )

        if thread is not None:
            type_note += self._apply_thread(part, hole, body, snapshot, thread)
        self.conn.refresh_display()
        try:
            final_diameter = float(hole.Diameter.Value)
        except Exception:
            final_diameter = float(diameter)
        extent = "through all" if applied_depth is None else f"depth {applied_depth:g} mm"
        summary = f"Hole '{hole.Name}' created: D{final_diameter:g} mm, {extent}{reversed_note}."
        if before is None or after is None:
            return f"{summary} Volume verification unavailable on this installation.{type_note}"
        return (
            f"{summary} Removed {before - after:.1f} mm3 ({before:.1f} -> {after:.1f})."
            f"{type_note}"
        )

    def _hole_update(self, part: Any, hole: Any, body: Any, snapshot: dict[str, Any]) -> None:
        try:
            part.UpdateObject(hole)
            return
        except Exception as e:
            error = e
        try:
            part.Update()
            return
        except Exception as e:
            error = e
        # Only now (after the removal) can we tell whether the sketch went too.
        try:
            name = str(hole.Name)
        except Exception:
            name = "the hole"
        if self._remove_feature(hole):
            outcome = (
                "it was removed from the tree, so no broken feature is left behind. "
                "Check the inputs (sizes, thread, sketch point) and retry. "
                + self._sketch_note(snapshot, body)
            ).rstrip()
        else:
            outcome = (
                "it could NOT be removed automatically and remains in the tree in "
                f"error: delete '{name}' with catia_delete_feature before continuing."
            )
        raise CATIAToolError(
            f"CATIA rejected '{name}' (update failed: {error}); {outcome}",
            code="FEATURE_UPDATE_FAILED",
            modified=None,
        ) from error

    @staticmethod
    def _reset_smooth(hole: Any) -> None:
        # Fail only when CATIA positively reads "threaded" afterwards: a
        # release whose ThreadingMode is unreadable and unwritable made plain
        # holes before this reset existed, and must keep doing so.
        try:
            current: int | None = int(hole.ThreadingMode)
        except Exception:
            current = None
        if current == _SMOOTH:
            return
        _set_prop(hole, "ThreadingMode", _SMOOTH)
        try:
            still_threaded = int(hole.ThreadingMode) != _SMOOTH
        except Exception:
            still_threaded = False
        if still_threaded:
            raise CATIAToolError(
                f"Hole '{hole.Name}' was created but CATIA kept the threading it carried "
                "over from the previous hole (ThreadingMode could not be set to smooth). "
                "It remains in the tree; delete it with catia_delete_feature, or retry "
                "with threaded=true and explicit thread values.",
                code="WRONG_GEOMETRY_EFFECT",
                modified=None,
            )

    def _apply_thread(
        self, part: Any, hole: Any, body: Any, snapshot: dict[str, Any], thread: dict[str, Any]
    ) -> str:
        """Thread an updated hole from a standard table, setting every thread
        field so nothing carries over from the previous hole.

        Field-proven on R428 SP6 (2026-11-02 field report):
        CreateStandardThreadDesignTable(1) (metric coarse),
        HoleThreadDescription.Value = "M6", ThreadDepth.Value = depth; CATIA
        then set the tap drill (diameter 4.917). V5-6R2020: threading a hole
        that has not materialized yet breaks the rebuild, so this runs AFTER
        the first update. B30 type libraries: threaded = 0 (1 = smooth).
        """
        name = str(hole.Name)

        def wrong(message: str) -> CATIAToolError:
            return CATIAToolError(
                f"Hole '{name}' was created but {message} It remains in the tree, not "
                "correctly threaded; delete it with catia_delete_feature or fix the "
                "thread in CATIA.",
                code="WRONG_GEOMETRY_EFFECT",
                modified=None,
            )

        if not _set_prop(hole, "ThreadingMode", _THREADED):
            raise wrong("CATIA rejected the threading mode, so it is smooth.")
        try:
            hole.CreateStandardThreadDesignTable(thread["code"])
        except Exception as error:
            raise wrong(
                f"CATIA refused the '{thread['standard']}' thread table "
                f"(CreateStandardThreadDesignTable({thread['code']}): {error})."
            ) from error
        def set_param(prop: str, value: Any) -> bool:
            try:
                return _set_prop(getattr(hole, prop), "Value", value)
            except Exception:  # the parameter itself is not exposed
                return False

        if not set_param("HoleThreadDescription", thread["designation"]):
            raise wrong(f"CATIA rejected the thread designation '{thread['designation']}'.")
        if not set_param("ThreadDepth", thread["depth"]):
            raise wrong(f"CATIA rejected the thread depth {thread['depth']:g} mm.")

        self._hole_update(part, hole, body, snapshot)

        read_designation = self._read_value(hole, "HoleThreadDescription")
        read_depth = self._read_value(hole, "ThreadDepth")
        if read_designation is not None and str(read_designation).strip() != thread["designation"]:
            raise wrong(
                f"its thread designation reads '{read_designation}' after "
                f"'{thread['designation']}' was set (not in the '{thread['standard']}' table?)."
            )
        if read_depth is not None and abs(float(read_depth) - thread["depth"]) > 1e-6:
            raise wrong(
                f"its thread depth reads {float(read_depth):g} mm after "
                f"{thread['depth']:g} mm was set."
            )
        parts = [f"Threaded: {read_designation or thread['designation']} ({thread['standard']})"]
        parts.append(f"thread depth {float(read_depth if read_depth is not None else thread['depth']):g} mm")
        for label, prop in (("thread diameter", "ThreadDiameter"), ("pitch", "ThreadPitch")):
            value = self._read_value(hole, prop)
            if value is not None:
                try:
                    parts.append(f"{label} {float(value):g} mm")
                except (TypeError, ValueError):
                    pass
        return " " + ", ".join(parts) + "; the hole diameter is the tap drill CATIA chose."

    @staticmethod
    def _read_value(obj: Any, prop: str) -> Any:
        """``obj.<prop>.Value`` for a parameter-object property, else None."""
        try:
            return getattr(obj, prop).Value
        except Exception:
            return None

    def _rect_pattern(self, args: dict[str, Any]) -> str:
        self.conn.ensure_connected()
        part = self.conn.get_active_part()
        body = self.conn.get_active_part_body()
        sf = part.ShapeFactory

        feature = self._get_last_shape(args.get("feature_name"))
        d1_count = args["dir1_count"]
        d1_spacing = args["dir1_spacing"]
        d2_count = args.get("dir2_count", 1)
        d2_spacing = args.get("dir2_spacing", 0)

        # AddNewRectPattern takes 12 parameters. Field-proven on V5R20:
        # topology edge references (Selection.Item(i).Reference) are accepted
        # as direction references, and CATIA rejects two PARALLEL directions,
        # so pass two ADJACENT edges (sharing a corner). Without explicit
        # edges, the origin planes give deterministic X/Y directions.
        dir1_edge = args.get("dir1_edge")
        dir2_edge = args.get("dir2_edge")
        if bool(dir1_edge) != bool(dir2_edge):
            raise InvalidArgument(
                "Pass both 'dir1_edge' and 'dir2_edge' ('Edge.N' from "
                "catia_list_edges, two adjacent edges sharing a corner), "
                "or neither."
            )
        if dir1_edge:
            ref1 = self._topo_reference("Edge", self._topo_index(dir1_edge, "Edge"))
            ref2 = self._topo_reference("Edge", self._topo_index(dir2_edge, "Edge"))
            directions = f"{dir1_edge} -> dir1, {dir2_edge} -> dir2"
        else:
            # Field-proven default on V5R20 (measured 2x2 boss grid): the
            # origin planes give deterministic directions, PlaneYZ -> X
            # and PlaneZX -> Y, through CreateReferenceFromObject.
            planes = self.conn.get_origin_elements()
            ref1 = part.CreateReferenceFromObject(planes["yz"])
            ref2 = part.CreateReferenceFromObject(planes["zx"])
            directions = "origin PlaneYZ -> dir1 (X), PlaneZX -> dir2 (Y)"

        before = self._body_volume_mm3(part, body)
        part.InWorkObject = body
        pattern = sf.AddNewRectPattern(
            feature,
            d1_count, d2_count,
            d1_spacing, d2_spacing,
            1, 1,          # position of the original along dir1/dir2
            ref1, ref2,    # direction references
            False, False,  # reversed dir1/dir2
            0.0,           # rotation angle
        )

        self._safe_update(part, pattern)
        self.conn.refresh_display()
        after = self._body_volume_mm3(part, body)
        effect = ""
        if before is not None and after is not None:
            effect = f" Volume {before:.1f} -> {after:.1f} mm3."
        return (
            f"Rectangular pattern '{pattern.Name}' created: {d1_count}x{d2_count}, "
            f"spacing {d1_spacing}x{d2_spacing} mm, directions: {directions}."
            + effect
        )

    def _mint_axis_reference(self, axis: str, center: list[float]) -> Any:
        """Build a rotation-axis Reference from a GSD line in a throwaway
        donor document.

        Field-proven on V5R20: AddNewCircPattern fails with 0x80020009 when
        the patterned document itself contains GSD geometry, but a Reference
        minted from a line in a separate document is accepted and survives
        closing that document.
        """
        app = self.conn.app
        target_doc = app.ActiveDocument
        target_name = str(target_doc.Name)
        previous_alerts = None
        try:
            previous_alerts = app.DisplayFileAlerts
            app.DisplayFileAlerts = False
        except Exception:
            pass
        donor_doc = app.Documents.Add("Part")
        try:
            donor = donor_doc.Part
            hsf = donor.HybridShapeFactory
            geoset = donor.HybridBodies.Add()
            dx, dy, dz = {"x": (1, 0, 0), "y": (0, 1, 0), "z": (0, 0, 1)}[axis]
            cx, cy, cz = center
            p1 = hsf.AddNewPointCoord(cx - 50 * dx, cy - 50 * dy, cz - 50 * dz)
            geoset.AppendHybridShape(p1)
            p2 = hsf.AddNewPointCoord(cx + 50 * dx, cy + 50 * dy, cz + 50 * dz)
            geoset.AppendHybridShape(p2)
            line = hsf.AddNewLinePtPt(
                donor.CreateReferenceFromObject(p1),
                donor.CreateReferenceFromObject(p2),
            )
            geoset.AppendHybridShape(line)
            donor.Update()
            # Both references are minted from the donor, exactly as the
            # validated recipe does.
            axis_ref = donor.CreateReferenceFromObject(line)
            center_ref = donor.CreateReferenceFromName("")
            return axis_ref, center_ref
        finally:
            try:
                donor_doc.Close()
            except Exception:
                pass
            try:
                target_doc.Activate()
            except Exception:
                pass
            if previous_alerts is not None:
                try:
                    app.DisplayFileAlerts = previous_alerts
                except Exception:
                    pass
            try:
                if str(app.ActiveDocument.Name) != target_name:
                    raise RuntimeError(
                        f"Could not re-activate '{target_name}' after the "
                        "donor round trip; aborting before patterning the "
                        "wrong document."
                    )
            except RuntimeError:
                raise
            except Exception:
                pass

    def _circ_pattern(self, args: dict[str, Any]) -> str:
        self.conn.ensure_connected()
        part = self.conn.get_active_part()
        body = self.conn.get_active_part_body()
        sf = part.ShapeFactory

        count = int(args["count"])
        if count < 1:
            raise InvalidArgument("count must be at least 1")
        angular_spacing = args.get("angular_spacing")
        if angular_spacing is None:
            angular_spacing = 360.0 / count
        axis = (args.get("axis") or "z").lower()
        if axis not in ("x", "y", "z"):
            raise InvalidArgument("axis must be 'x', 'y' or 'z'")
        center = args.get("center") or [0.0, 0.0, 0.0]
        if len(center) != 3:
            raise InvalidArgument("center must be [x, y, z] in mm")
        if self.conn.is_3dx:
            return self._circ_pattern_3dx(args, count, angular_spacing, axis, center)
        # Field-proven precondition: AddNewCircPattern fails with 0x80020009
        # when the patterned document itself contains GSD geometry.
        try:
            geosets = part.HybridBodies.Count
        except Exception:
            geosets = 0
        if geosets:
            raise UnsupportedCapability(
                "catia_circ_pattern cannot run on a part that contains "
                "geometrical sets (GSD geometry): CATIA rejects the pattern "
                "with 0x80020009 in that case. Remove the geometrical sets "
                "or pattern a GSD-free part."
            )
        self._get_last_shape(args.get("feature_name"))  # validate early

        axis_ref, center_ref = self._mint_axis_reference(axis, center)
        # Re-resolve after the donor round trip: the active document changed
        # and came back.
        part = self.conn.get_active_part()
        body = self.conn.get_active_part_body()
        sf = part.ShapeFactory
        feature = self._get_last_shape(args.get("feature_name"))

        before = self._body_volume_mm3(part, body)
        part.InWorkObject = body
        # AddNewCircPattern takes 12 parameters; instances around the axis
        # belong in the ANGULAR slot (3rd). Flags as field-validated
        # (4 bosses at radius 40, angles 0/90/180/270 on V5R20).
        pattern = sf.AddNewCircPattern(
            feature,
            1, count,              # radial copies, angular copies
            0.0, angular_spacing,  # radial step, angular step (deg)
            1, 1,                  # position of the original
            center_ref, axis_ref,  # rotation center (from axis line), axis
            False,                 # reversed rotation axis
            0.0,                   # rotation angle
            False,                 # radius aligned
        )

        self._safe_update(part, pattern)
        self.conn.refresh_display()
        after = self._body_volume_mm3(part, body)
        effect = ""
        if before is not None and after is not None:
            effect = f" Volume {before:.1f} -> {after:.1f} mm3."
        return (
            f"Circular pattern '{pattern.Name}' created: {count} instances, "
            f"{angular_spacing} deg spacing around the {axis.upper()} axis "
            f"through {center}." + effect
        )

    def _construction_geoset(self, part: Any) -> Any:
        for hb in iter_geosets(part.HybridBodies):
            if str(hb.Name) == "MCP_Construction":
                return hb
        geoset = part.HybridBodies.Add()
        geoset.Name = "MCP_Construction"
        return geoset

    def _circ_pattern_3dx(
        self, args: dict[str, Any], count: int, angular_spacing: float, axis: str, center: list[float]
    ) -> str:
        """3DEXPERIENCE circular pattern. The V5R20 donor-document recipe
        needs file documents, which 3DEXPERIENCE does not have; the rotation
        axis is a GSD line in the part itself, kept in a construction
        geometrical set named MCP_Construction."""
        part = self.conn.get_active_part()
        body = self.conn.get_active_part_body()
        sf = part.ShapeFactory
        feature = self._get_last_shape(args.get("feature_name"))

        hsf = part.HybridShapeFactory
        geoset = self._construction_geoset(part)
        dx, dy, dz = {"x": (1, 0, 0), "y": (0, 1, 0), "z": (0, 0, 1)}[axis]
        cx, cy, cz = center
        p1 = hsf.AddNewPointCoord(cx - 50 * dx, cy - 50 * dy, cz - 50 * dz)
        geoset.AppendHybridShape(p1)
        part.UpdateObject(p1)
        p2 = hsf.AddNewPointCoord(cx + 50 * dx, cy + 50 * dy, cz + 50 * dz)
        geoset.AppendHybridShape(p2)
        part.UpdateObject(p2)
        line = hsf.AddNewLinePtPt(
            part.CreateReferenceFromObject(p1), part.CreateReferenceFromObject(p2)
        )
        geoset.AppendHybridShape(line)
        part.UpdateObject(line)
        axis_ref = part.CreateReferenceFromObject(line)
        center_ref = part.CreateReferenceFromName("")

        before = self._body_volume_mm3(part, body)
        part.InWorkObject = body
        try:
            pattern = sf.AddNewCircPattern(
                feature,
                1, count,              # radial copies, angular copies
                0.0, angular_spacing,  # radial step, angular step (deg)
                1, 1,                  # position of the original
                center_ref, axis_ref,  # rotation center (from axis line), axis
                False,                 # reversed rotation axis
                0.0,                   # rotation angle
                False,                 # radius aligned
            )
            self._safe_update(part, pattern)
        except CATIAToolError as e:
            # Keep the typed code (FEATURE_UPDATE_FAILED); the axis line was
            # created, so the model changed whatever the error said before.
            e.args = (
                (
                    f"{e} The construction axis line '{line.Name}' remains in geometrical "
                    f"set '{geoset.Name}'; delete it if it is not needed."
                ),
            )
            e.modified = None
            raise
        except Exception as e:
            raise RuntimeError(
                f"{e} The construction axis line '{line.Name}' remains in geometrical "
                f"set '{geoset.Name}'; delete it if it is not needed."
            ) from e
        self.conn.refresh_display()
        after = self._body_volume_mm3(part, body)
        effect = ""
        if before is not None and after is not None:
            effect = f" Volume {before:.1f} -> {after:.1f} mm3."
        return (
            f"Circular pattern '{pattern.Name}' created: {count} instances, "
            f"{angular_spacing} deg spacing around the {axis.upper()} axis through "
            f"{center}. Axis line '{line.Name}' kept in geometrical set "
            f"'{geoset.Name}'." + effect
        )

    def _profile_object(self, part: Any, name: str) -> Any:
        return find_element(part, name)

    def _sweep_feature(self, kind: str, args: dict[str, Any]) -> str:
        self.conn.ensure_connected()
        part = self.conn.get_active_part()
        body = self.conn.get_active_part_body()
        sf = part.ShapeFactory
        # Profile and center curve are addressed by name, so a sketch still
        # open in the sketcher is only closed, never substituted.
        self._close_open_sketch()
        profile = self._profile_object(part, args["profile"])
        center = self._profile_object(part, args["center_curve"])
        before = self._body_volume_mm3(part, body)
        part.InWorkObject = body
        method = "AddNewRib" if kind == "rib" else "AddNewSlot"
        try:
            feature = getattr(sf, method)(profile, center)
        except Exception:
            # GSD curves (not sketches): the FromRef variant takes References.
            feature = getattr(sf, method + "FromRef")(
                part.CreateReferenceFromObject(profile), part.CreateReferenceFromObject(center)
            )
        self._safe_update(part, feature)
        self.conn.refresh_display()
        after = self._body_volume_mm3(part, body)
        if before is not None and after is not None:
            delta = after - before
            if kind == "slot" and delta >= 0:
                raise CATIAToolError(
                    f"Slot '{feature.Name}' was created in the tree but removed no material "
                    f"(volume stayed {before:.1f} mm3). Check that the profile sweeps "
                    "through the solid, then delete the slot with catia_delete_feature.",
                    code="FEATURE_NO_EFFECT",
                    modified=None,
                )
            if kind == "rib" and delta <= 0:
                raise CATIAToolError(
                    f"Rib '{feature.Name}' was created in the tree but added no material "
                    f"(volume {before:.1f} -> {after:.1f} mm3). Delete it with "
                    "catia_delete_feature.",
                    code="FEATURE_NO_EFFECT",
                    modified=None,
                )
            return (
                f"{kind.capitalize()} '{feature.Name}' created along '{args['center_curve']}'. "
                f"Volume {before:.1f} -> {after:.1f} mm3."
            )
        return (
            f"{kind.capitalize()} '{feature.Name}' created along '{args['center_curve']}' "
            "(volume verification unavailable)."
        )

    def _add_body(self, name: str | None) -> str:
        self.conn.ensure_connected()
        part = self.conn.get_active_part()
        body = part.Bodies.Add()
        if name:
            body.Name = name
        self.conn.set_active_body(str(body.Name))
        part.Update()
        return (
            f"Body '{body.Name}' added and set as the active body: new sketches and "
            "features go into it."
        )

    def _set_active_body(self, body_name: str | None) -> str:
        self.conn.ensure_connected()
        body = self.conn.set_active_body(body_name)
        return f"Active body: '{body.Name}'."

    _BOOLEAN_METHODS = {
        "add": "AddNewAdd",
        "remove": "AddNewRemove",
        "intersect": "AddNewIntersect",
        "assemble": "AddNewAssemble",
    }

    def _boolean(self, args: dict[str, Any]) -> str:
        self.conn.ensure_connected()
        part = self.conn.get_active_part()
        operation = args["operation"]
        tool_body = body_by_name(part, args["body_name"])
        target = body_by_name(part, args["target_body"]) if args.get("target_body") else part.MainBody
        if str(tool_body.Name) == str(target.Name):
            raise InvalidArgument("body_name and the target body must differ.")
        before = self._body_volume_mm3(part, target)
        part.InWorkObject = target
        feature = getattr(part.ShapeFactory, self._BOOLEAN_METHODS[operation])(tool_body)
        self._safe_update(part, feature)
        self.conn.refresh_display()
        after = self._body_volume_mm3(part, target)
        effect = ""
        if before is not None and after is not None:
            effect = f" Target volume {before:.1f} -> {after:.1f} mm3."
        return (
            f"Boolean {operation} '{feature.Name}': '{tool_body.Name}' into "
            f"'{target.Name}'.{effect}"
        )

    def _find_named(self, part: Any, name: str) -> Any:
        """Body, feature, sketch, geometrical set or GSD element by name."""
        for body in iter_items(part.Bodies):
            if str(body.Name) == name:
                return body
            for collection in ("Shapes", "Sketches"):
                try:
                    return getattr(body, collection).Item(name)
                except Exception:
                    pass
        for geoset in iter_geosets(part.HybridBodies):
            if str(geoset.Name) == name:
                return geoset
        try:
            return find_element(part, name)
        except ObjectNotFound:
            raise ObjectNotFound(
                f"No body, feature, sketch, geometrical set or GSD element named "
                f"'{name}' in the active part."
            )

    def _rename(self, name: str, new_name: str) -> str:
        self.conn.ensure_connected()
        part = self.conn.get_active_part()
        obj = self._find_named(part, name)
        obj.Name = new_name
        return f"'{name}' renamed to '{obj.Name}'."

    def _set_feature_active(self, name: str, active: bool) -> str:
        self.conn.ensure_connected()
        part = self.conn.get_active_part()
        feature = self._find_named(part, name)
        if active:
            part.Activate(feature)
        else:
            part.Inactivate(feature)
        try:
            part.Update()
        except Exception as e:
            self.conn.refresh_display()
            return (
                f"'{name}' {'activated' if active else 'deactivated'}, but the part "
                f"update failed afterwards ({e}); dependent features are in error."
            )
        self.conn.refresh_display()
        return f"'{name}' {'activated' if active else 'deactivated'}; part updated."

    def _select_named(self, names: list[str]) -> Any:
        part = self.conn.get_active_part()
        objects = [self._find_named(part, n) for n in names]
        sel = self.conn.hso
        sel.Clear()
        for obj in objects:
            sel.Add(obj)
        return sel

    def _set_visibility(self, names: list[str], visible: bool) -> str:
        self.conn.ensure_connected()
        sel = self._select_named(names)
        try:
            # CatVisPropertyShow: 0 = show, 1 = no show
            sel.VisProperties.SetShow(0 if visible else 1)
        finally:
            try:
                sel.Clear()
            except Exception:
                pass
        self.conn.refresh_display()
        return f"{'Shown' if visible else 'Hidden'}: {', '.join(names)}."

    def _set_color(self, names: list[str], rgb: list[int]) -> str:
        self.conn.ensure_connected()
        r, g, b = (int(v) for v in rgb)
        sel = self._select_named(names)
        try:
            sel.VisProperties.SetRealColor(r, g, b, 1)
        finally:
            try:
                sel.Clear()
            except Exception:
                pass
        self.conn.refresh_display()
        return f"Color ({r}, {g}, {b}) applied to: {', '.join(names)}."

    @staticmethod
    def _is_hole(feature: Any) -> bool:
        """Locale-independent: the COM class name from type information, or
        the members only a Hole exposes when type information is missing."""
        if type_name(feature) == "Hole":
            return True
        return name_exists(feature, "BottomLimit") and name_exists(feature, "ThreadingMode")

    def _user_pattern(self, args: dict[str, Any]) -> str:
        self.conn.ensure_connected()
        part = self.conn.get_active_part()
        body = self.conn.get_active_part_body()
        sf = part.ShapeFactory

        feature = self._get_last_shape(args.get("feature_name"))
        count = int(args["count"])
        if count < 1:
            raise InvalidArgument("count must be at least 1")
        sketch_name = args.get("sketch_name")
        if not sketch_name:
            # Field-proven trap: the last item of body.Sketches is stale right
            # after CloseEdition and resolves to the previous sketch, so the
            # points sketch must be addressed by name.
            raise InvalidArgument(
                "catia_user_pattern requires 'sketch_name': the points sketch "
                "name reported by catia_close_sketch (e.g. 'Sketch.3')."
            )
        if self.conn.is_3dx and self._is_hole(feature):
            # Live R428 SP6: CATIA rejects the update of a user pattern of a
            # sketch-positioned Hole. The likely missing input is the anchor
            # point, but DSYAutomation.chm R428 types UserPattern.AnchorPoint
            # only as CATIABase (no SetAnchorPoint, no example), so no
            # deterministic anchor recipe is established.
            raise UnsupportedCapability(
                f"catia_user_pattern cannot pattern the hole '{feature.Name}' on "
                "3DEXPERIENCE: CATIA rejects a user pattern of a sketch-positioned Hole "
                "on update (live R428 SP6), and the anchor point it would need "
                "(UserPattern.AnchorPoint) has no documented recipe. Nothing was created. "
                "Instead, draw one circle per position in a sketch and cut them with "
                "catia_pocket, or create one catia_hole per position."
            )
        # The points sketch must be closed before it can locate instances.
        self._close_open_sketch()
        sketch = self._get_last_sketch(sketch_name)

        before = self._body_volume_mm3(part, body)
        part.InWorkObject = body
        # Field-proven on V5R20: AddNewUserPattern(shape, n) then
        # AddFeatureToLocatePositions(points sketch). A locating point that
        # coincides with the seed makes the update fail (0x80020009).
        pattern = sf.AddNewUserPattern(feature, count)
        pattern.AddFeatureToLocatePositions(sketch)
        self._safe_update(part, pattern)
        self.conn.refresh_display()
        after = self._body_volume_mm3(part, body)
        effect = ""
        if before is not None and after is not None:
            effect = f" Volume {before:.1f} -> {after:.1f} mm3."
        return (
            f"User pattern '{pattern.Name}' created: {count} instances placed "
            f"on the points of sketch '{sketch.Name}'." + effect
        )

    def _mirror(self, args: dict[str, Any]) -> str:
        self.conn.ensure_connected()
        part = self.conn.get_active_part()
        # Kept for its check: a deleted/renamed active body is refused
        # (OBJECT_NOT_FOUND) before anything is created.
        self.conn.get_active_part_body()
        sf = part.ShapeFactory

        plane_key = args["plane"].lower()
        planes = self.conn.get_origin_elements()
        if plane_key not in planes:
            raise InvalidArgument(f"Unknown plane '{plane_key}'. Use 'xy', 'yz', or 'zx'.")

        mirror_plane = planes[plane_key]
        ref = part.CreateReferenceFromObject(mirror_plane)

        # AddNewMirror takes only the plane reference and mirrors the
        # body's existing shapes; per-feature mirroring is not expressible
        # through this API.
        mirror = sf.AddNewMirror(ref)

        part.UpdateObject(mirror)
        self.conn.refresh_display()
        return (
            f"Mirror created about {plane_key.upper()} plane (mirrors the "
            f"body's shapes). Feature: '{mirror.Name}'"
        )

    def _shell(self, args: dict[str, Any]) -> str:
        self.conn.ensure_connected()
        part = self.conn.get_active_part()
        # Kept for its check: a deleted/renamed active body is refused
        # (OBJECT_NOT_FOUND) before anything is created.
        self.conn.get_active_part_body()
        sf = part.ShapeFactory

        thickness = args["thickness"]
        faces = args.get("faces_to_remove") or []
        if not faces:
            raise InvalidArgument(
                "catia_shell requires 'faces_to_remove' with at least one "
                "indexed face name ('Face.N' from catia_list_faces): "
                "AddNewShell's first parameter is the face to remove."
            )
        refs = [
            self._topo_reference("Face", self._topo_index(f, "Face"))
            for f in faces
        ]
        # AddNewShell(iFaceToRemove, iInternalThickness, iExternalThickness)
        shell = sf.AddNewShell(refs[0], thickness, 0)
        skipped = []
        for extra, ref in zip(faces[1:], refs[1:]):
            try:
                shell.AddFaceToRemove(ref)
            except Exception:
                skipped.append(extra)

        part.UpdateObject(shell)
        self.conn.refresh_display()
        note = f" (could not add: {', '.join(skipped)})" if skipped else ""
        return (
            f"Shell created: {thickness} mm wall, removed "
            f"{len(faces) - len(skipped)} face(s){note}. Feature: '{shell.Name}'"
        )

    def _draft(self, args: dict[str, Any]) -> str:
        self.conn.ensure_connected()
        part = self.conn.get_active_part()
        # Kept for its check: a deleted/renamed active body is refused
        # (OBJECT_NOT_FOUND) before anything is created.
        self.conn.get_active_part_body()
        sf = part.ShapeFactory

        angle = args["angle"]
        face_name = args.get("face_name")
        if not face_name:
            raise InvalidArgument(
                "catia_draft requires 'face_name' ('Face.N' from "
                "catia_list_faces): the face to draft."
            )
        face_ref = self._topo_reference("Face", self._topo_index(face_name, "Face"))

        plane_key = args.get("pulling_direction", "xy").lower()
        planes = self.conn.get_origin_elements()
        neutral = planes.get(plane_key)
        if not neutral:
            raise InvalidArgument(f"Unknown pulling direction plane: {plane_key}")
        neutral_ref = part.CreateReferenceFromObject(neutral)

        # Pulling direction = the selected plane's normal.
        direction = {"xy": (0, 0, 1), "yz": (1, 0, 0), "zx": (0, 1, 0)}[plane_key]
        parting_ref = part.CreateReferenceFromName("")

        # AddNewDraft(iFaceToDraft, iNeutral, iNeutralMode, iParting,
        #             iDirX, iDirY, iDirZ, iMode, iAngle, iMultiselectionMode)
        draft = sf.AddNewDraft(
            face_ref, neutral_ref, 0, parting_ref,
            direction[0], direction[1], direction[2],
            0, angle, 0,
        )

        part.UpdateObject(draft)
        self.conn.refresh_display()
        return f"Draft created: {angle} deg on {face_name}. Feature: '{draft.Name}'"

    def _thickness(self, args: dict[str, Any]) -> str:
        self.conn.ensure_connected()
        part = self.conn.get_active_part()
        # Kept for its check: a deleted/renamed active body is refused
        # (OBJECT_NOT_FOUND) before anything is created.
        self.conn.get_active_part_body()
        sf = part.ShapeFactory

        offset = args["offset"]
        face_name = args.get("face_name")
        if not face_name:
            raise InvalidArgument(
                "catia_thickness requires 'face_name' ('Face.N' from "
                "catia_list_faces): the face to thicken."
            )
        face_ref = self._topo_reference("Face", self._topo_index(face_name, "Face"))

        # AddNewThickness(iFaceToThicken, iOffset) - 2 parameters.
        thickness = sf.AddNewThickness(face_ref, offset)

        part.UpdateObject(thickness)
        self.conn.refresh_display()
        return f"Thickness added: {offset} mm on {face_name}. Feature: '{thickness.Name}'"

    def _delete_feature(self, args: dict[str, Any]) -> str:
        self.conn.ensure_connected()
        part = self.conn.get_active_part()
        target = args["feature_name"]
        feature = None
        bodies = part.Bodies
        for b in range(1, bodies.Count + 1):
            shapes = bodies.Item(b).Shapes
            for i in range(1, shapes.Count + 1):
                shape = shapes.Item(i)
                if str(shape.Name) == target:
                    feature = shape
                    break
            if feature is not None:
                break
        if feature is None:
            raise ObjectNotFound(
                f"No feature named '{target}' in any body of the active part. "
                "Use catia_list_features to see names."
            )
        sel = self.conn.hso
        try:
            sel.Clear()
            sel.Add(feature)
            sel.Delete()
        finally:
            try:
                sel.Clear()
            except Exception:
                pass
        try:
            part.Update()
        except Exception as e:
            self.conn.refresh_display()
            return (
                f"Feature '{target}' deleted, but the part update failed "
                f"afterwards ({e}): features that depended on it are now "
                "in error. Fix or delete them too."
            )
        self.conn.refresh_display()
        return f"Feature '{target}' deleted."

    def _list_features(self) -> str:
        self.conn.ensure_connected()
        body = self.conn.get_active_part_body()
        shapes = body.Shapes

        features = []
        for i in range(1, shapes.Count + 1):
            shape = shapes.Item(i)
            features.append({
                "index": i,
                "name": shape.Name,
                "type": self._feature_type(shape),
            })

        if not features:
            return "No features in the active body"
        return json.dumps(features, indent=2)

    @staticmethod
    def _feature_type(shape: Any) -> str:
        """Feature type for catia_list_features.

        The COM class name from type information first (locale-independent;
        type_name already resolves Hole on R428, see _is_hole). Live R428 and
        V5 smoke runs reported 'unknown' for every feature because shapes
        expose no string ``Type`` property (Hole.Type is the CatHoleType
        enum). Fallbacks: a string ``Type`` property, then the tree name
        without its ``.N`` suffix (locale-dependent), then 'unknown'.
        """
        name = type_name(shape)
        if name:
            return name
        try:
            value = shape.Type
            if isinstance(value, str) and value:
                return value
        except Exception:
            pass
        try:
            stem = str(shape.Name).rsplit(".", 1)[0].strip()
            if stem:
                return stem
        except Exception:
            pass
        return "unknown"

    def _list_faces(self, body_name: str | None = None) -> str:
        self.conn.ensure_connected()
        if body_name:
            part = self.conn.get_active_part()
            bodies = part.Bodies
            try:
                body = bodies.Item(body_name)
            except Exception:
                names = ", ".join(str(bodies.Item(i).Name) for i in range(1, bodies.Count + 1))
                raise ObjectNotFound(
                    f"No body named '{body_name}'. Top-level bodies: {names or 'none'}."
                ) from None
        else:
            body = self.conn.get_active_part_body()

        faces = []
        try:
            if body.Shapes.Count == 0:
                return "No solid shape in the active body yet"
            sel = self.conn.hso
            try:
                count = search_final_topology(sel, body, "Face", is_3dx=self.conn.is_3dx)
                for i in range(1, count + 1):
                    faces.append({"index": i, "name": f"Face.{i}"})
            finally:
                try:
                    sel.Clear()
                except Exception:
                    pass
        except Exception as e:
            return f"Could not enumerate faces: {e}"

        if not faces:
            return "No faces found on the final shape"
        return json.dumps(faces, indent=2)

    def _list_edges(self) -> str:
        self.conn.ensure_connected()
        body = self.conn.get_active_part_body()

        # Get edges from the last shape
        last_shape = self._get_last_shape()
        edges = []
        try:
            # Access boundary representation
            sel = self.conn.hso
            try:
                count = search_final_topology(
                    sel, body, "Edge", last_shape, is_3dx=self.conn.is_3dx
                )
                for i in range(1, count + 1):
                    # Canonical indexed name accepted by catia_measure_distance,
                    # catia_fillet and catia_chamfer. Raw HSO names are long
                    # and unusable in searches.
                    edges.append({"index": i, "name": f"Edge.{i}"})
            finally:
                try:
                    sel.Clear()
                except Exception:
                    pass
        except Exception as e:
            return f"Could not enumerate edges: {e}. Use CATIA selection to identify edge names."

        if not edges:
            return "No edges found on the last feature"
        return json.dumps(edges, indent=2)
