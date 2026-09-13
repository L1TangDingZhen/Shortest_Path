"""Delivery requirements, independent of the direction a vehicle travels.

Left and right refer to the edge's geometry oriented from CSV u to v.
For a closed way the stored geometry's order is the reference direction.
"""

from collections import Counter

from split_edge import project_onto_chain


DELIVERY_SERVICE = {"none": 0, "left": 1, "right": 1, "both": 2,
                    "unspecified": 1}
LEGACY_DELIVERY = {"0": "none", "1": "unspecified", "2": "both",
                   "x": "none"}


def delivery_mode(row, require_known=False):
    service = str(row.get("service") or "").strip().lower() or "0"
    if service not in LEGACY_DELIVERY:
        raise ValueError(f"edge {row.get('edge_id', '?')}: bad service "
                         f"value {service!r} (must be 0, 1, 2 or x)")
    mode = str(row.get("delivery") or "").strip().lower()
    mode = mode or LEGACY_DELIVERY[service]
    if mode not in DELIVERY_SERVICE:
        raise ValueError(f"edge {row.get('edge_id', '?')}: bad delivery "
                         f"value {mode!r}")
    if ((service == "x" and mode != "none") or
            (service != "x" and int(service) != DELIVERY_SERVICE[mode])):
        raise ValueError(f"edge {row.get('edge_id', '?')}: service={service} "
                         f"conflicts with delivery={mode}; edit both fields "
                         "together or use the editor")
    if require_known and mode == "unspecified":
        raise ValueError(f"edge {row.get('edge_id', '?')}: delivery is "
                         "unspecified; choose left or right in "
                         "the editor before solving")
    return mode


def tasks_for(mode):
    if mode == "both":
        return ("left", "right")
    if mode in ("left", "right"):
        return (mode,)
    if mode == "none":
        return ()
    raise ValueError("delivery is unspecified; choose left or right")


def travel_side(required_side, forward):
    if forward:
        return required_side
    return "right" if required_side == "left" else "left"


def counterflow(required_side, forward, oneway, traffic_side):
    if oneway:
        return not forward
    return travel_side(required_side, forward) != traffic_side


def point_side(coords, point):
    """Distance, side of the local tangent, and position along the reference."""
    distance, along, _, index, _ = project_onto_chain(coords, point)
    a, b = coords[index:index + 2]
    cross = ((b[1] - a[1]) * (point[0] - a[0]) -
             (b[0] - a[0]) * (point[1] - a[1]))
    side = "left" if cross > 1e-14 else "right" if cross < -1e-14 else ""
    return distance, side, along


def remap_side(mode, old_coords, new_coords):
    """Keep a physical side when an extraction reverses/splits its reference."""
    if mode not in ("left", "right"):
        return mode
    if not old_coords or not new_coords:
        return "unspecified"
    alignment = 0.0
    for a, b in zip(new_coords, new_coords[1:]):
        middle = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        distance, _, _, index, _ = project_onto_chain(old_coords, middle)
        if distance > 6.0:
            return "unspecified"
        u, v = old_coords[index:index + 2]
        alignment += ((b[0] - a[0]) * (v[0] - u[0]) +
                      (b[1] - a[1]) * (v[1] - u[1]))
    if abs(alignment) < 1e-18:
        return "unspecified"
    return mode if alignment > 0 else travel_side(mode, False)


def validate_coverage(edges, segments):
    """Check the exact service tasks, not pass counts or travel directions."""
    expected = Counter((edge["edge_id"], side)
                       for edge in edges
                       for side in tasks_for(delivery_mode(edge, True)))
    actual = Counter((s["edge_id"], s.get("delivery_side"))
                     for s in segments if s["kind"] == "service")
    if actual != expected:
        raise ValueError("delivery coverage failed: missing, duplicated or "
                         "unexpected side tasks")
    return sum(expected.values())
