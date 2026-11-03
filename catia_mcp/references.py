"""Shared element and topology resolution for Part tools (V5 and 3DEXPERIENCE).

Named elements: origin plane aliases, hybrid shapes and sketches in every
geometrical set (nested included), then sketches in solid bodies.

Topology: indexed Face.N / Edge.N / Vertex.N over the final shape of a body,
matching catia_list_faces / catia_list_edges. References come from the
selection's Reference property because CreateReferenceFromObject rejects
HSO-resolved topology cells (field-verified E_INVALIDARG on V5R20). Every
enumeration goes through search_final_topology so indices agree across tools;
the search root differs per platform (see there).
"""

from __future__ import annotations

import re
from typing import Any

from catia_mcp.automation import iter_items
from catia_mcp.connection import CATIAConnection
from catia_mcp.errors import InvalidArgument, ObjectNotFound

# Origin plane aliases
PLANE_MAP = {
    "xy": "PlaneXY",
    "yz": "PlaneYZ",
    "zx": "PlaneZX",
    "xz": "PlaneZX",  # alias
}

_TOPOLOGY = re.compile(r"^(Face|Edge|Vertex)\.(\d+)$")


def iter_geosets(hybrid_bodies: Any):
    """Yield all geometrical sets, including nested ones."""
    for i in range(1, hybrid_bodies.Count + 1):
        hb = hybrid_bodies.Item(i)
        yield hb
        try:
            yield from iter_geosets(hb.HybridBodies)
        except Exception:
            pass


def find_element(part: Any, name: str) -> Any:
    """Find a wireframe/surface element or sketch by its tree name."""
    key = name.lower()
    if key in PLANE_MAP:
        return getattr(part.OriginElements, PLANE_MAP[key])

    for hb in iter_geosets(part.HybridBodies):
        for collection in ("HybridShapes", "HybridSketches"):
            try:
                return getattr(hb, collection).Item(name)
            except Exception:
                pass

    bodies = part.Bodies
    for i in range(1, bodies.Count + 1):
        try:
            return bodies.Item(i).Sketches.Item(name)
        except Exception:
            pass

    raise ObjectNotFound(
        f"Element '{name}' not found in geometrical sets or body sketches. "
        "Use catia_gsd_list_elements to see available element names."
    )


def topology_match(name: str) -> tuple[str, int] | None:
    match = _TOPOLOGY.match(name or "")
    if not match:
        return None
    return match.group(1), int(match.group(2))


def topology_index(name: str, kind: str) -> int:
    match = topology_match(name)
    if not match or match[0] != kind:
        raise InvalidArgument(
            f"Expected an indexed name like '{kind}.1' (from "
            f"catia_list_faces / catia_list_edges), got '{name}'."
        )
    return match[1]


def _search(sel: Any, root: Any, selection_type: str, *, probe: bool = False) -> int:
    """Select `root` and replace the selection with its `selection_type`
    cells. With `probe` (a selection type the release may not know, such as
    CGMEdge), a raising search counts as no hits; otherwise the COM error
    propagates as it always did (V5 and the generic types are unchanged)."""
    sel.Clear()
    sel.Add(root)
    try:
        sel.Search(f"Topology.{selection_type},sel")
    except Exception:
        if not probe:
            raise
        sel.Clear()
        return 0
    return sel.Count


def search_final_topology(
    sel: Any, body: Any, kind: str, last_shape: Any | None = None, *, is_3dx: bool
) -> int:
    """Fill `sel` with the Face / Edge / Vertex cells of a body's final shape
    and return how many there are. The caller clears `sel`.

    CATIA V5: the search starts from the body's last shape (field-validated
    on V5R20) and runs on the body only when that finds nothing.

    3DEXPERIENCE faces and vertices: the search runs on the body, never on
    the last shape. Live on R428 SP6, a last-shape search returns only the
    cells that feature created (2 faces when the last feature is a Hole) or
    none at all (a boolean, Add.1), while the body search returns the final
    solid (14 faces for a block with 4 holes, 15 on the smoke block after a
    boolean). Falling back to the last shape would index a partial cell
    set, so there is no fallback.

    3DEXPERIENCE edges: the generic ``Topology.Edge`` search on the body
    returned 16 edges for a 12-edge box on R428 SP6 (the extra hits are
    presumably the profile sketch's wire edges), and fillets built on them
    failed. Field-proven on R428 SP6 (2026-11-02 field report): selecting
    the Pad (the last shape) and searching ``Topology.CGMEdge,sel`` returns
    exactly the 12 solid edges, and those references fillet. Order:

    1. ``CGMEdge`` on the last shape (field-proven for a Pad last).
    2. ``CGMEdge`` on the body, when the last shape exposes none (for
       example a boolean). UNVERIFIED live.
    3. ``Edge`` on the body: the previous behaviour, kept for releases that
       do not know the CGMEdge type. On R428 it over-counts (see above).

    As on V5, a last-shape enumeration covers the edges of the last feature
    (after a Hole, the hole's own edges; R428 face behaviour suggests this,
    unverified for edges).
    """
    if is_3dx and kind == "Edge":
        if last_shape is None:
            last_shape = body.Shapes.Item(body.Shapes.Count)
        attempts: tuple[tuple[Any, str], ...] = (
            (last_shape, "CGMEdge"),
            (body, "CGMEdge"),
            (body, "Edge"),
        )
    elif is_3dx:
        attempts = ((body, kind),)
    else:
        if last_shape is None:
            last_shape = body.Shapes.Item(body.Shapes.Count)
        attempts = ((last_shape, kind), (body, kind))
    for root, selection_type in attempts:
        if _search(sel, root, selection_type, probe=selection_type == "CGMEdge"):
            break
    return sel.Count


def topology_references(conn: CATIAConnection, kind: str, body: Any | None = None) -> list[Any]:
    """Every Face / Edge / Vertex Reference of a body's final shape (default:
    the active body), in the order catia_list_faces / catia_list_edges and
    topology_reference index them."""
    body = body if body is not None else conn.get_active_part_body()
    if body.Shapes.Count == 0:
        raise ObjectNotFound("The active body has no solid shape yet.")
    sel = conn.hso
    try:
        count = search_final_topology(sel, body, kind, is_3dx=conn.is_3dx)
        return [sel.Item(i).Reference for i in range(1, count + 1)]
    finally:
        try:
            sel.Clear()
        except Exception:
            pass


def topology_reference(conn: CATIAConnection, kind: str, index: int, body: Any | None = None) -> Any:
    """Resolve Face.N / Edge.N / Vertex.N on a body's final shape (default:
    the active body) to a Reference."""
    body = body if body is not None else conn.get_active_part_body()
    if body.Shapes.Count == 0:
        raise ObjectNotFound("The active body has no solid shape yet.")
    sel = conn.hso
    try:
        count = search_final_topology(sel, body, kind, is_3dx=conn.is_3dx)
        if index < 1 or index > count:
            raise ObjectNotFound(
                f"{kind}.{index} is out of range: the final shape exposes "
                f"{count} {kind.lower()}(s). Use catia_list_faces / "
                "catia_list_edges to enumerate valid indices."
            )
        return sel.Item(index).Reference
    finally:
        try:
            sel.Clear()
        except Exception:
            pass


def element_or_face_reference(conn: CATIAConnection, part: Any, name: str) -> Any:
    """Reference to a named element (plane, curve, sketch) or to Face.N."""
    topo = topology_match(name)
    if topo:
        return topology_reference(conn, topo[0], topo[1])
    return part.CreateReferenceFromObject(find_element(part, name))


def body_by_name(part: Any, body_name: str) -> Any:
    bodies = part.Bodies
    try:
        return bodies.Item(body_name)
    except Exception:
        names = ", ".join(str(b.Name) for b in iter_items(bodies))
        raise ObjectNotFound(f"No body named '{body_name}'. Top-level bodies: {names or 'none'}.")
