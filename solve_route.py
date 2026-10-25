#!/usr/bin/env python3
"""Solve a postal delivery round as a Rural Postman Problem.

Input:  a data directory containing edges.csv and nodes.csv produced by
        extract_network.py, AFTER you have edited the `service` column:

            2 = I deliver both sides  -> street must be traversed twice
            1 = I deliver one side only, selected by delivery=left/right
            0 = not my street, but I may ride along it (connector)
            x = never use this edge   -> removed from the network
                entirely (not even as a deadhead shortcut)

Output: route.csv       ordered traversal list -- the basis for the new
                        sort sequence
        route_map.html  interactive route viewer: step through the walk
                        with a slider/play button, current segment
                        highlighted with a direction arrow
        console summary service km vs total km vs extra (deadhead) km

Algorithm (4 phases; matching gives exact augmentation for connected
required work in an undirected graph with symmetric additive costs, but directional/turn preferences and
auto-bridging make the current full objective heuristic):

  Phase 1  Build the full network F (all edges, including service=0)
           and the required multigraph R (one task per required physical
           delivery side). F is the world you can ride through; R is the work
           you must do.

  Phase 2  Parity repair.  A single continuous route can only exist if
           every node of R has even degree (Euler, 1736).  Nodes with
           odd degree are exactly where a route is forced to break.
           Compute shortest paths between all odd nodes over F, then a
           minimum-weight perfect matching (blossom algorithm, via
           networkx) picks the cheapest set of extra connections.
           Extra riding may also include component bridges and a
           requested return leg; matching alone does not optimise
           the full directional/turn-aware route.

  Phase 3  R + connectors is now Eulerian: extract an Euler circuit
           (or an open Euler path with --open).

  Phase 4  Expand connectors back into real street segments and emit
           the ordered route.

Usage:
    python solve_route.py --data data --out result
    python solve_route.py --data data --out result --open
    python solve_route.py --data data --out result --start=-37.8406,144.9541
    python solve_route.py --data data --out result \
        --start=-37.8406,144.9541 --end=-37.8380,144.9520

--start + --end (SPEC-2): a zero-length virtual REQUIRED edge
end -> start is added, the ordinary closed circuit is solved, then the
circuit is rotated so the virtual edge is last and dropped -- what
remains is an open route start -> ... -> end. This preserves the
endpoint constraints, not a global optimality guarantee. The
endpoints are snapped to the full network F, so they may lie on
non-service streets (e.g. a depot or a parcel-handover office reached
via service-0 corridor streets); if they sit off the service
component, the auto-bridge connects them and the heuristic-bridging warning
applies as usual.
"""

import argparse
import csv
import io
import itertools
import json
import math
import random
import sys
from collections import defaultdict
from pathlib import Path

import networkx as nx

from round_history import DEFAULT_HISTORY

from delivery import (counterflow, delivery_mode, tasks_for, travel_side,
                      validate_coverage)
from split_edge import oriented_coords


# ----------------------------------------------------------------------
# Input
# ----------------------------------------------------------------------

def load_data(data_dir: Path, inputs=None):
    """Read nodes.csv and edges.csv. Node ids are kept as strings."""
    def source(name):
        if inputs is not None:
            return io.StringIO(inputs[name].decode("utf-8-sig"), newline="")
        return open(data_dir / name, newline="", encoding="utf-8-sig")

    nodes, control = {}, {}
    with source("nodes.csv") as f:
        for row in csv.DictReader(f):
            nodes[row["node_id"]] = (float(row["lat"]), float(row["lon"]))
            kind = (row.get("control") or "").strip()
            if kind:
                control[row["node_id"]] = (
                    kind, (row.get("control_group") or "").strip())

    edges, blank_service, excluded, pending = [], 0, 0, 0
    with source("edges.csv") as f:
        for row in csv.DictReader(f):
            raw = (row.get("service") or "").strip()
            if raw == "":
                blank_service += 1
                service = 0
            elif raw.lower() == "x":
                excluded += 1
                continue          # excluded: not part of the graph at all
            else:
                try:
                    service = int(raw)
                except ValueError:
                    sys.exit(f"edges.csv: bad service value {raw!r} "
                             f"on edge {row['edge_id']} "
                             f"(must be 0, 1, 2 or x)")
            if service not in (0, 1, 2):
                sys.exit(f"edges.csv: service={service} on edge "
                         f"{row['edge_id']} (must be 0, 1, 2 or x)")
            try:
                mode = delivery_mode(row)
            except ValueError as exc:
                sys.exit(f"edges.csv: {exc}")
            pending += mode == "unspecified"
            if service and row["u"] == row["v"]:
                coords = oriented_coords(row, nodes)
                if len(set(coords)) < 3:
                    sys.exit("edges.csv: a service loop has no usable geometry; "
                             "its delivery sides cannot be located. Add its "
                             "geometry before solving.")
            edges.append({
                "edge_id": row["edge_id"],
                "u": row["u"],
                "v": row["v"],
                "name": row.get("name") or "(unnamed)",
                "highway": (row.get("highway") or "").split(";")[0].strip(),
                "oneway": (row.get("oneway") or "").strip().lower()
                in ("true", "yes", "1"),
                "maxspeed": float(row["maxspeed_kmh"])
                if (row.get("maxspeed_kmh") or "").strip() else None,
                "length": float(row["length_m"]),
                "service": service,
                "delivery": mode,
                "wkt": row.get("geometry_wkt") or "",
            })
    if blank_service:
        print(f"  note: {blank_service} edges had a blank service value "
              f"-> treated as 0 (connector only)")
    if excluded:
        print(f"  note: {excluded} edges excluded (service=x) -> the "
              f"route will never use them, not even as shortcuts")
    if pending:
        sys.exit(f"{pending} one-sided edge(s) have unspecified delivery. "
                 "Set delivery=left or right in the editor before solving. "
                 "service=1 always means one side, never both.")
    return nodes, edges, control


# ----------------------------------------------------------------------
# Cost profiles -- what "shortest" means
# ----------------------------------------------------------------------
#
# `length` is always real metres: it is what the mandatory distance,
# route.csv and every printed kilometre are made of.  `cost` is what
# the optimiser tries to reduce: modelled riding time plus configured
# soft preferences. Reported time excludes the artificial penalty factors;
# the heuristic route is not guaranteed to minimise it globally.
# A metre of footpath costs more than a metre of road, so the cheapest
# route can be slightly longer in metres. That is the point.
#
# Stops at letterboxes are excluded on purpose: the same letterboxes
# are served whichever way the route runs, so stop time is a constant
# that cancels out when comparing routes -- and it varies by the day's
# mail, which would make it noise rather than signal.

# Surfaces you ride *on* rather than drive along: a footway has no
# posted limit (0% of them carry one in OSM), and what you do there is
# set by the vehicle and the pedestrians, not by the law. Everywhere
# else the posted limit is the honest number.
FOOT_SURFACES = {"footway", "path", "pedestrian", "steps", "corridor",
                 "track", "cycleway"}

EDV_SURFACE = {"footway": 10, "path": 10, "pedestrian": 10,
               "cycleway": 15, "track": 10, "corridor": 6, "steps": 2}

# Fallback speeds, used only where OSM has no posted limit.
ROAD_FALLBACK = {"service": 25, "living_street": 20,
                 "residential": 50, "unclassified": 50,
                 "tertiary": 50, "tertiary_link": 50,
                 "secondary": 60, "secondary_link": 60,
                 "primary": 60, "primary_link": 60,
                 "trunk": 60, "trunk_link": 60}

EDV_TOP_SPEED = 50.0        # what the vehicle can actually do

PROFILES = {
    # One speed everywhere, so cost is proportional to metres: this is
    # what "shortest distance" means once cost is measured in seconds.
    "distance": {"speeds": {}, "use_maxspeed": False, "cap_kmh": None},
    # What the vehicle does: the posted limit where it is slower than
    # the vehicle, the vehicle's own top speed where it is not.
    "edv": {"speeds": {**EDV_SURFACE, **ROAD_FALLBACK},
            "use_maxspeed": True, "cap_kmh": EDV_TOP_SPEED},
    # What the road allows: the posted limit, whatever it says.
    "limits": {"speeds": {**EDV_SURFACE, **ROAD_FALLBACK},
               "use_maxspeed": True, "cap_kmh": None},
}
DEFAULT_PROFILE = "edv"
DEFAULT_SPEED = 15.0


def load_profile(name):
    """Preset name, or a JSON file {"speeds": {highway: km/h},
    "use_maxspeed": bool, "cap_kmh": number}. Returns (label,
    profile or None)."""
    if name in PROFILES:
        return name, PROFILES[name]
    path = Path(name)
    if not path.exists():
        sys.exit(f"--profile: unknown preset and no such file: {name!r} "
                 f"(presets: {', '.join(PROFILES)})")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        sys.exit(f"--profile {name}: not valid JSON ({exc})")
    speeds = data.get("speeds", data if "use_maxspeed" not in data else {})
    if not isinstance(speeds, dict) or not speeds:
        sys.exit(f"--profile {name}: expected a non-empty object of "
                 f"highway -> km/h")
    try:
        speeds = {k: float(v) for k, v in speeds.items()}
        cap = data.get("cap_kmh")
        cap = float(cap) if cap else None
    except (TypeError, ValueError):
        sys.exit(f"--profile {name}: speeds must be numbers")
    if any(v <= 0 for v in speeds.values()) or (cap is not None and cap <= 0):
        sys.exit(f"--profile {name}: speeds must be positive")
    return path.stem, {"speeds": speeds,
                       "use_maxspeed": bool(data.get("use_maxspeed")),
                       "cap_kmh": cap}


def speed_kmh(highway, maxspeed, profile):
    """How fast this edge is ridden. Posted limits only apply where you
    ride on the carriageway; on a footway the surface decides."""
    kmh = profile["speeds"].get(highway, DEFAULT_SPEED)
    if profile.get("use_maxspeed") and maxspeed \
            and highway not in FOOT_SURFACES:
        kmh = maxspeed
    cap = profile.get("cap_kmh")
    return min(kmh, cap) if cap else kmh


def edge_cost(length, highway, maxspeed, profile):
    """What the optimiser minimises: seconds. Distances are reported
    from `length` regardless, so a kilometre is always a kilometre."""
    return length / (speed_kmh(highway, maxspeed, profile) / 3.6)


# ----------------------------------------------------------------------
# Phase 1 -- graphs
# ----------------------------------------------------------------------

def build_graphs(edges, profile=None, wrong_way=1.0):
    """F: undirected view used for lookups and cross streets.
    R: the required work.  D: directed cost graph -- every edge becomes
    two arcs so that riding a one-way street the wrong way can be made
    more expensive than riding it the right way.  All path finding runs
    on D; nothing else knows about direction."""
    F = nx.MultiGraph()    # full rideable network
    R = nx.MultiGraph()    # required work (with multiplicities)
    D = nx.MultiDiGraph()  # same network, directed, carrying `cost`
    for e in edges:
        cost = edge_cost(e["length"], e["highway"], e["maxspeed"],
                         profile)
        kmh = speed_kmh(e["highway"], e["maxspeed"], profile)
        # One dict for both graphs: F carrying a stale copy of what D
        # knows is how the graded turn cost went quietly missing once
        # (`kmh` reached D only, and every lookup through F read None).
        common = dict(length=e["length"], name=e["name"], wkt=e["wkt"],
                      highway=e["highway"], oneway=e["oneway"],
                      maxspeed=e["maxspeed"], kmh=kmh, cu=e["u"], cv=e["v"])
        F.add_edge(e["u"], e["v"], key=e["edge_id"],
                   service=e["service"], delivery=delivery_mode(e, True), **common)
        D.add_edge(e["u"], e["v"], key=e["edge_id"], cost=cost,
                   against=False, **common)
        D.add_edge(e["v"], e["u"], key=e["edge_id"],
                   cost=cost * wrong_way if e["oneway"] else cost,
                   against=e["oneway"], **common)
        for p, side in enumerate(tasks_for(delivery_mode(e, True)), 1):
            R.add_edge(e["u"], e["v"], key=f'{e["edge_id"]}|p{p}',
                       base=e["edge_id"], length=e["length"], delivery_side=side)
    return F, R, D


def cheapest_arc(D, x, y):
    """(key, data) of the arc actually taken between adjacent nodes."""
    return min(D[x][y].items(), key=lambda kv: kv[1]["cost"])


def path_length_m(D, arc_path):
    """Real metres along an arc path (whose cost may be weighted)."""
    return sum(D[u][v][k]["length"] for u, v, k in arc_path)


# ----------------------------------------------------------------------
# Routing: two algorithms, same interface
# ----------------------------------------------------------------------
#
# `node`  Dijkstra over the road graph. A turn costs nothing, because a
#         node has no memory of how you arrived. This is what the
#         solver has always done.
#
# `turn`  Dijkstra one level up, over the LINE GRAPH: a vertex is
#         "travelling along this arc", an edge is a manoeuvre, and its
#         weight is the next arc's seconds plus what the turn costs.
#         Now "right onto a 60 km/h road" is priced where the route is
#         actually chosen, not just when picking between equal tours.
#         This is how real routing engines do it.
#
# Both return ARC paths -- [(u, v, key), ...] -- so the arcs the search
# priced are exactly the arcs that end up in the route.

SOURCE = ("<virtual source>",)

# The algorithm list. Adding one here is all it takes for `--algorithm
# all` to solve with it and put its route beside the others.
ALGORITHMS = {"node": ["node"], "turn": ["turn"]}

# What a directions app calls a preference: one objective each, solved
# separately, then all of them measured on the SAME yardstick so the
# numbers beside them can be compared. Adding one is a dict entry.
VARIANTS = {
    "shortest": {"label": "distance priority", "algorithm": "node",
                 "profile": "distance", "wrong_way_penalty": 1.0,
                 "turn_weight": 0.0},
    "fastest": {"label": "riding-time priority", "algorithm": "node",
                "profile": "edv", "wrong_way_penalty": 1.0,
                "turn_weight": 0.0},
    "recommended": {"label": "recommended", "algorithm": "turn",
                    "profile": "edv", "wrong_way_penalty": 3.0,
                    "turn_weight": 1.0},
    "gentle": {"label": "turn avoidance", "algorithm": "turn",
               "profile": "edv", "wrong_way_penalty": 3.0,
               "turn_weight": 4.0},
    "with_traffic": {"label": "traffic-flow preference", "algorithm": "turn",
                     "profile": "edv", "wrong_way_penalty": 12.0,
                     "turn_weight": 1.0},
}
VARIANT_ORDER = ["recommended", "fastest", "shortest", "gentle",
                 "with_traffic"]
SCORE_PROFILE = "edv"      # every variant is measured with this one

# Soft-cost presets only: these do not model access, sidewalk availability
# or vehicle speeds. No preference is applied until one is requested/saved.
VEHICLE_WRONG_WAY = {
    "tricycle": 3.0,
    "footpath": 1.0,
    "carriageway": 12.0,
}


def resolve_wrong_way_penalty(explicit, vehicle, saved):
    """Resolve a single run's preference, retaining its source for reporting."""
    if explicit is not None:
        value, source = explicit, "--wrong-way-penalty"
    elif vehicle is not None:
        value, source = VEHICLE_WRONG_WAY[vehicle], f"--vehicle {vehicle}"
    elif saved not in (None, ""):
        value, source = saved, "endpoints.json"
    else:
        value, source = 1.0, "default"
    error = f"{source}: wrong-way penalty must be a finite number >= 1.0"
    try:
        factor = float(value)
    except (TypeError, ValueError):
        raise ValueError(error) from None
    if not math.isfinite(factor) or factor < 1.0:
        raise ValueError(error)
    return factor, source


def arc_bearings(D, nodes):
    """(bearing leaving, bearing arriving) for every arc."""
    out = {}
    for u, v, k, d in D.edges(keys=True, data=True):
        chain = segment_coords({"wkt": d["wkt"], "u": u, "v": v}, nodes)
        out[(u, v, k)] = (
            bearing_of(chain[0], chain[1]) if len(chain) > 1 else None,
            bearing_of(chain[-2], chain[-1]) if len(chain) > 1 else None)
    return out


def build_line_graph(D, nodes, traffic_side="left",
                     turn_weight=1.0, control=None):
    """Vertices are arcs, edges are turns. Size is sum over nodes of
    in-degree x out-degree -- a few times the road graph, no more."""
    ends = arc_bearings(D, nodes)
    L = nx.DiGraph()
    for (u, v, k), (_, arriving) in ends.items():
        for _, w, k2 in D.out_edges(v, keys=True):
            leaving = ends[(v, w, k2)][0]
            entered = D[v][w][k2]
            L.add_edge((u, v, k), (v, w, k2),
                       weight=entered["cost"]
                       + (node_delay(control, u, v) if control else 0.0)
                       + turn_weight
                       * turn_seconds(turn_kind(arriving, leaving,
                                                traffic_side),
                                      entered.get("kmh")))
    return L


def make_router(D, nodes, algorithm="node", traffic_side="left",
                turn_weight=1.0, control=None):
    router = {"kind": algorithm, "D": D}
    if algorithm == "turn":
        other = "right" if traffic_side == "left" else "left"
        router["L"] = build_line_graph(D, nodes, traffic_side, turn_weight,
                                       control)
        # Searching paths that END at a target means walking the network
        # backwards; mirroring it swaps left and right, so the reversed
        # copy is built for the other side of the road to keep real
        # turns priced correctly.
        router["L_rev"] = build_line_graph(D.reverse(copy=True), nodes,
                                           other, turn_weight, control)
    return router


def node_path_to_arcs(D, node_path):
    return [(x, y, cheapest_arc(D, x, y)[0])
            for x, y in zip(node_path, node_path[1:])]


def _dijkstra_arcs(router, sources, reverse):
    """Cheapest arc path from (or into) any source node, per node."""
    D = router["D"]
    L = router["L_rev" if reverse else "L"]
    L.add_node(SOURCE)
    try:
        for s in sources:
            for u, v, k in D.in_edges(s, keys=True) if reverse \
                    else D.out_edges(s, keys=True):
                arc = (v, u, k) if reverse else (u, v, k)
                L.add_edge(SOURCE, arc, weight=D[u][v][k]["cost"])
        dist, paths = nx.single_source_dijkstra(L, SOURCE, weight="weight")
    finally:
        L.remove_node(SOURCE)

    best_cost, best_path = {}, {}
    for arc, cost in dist.items():
        if arc is SOURCE or arc == SOURCE:
            continue
        # arc[1] either way. Forward that is where the path ends;
        # in the mirrored graph the arcs come back flipped, so it is
        # where the real path BEGINS -- which is what a caller asking
        # "how do I get INTO this target" is keying on.
        head = arc[1]
        if head not in best_cost or cost < best_cost[head]:
            best_cost[head] = cost
            chain = [a for a in paths[arc] if a != SOURCE]
            if reverse:                      # stored on the mirror image
                chain = [(v, u, k) for u, v, k in reversed(chain)]
            best_path[head] = chain
    for s in sources:
        best_cost[s], best_path[s] = 0.0, []
    return best_cost, best_path


def route_from(router, sources, reverse=False):
    """(cost, arc path) to every reachable node, from any of `sources`
    (or into them, with reverse=True)."""
    # Equal-cost paths must not depend on the process's string hash seed.
    sources = sorted(set(sources))
    if router["kind"] == "turn":
        return _dijkstra_arcs(router, sources, reverse)
    D = router["D"]
    graph = D.reverse(copy=False) if reverse else D
    dist, paths = nx.multi_source_dijkstra(graph, sources=sources,
                                           weight="cost")
    arcs = {}
    for n, p in paths.items():
        arcs[n] = node_path_to_arcs(D, list(reversed(p)) if reverse else p)
    return dist, arcs


def route_path(router, a, b):
    """Cheapest arc path a -> b."""
    if router["kind"] == "turn":
        _, paths = _dijkstra_arcs(router, [a], False)
        if b not in paths:
            raise nx.NetworkXNoPath(f"{a} -> {b}")
        return paths[b]
    return node_path_to_arcs(
        router["D"], nx.shortest_path(router["D"], a, b, weight="cost"))


def both_ways(router, a, b):
    """A connector is ridden in whichever direction the tour needs, and
    with turn costs the two directions are different paths."""
    return {a: route_path(router, a, b), b: route_path(router, b, a)}



def ensure_required_connected(R, router, pinned_endpoints=False):
    """If the required work falls into several islands, bridge them with
    the cheapest paths through the full network. Prints a warning, since
    bridging is heuristic and provides no bound on the optimality gap.

    Growing from one component and always attaching the nearest other
    one is Prim-style growth. With symmetric additive path costs this
    gives an MST on the component-distance graph, not an RPP optimum
    (a Steiner-style branch off a bridge, or connectivity gained for
    free from the parity matching, can beat it)."""
    comps = sorted(tuple(sorted(c)) for c in nx.connected_components(R))
    bridges = 0
    bridge_len = 0.0
    while len(comps) > 1:
        base = comps[0]
        others = set().union(*comps[1:])
        best = None
        # cheapest link either way out of the growing component
        for reverse in (False, True):
            dist, paths = route_from(router, base, reverse=reverse)
            for n in sorted(others.intersection(dist)):
                candidate = (dist[n], tuple(paths[n]))
                if best is None or candidate < best:
                    best = candidate
        if best is None:
            sys.exit("Service streets are disconnected and no rideable "
                     "edges link them. Extract a larger area, or check "
                     "that a linking street was not excluded (service=x).")
        _, arcs = best
        a, b = arcs[0][0], arcs[-1][1]
        R.add_edge(a, b, key=f"bridge|{bridges}",
                   paths=both_ways(router, a, b))
        bridges += 1
        bridge_len += path_length_m(router["D"], arcs)
        comps = sorted(tuple(sorted(c)) for c in nx.connected_components(R))
    if bridges:
        islands = bridges + 1 - (1 if pinned_endpoints else 0)
        extra = " + the pinned start/end" if pinned_endpoints else ""
        print(f"  warning: required work formed {bridges + 1} components "
              f"({islands} service island(s){extra}); linked by "
              f"{bridges} bridge(s) totalling {bridge_len / 1000:.2f} km."
              f"\n           Heuristic bridging; the kilometres above are "
              f"added riding, not an optimality-gap estimate. "
              f"Global optimum not guaranteed."
              f"\n           Only fix islands "
              f"caused by annotation gaps (a short service=0 sliver "
              f"inside a street you do deliver); never mark a street "
              f"you do not deliver just to merge islands.")
    return bridges, bridge_len


# ----------------------------------------------------------------------
# Phase 2 -- parity repair
# ----------------------------------------------------------------------

def odd_nodes(R):
    return sorted(n for n in R.nodes if R.degree(n) % 2 == 1)


def pairwise_shortest(router, targets):
    """Cheapest cost and node path for every ORDERED pair of targets,
    over the full network (service-0 edges are fair game). Costs are
    asymmetric once a wrong-way penalty is in play, so both directions
    are kept."""
    dist, path = {}, {}
    targets = sorted(set(targets))
    for s in targets:
        d, p = route_from(router, [s])
        for t in targets:
            if t != s:
                if t not in d:
                    sys.exit("Network is disconnected: some service "
                             "streets cannot reach each other at all. "
                             "Extract a larger area, or check service=x "
                             "exclusions.")
                dist[(s, t)] = d[t]
                path[(s, t)] = p[t]
    return dist, path


def symmetrise(dist):
    """Matching needs one number per unordered pair, but the traversal
    direction is only decided later by the Euler tour. Take the cheaper
    direction. Symmetric path costs are unchanged; asymmetric costs
    are relaxed to their smaller value. This does not bound the full
    turn-aware tour objective (see NF2 / B10 in docs/DEVELOPMENT.md)."""
    out = {}
    for (a, b), cost in dist.items():
        out[(a, b)] = out[(b, a)] = min(cost, dist[(b, a)])
    return out


def min_matching(nodes_subset, dist):
    """Minimum-weight perfect matching on a complete graph over the odd
    nodes. This solves the matching subproblem, not the full route
    objective with directional costs, turns or component bridging."""
    if not nodes_subset:
        return [], 0.0
    K = nx.Graph()
    for a, b in itertools.combinations(sorted(nodes_subset), 2):
        K.add_edge(a, b, weight=dist[(a, b)])
    M = sorted(tuple(sorted(pair)) for pair in nx.min_weight_matching(K))
    cost = sum(dist[(a, b)] for a, b in M)
    return M, cost


def parity_repair(R, router, open_route, pin_start=None, pin_end=None):
    """Add matching connectors to R. Returns (endpoints, extra_cost),
    where endpoints is (start, end) for an open route and None for a
    circuit.

    Which nodes must be matched is not a choice: a walk exists exactly
    when every node has even degree apart from its two ends, so the set
    to repair is the odd nodes with the wanted ends flipped in or out
    of it (a T-join). A pinned end therefore belongs IN that set when
    its degree is even -- the round has to finish there, so the parity
    has to be paid for. Choosing the odd node nearest the pin instead
    is what used to finish the round somewhere else entirely."""
    odd = odd_nodes(R)

    def add_connectors(pairs, path):
        for i, (a, b) in enumerate(pairs):
            R.add_edge(a, b, key=f"match|{i}",
                       paths={a: path[(a, b)], b: path[(b, a)]})

    if not open_route:
        if not odd:
            return None, 0.0
        dist, path = pairwise_shortest(router, odd)
        pairs, cost = min_matching(odd, symmetrise(dist))
        add_connectors(pairs, path)
        return None, cost

    # Both ends free and only two odd nodes: they ARE the ends, and
    # nothing is left to match.
    if len(odd) == 2 and pin_start is None and pin_end is None:
        return (odd[0], odd[1]), 0.0

    starts = [pin_start] if pin_start is not None else list(odd)
    ends = [pin_end] if pin_end is not None else list(odd)
    targets = sorted(set(odd) | {n for n in (pin_start, pin_end)
                                 if n is not None})
    if not targets or not starts or not ends:
        return None, 0.0        # nothing to open the walk at: a circuit

    dist, path = pairwise_shortest(router, targets)
    sdist = symmetrise(dist)
    oddset = set(odd)
    best = None
    for a in starts:
        for b in ends:
            if a == b:
                continue
            rest = sorted(oddset.symmetric_difference({a, b}))
            pairs, cost = min_matching(rest, sdist)
            if best is None or cost < best[0]:
                best = (cost, (a, b), pairs)
    if best is None:
        return None, 0.0
    cost, endpoints, pairs = best
    add_connectors(pairs, path)
    return endpoints, cost


# ----------------------------------------------------------------------
# Phase 3 + 4 -- Euler traversal and expansion
# ----------------------------------------------------------------------

VIRTUAL_KEY = "virtual|endpin"


def deadhead_segments(D, arc_path):
    """Turn an arc path into deadhead segments. These are the very arcs
    the router priced, so nothing is re-chosen behind its back."""
    segs = []
    for x, y, key in arc_path:
        data = D[x][y][key]
        segs.append(dict(kind="deadhead", edge_id=key, u=x, v=y,
                         name=data["name"], length=data["length"],
                         wkt=data["wkt"], pass_label="-",
                         highway=data["highway"], against=data["against"],
                         maxspeed=data["maxspeed"]))
    return segs


# ----------------------------------------------------------------------
# Phase 3 -- choosing WHICH Euler tour
# ----------------------------------------------------------------------
#
# Every Euler tour over the same augmented graph covers the same edge
# instances. Physical length is invariant only if each instance expands
# to the same length in either direction. We also prefer fewer awkward
# turns and less riding against mapped traffic flow.
# Hierholzer's algorithm stays correct whichever unused edge is picked
# next (unlike Fleury, which needs bridge avoidance), so a greedy
# preference preserves the Euler coverage, not optimality.
#
# One caveat on "same length": a connector's two directions are two
# separately computed least-cost paths. Directional penalties, turn costs
# or equal-cost alternatives can give them different physical lengths.
# Changing a tour can therefore change riding distance as well as turns;
# there is no general bound that limits this to a few metres.
#
# The wrong-way weight is tied to --wrong-way-penalty rather than being
# a constant: preferring straight continuations pulls the tour INTO the
# long one-way chains of a divided arterial, so unless the rider has
# actually asked to avoid wrong-way riding, the tour should not trade
# turns for it (measured: a weak weight made wrong-way worse, not
# better).

TURN_SECONDS = {"straight": 0.0, "easy": 4.0, "cross": 12.0, "u": 25.0}
# Crossing traffic to enter a road costs what that road's traffic makes
# you wait for, so the cost scales with how fast the road you are
# joining runs. Going straight on or turning with the traffic does not
# cross anything, so it is unaffected.
# Waiting is not riding, and on a footpath round it is a large part of
# the day: crossings are where the time goes. Charged once per
# intersection entered, never per tagged node -- an intersection is 4.5
# tagged nodes on average, so per-node would over-count by that much.
DELAY_SECONDS = {"signals": 25.0, "crossing": 5.0,
                 "give_way": 3.0, "stop": 5.0}


def node_delay(control, previous, node):
    """Seconds lost entering `node` from `previous`. Zero when both are
    already inside the same intersection: you only wait once."""
    here = control.get(node)
    if not here:
        return 0.0
    there = control.get(previous)
    if there and there[1] and there[1] == here[1]:
        return 0.0
    return DELAY_SECONDS.get(here[0], 0.0)


TURN_REFERENCE_KMH = 50.0
TURN_GRADE_RANGE = (0.4, 2.0)


def turn_seconds(kind, entering_kmh=None):
    base = TURN_SECONDS[kind]
    if not base or kind not in ("cross", "u") or not entering_kmh:
        return base
    lo, hi = TURN_GRADE_RANGE
    return base * min(hi, max(lo, entering_kmh / TURN_REFERENCE_KMH))
STRAIGHT_DEG = 30      # within this, it is not a turn
U_TURN_DEG = 150       # beyond this, it is a U-turn
WRONG_WAY_TOUR_WEIGHT = 0.5   # seconds per metre, per unit of penalty
# B3: doing both sides of a street back to back keeps its mail in one
# bundle. It means turning at the far end and riding back, so the bonus
# has to outweigh the U-turn it implies. Distance is unchanged only
# when directional connector expansions have equal lengths.
PAIR_SECONDS = 30.0


def turn_kind(bearing_in, bearing_out, traffic_side="left"):
    """straight / easy / cross / u. The `cross` turn is the one that
    cuts the oncoming stream: right where traffic keeps left."""
    if bearing_in is None or bearing_out is None:
        return "straight"
    delta = (bearing_out - bearing_in + 180) % 360 - 180
    if abs(delta) <= STRAIGHT_DEG:
        return "straight"
    if abs(delta) >= U_TURN_DEG:
        return "u"
    turning_right = delta > 0
    crosses = turning_right if traffic_side == "left" else not turning_right
    return "cross" if crosses else "easy"


WINDS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def ring_area(chain):
    """Twice the signed area of a closed chain, in a local east-north
    frame. Positive means anticlockwise."""
    k = math.cos(math.radians(chain[0][0]))
    return sum((x1 * k) * y2 - (x2 * k) * y1
               for (y1, x1), (y2, x2) in zip(chain, chain[1:]))


def kerb_side(chain, reference_side="left", closed=False):
    """Coarse label for a side of the fixed reference chain, not travel.

    Curves use the endpoint bearing only for this display
    label; the local polyline tangent defines the physical delivery side.
    A closed way's left side is inside an anticlockwise ring.
    """
    if closed:
        area = ring_area(chain) if len(chain) > 2 else 0.0
        if not area:
            return ""            # a loop with no shape: nothing to name
        anticlockwise = area > 0
        inner = anticlockwise if reference_side == "left" else not anticlockwise
        return "inner" if inner else "outer"
    run = bearing_of(chain[0], chain[-1]) if len(chain) > 1 else None
    if run is None:
        return ""
    return WINDS[round((run + (-90 if reference_side == "left" else 90))
                       / 45) % 8]


def bearing_of(a, b):
    return None if a == b else (
        math.degrees(math.atan2(
            (b[1] - a[1]) * math.cos(math.radians((a[0] + b[0]) / 2)),
            b[0] - a[0])) % 360)


def tour_meta(R, F, D, nodes, traffic_side="left"):
    """Per required-edge geometry facts the tour chooser needs: the
    bearing leaving u, the bearing arriving at v, and direction costs.
    Service costs follow the task's physical side, independently of travel.
    """
    meta = {}
    blank = {"out": None, "in": None, "against_m": 0.0}
    for u, v, key, data in R.edges(keys=True, data=True):
        loop_reverse = False
        if "base" not in data and "paths" not in data:
            meta[key] = {u: blank, v: blank}   # the virtual end->start
            continue
        if "paths" in data:
            chains, against = {}, {}
            for start, arcs in data["paths"].items():
                chains[start] = ([nodes[arcs[0][0]]] +
                                 [nodes[y] for _, y, _ in arcs]) \
                    if arcs else [nodes[start], nodes[start]]
                against[start] = sum(D[x][y][k]["length"]
                                     for x, y, k in arcs
                                     if D[x][y][k]["against"])
            direction_cost = against
        else:
            edata = F[u][v][data["base"]]
            chain = segment_coords({"wkt": edata["wkt"], "u": u, "v": v},
                                   nodes)
            chains = {u: chain, v: list(reversed(chain))}
            if u == v:
                # A self-loop has no endpoint direction. Keep an explicit
                # orientation per task, preferring the mapped traffic flow.
                loop_reverse = (not edata["oneway"] and
                                data["delivery_side"] != traffic_side)
                chains = {u: list(reversed(chain)) if loop_reverse else chain}
            wrong = edata["length"] if edata["oneway"] else 0.0
            against = {u: 0.0 if not edata["oneway"] or u == edata["cu"]
                       else wrong,
                       v: 0.0 if not edata["oneway"] or v == edata["cu"]
                       else wrong}
            direction_cost = {
                start: edata["length"] if counterflow(
                    data["delivery_side"], not loop_reverse if u == v
                    else start == edata["cu"], edata["oneway"], traffic_side)
                else 0.0 for start in chains}
        info = {"base": data.get("base"),
                "loop_reverse": loop_reverse,
                "street": (F[u][v][data["base"]]["name"]
                           if "base" in data else None),
                "kmh": (F[u][v][data["base"]].get("kmh")
                        if "base" in data else None)}
        for start, chain in chains.items():
            info[start] = {
                "out": bearing_of(chain[0], chain[1]) if len(chain) > 1
                else None,
                "in": bearing_of(chain[-2], chain[-1]) if len(chain) > 1
                else None,
                "against_m": against.get(start, 0.0),
                "direction_cost_m": direction_cost.get(start, 0.0),
            }
        meta[key] = info
    return meta


def euler_tour(R, source, meta, traffic_side="left", optimise=True,
               wrong_way_penalty=1.0, first_key=None,
               pair_passes=False, street_first=False):
    """Hierholzer, picking the next edge by turn comfort, by not riding
    one-way streets backwards, and by finishing a two-sided street
    while you are on it. Returns [(u, v, key), ...].

    The service work is fixed, but directional connector paths can differ.
    With optimise=False the first available edge is taken.
    """
    adj = defaultdict(list)
    for u, v, key in R.edges(keys=True):
        adj[u].append((v, key))
        if u != v:
            adj[v].append((u, key))
    used = set()
    wrong_weight = max(0.0, wrong_way_penalty - 1.0) * WRONG_WAY_TOUR_WEIGHT

    def run_against(node, key):
        """Counterflow metres committed to by taking `key` from
        `node`: most nodes have degree 2 and offer no further choice,
        so entering a chain the wrong way commits to all of it. Judging
        only the first edge is what made the tour prefer to slide into
        long backwards runs on a divided arterial."""
        total, seen = 0.0, set()
        while key is not None and key not in seen:
            seen.add(key)
            total += meta.get(key, {}).get(node, {}).get("direction_cost_m", 0.0)
            nxt = next((n for n, k in adj[node] if k == key), None)
            if nxt is None or len(adj[nxt]) != 2:
                break
            node, key = nxt, next((k for _, k in adj[nxt] if k != key),
                                  None)
        return total

    def choose(node, bearing_in, prev_key=None):
        if first_key is not None and not used:
            forced = next(((i, n, k) for i, (n, k) in enumerate(adj[node])
                           if k == first_key), None)
            if forced:
                return forced
        best = None
        for i, (nbr, key) in enumerate(adj[node]):
            if key in used:
                continue
            if not optimise:
                return i, nbr, key
            info = meta.get(key, {}).get(node, {})
            score = (turn_seconds(turn_kind(bearing_in, info.get("out"),
                                            traffic_side),
                                  meta.get(key, {}).get("kmh"))
                     + wrong_weight * (run_against(node, key)
                                       if wrong_weight else 0.0))
            if street_first and prev_key is not None:
                street = meta.get(key, {}).get("street")
                if street and meta.get(prev_key, {}).get("street") == street:
                    score -= STREET_CONTINUE_SECONDS   # finish the street
            if pair_passes and prev_key is not None:
                base = meta.get(key, {}).get("base")
                if base and meta.get(prev_key, {}).get("base") == base:
                    score -= PAIR_SECONDS        # the other side of it
            if best is None or score < best[0]:
                best = (score, i, nbr, key)
        return None if best is None else best[1:]

    stack = [(source, None, None)]        # node, key used to get here
    out = []
    while stack:
        node, in_key, bearing_in = stack[-1]
        picked = choose(node, bearing_in, in_key)
        if picked is None:
            stack.pop()
            if stack and in_key is not None:
                out.append((stack[-1][0], node, in_key))
        else:
            _, nbr, key = picked
            used.add(key)
            stack.append((nbr, key,
                          meta.get(key, {}).get(node, {}).get("in")))
    out.reverse()
    return out



# ----------------------------------------------------------------------
# Choosing WHICH Euler tour, part two: search instead of guess
# ----------------------------------------------------------------------
#
# The greedy chooser samples one tour. Searching may improve the chosen
# objective while preserving edge coverage; distance can also change
# when connectors expand differently in the two directions.
#
# Two moves keep the walk valid, and both act on a CLOSED sub-walk --
# a stretch that leaves a node and comes back to it:
#
#   reverse   ride that loop the other way round
#   relocate  do it at a different visit of the same node
#
# Nothing else is needed: any Euler tour is reachable from any other by
# such moves, and neither changes which edges are ridden.

STREET_RUN_SECONDS = 20.0   # what breaking a street into one more
                            # visit costs the person sorting the mail
# Staying on a street until it is finished is what keeps its mail in one
# bundle. Rearranging a finished tour cannot deliver this -- reversing
# loops does not restructure at street scale -- but biasing the
# construction can, because that is where the structure comes from.
STREET_CONTINUE_SECONDS = 45.0


def tour_nodes(euler):
    return [euler[0][0]] + [v for _, v, _ in euler] if euler else []


def tour_score(euler, meta, control, traffic_side, names,
               wrong_weight=0.0, run_weight=STREET_RUN_SECONDS):
    """Everything the tour can still influence, in seconds. Distance is
    absent on purpose -- it is the same for every tour."""
    turn = delay = wrong = 0.0
    runs = 0
    previous_in = previous_node = last_street = None
    for u, v, key in euler:
        info = meta.get(key, {})
        side = info.get(u, {})
        if previous_in is not None:
            turn += turn_seconds(
                turn_kind(previous_in, side.get("out"), traffic_side),
                info.get("kmh"))
        if control and previous_node is not None:
            delay += node_delay(control, previous_node, u)
        wrong += side.get("direction_cost_m", 0.0)
        base = info.get("base")
        if base:
            street = names.get(base)
            if street != last_street:
                runs += 1
            last_street = street
        if side.get("in") is not None:
            previous_in = side["in"]
        previous_node = u
    return turn + delay + wrong_weight * wrong + run_weight * runs


def local_search_tour(euler, meta, control, traffic_side, names,
                      wrong_weight=0.0, run_weight=STREET_RUN_SECONDS,
                      iterations=4000, seed=12345):
    """Hill-climb over closed sub-walks. Returns (moves kept, before,
    after) so the gain can be reported honestly."""
    rnd = random.Random(seed)
    score = tour_score(euler, meta, control, traffic_side, names,
                       wrong_weight, run_weight)
    before, kept = score, 0
    best_score, best_tour, temperature = score, list(euler), 1.0
    for _ in range(iterations):
        seq = tour_nodes(euler)
        spots = defaultdict(list)
        for i, node in enumerate(seq):
            spots[node].append(i)
        repeated = [v for v in spots.values() if len(v) > 1]
        if not repeated:
            break
        # consecutive visits of the same node bound the SMALLEST closed
        # sub-walks, and those are the ones worth shuffling: picking two
        # visits at random mostly proposes huge blocks that never improve
        where = rnd.choice(repeated)
        at = rnd.randrange(len(where) - 1)
        a, b = where[at], where[at + 1]
        if b - a < 2:
            continue
        block = euler[a:b]
        if rnd.random() < 0.5:
            trial = (euler[:a]
                     + [(y, x, k) for x, y, k in reversed(block)]
                     + euler[b:])
        else:
            rest = euler[:a] + euler[b:]
            rest_seq = tour_nodes(rest)
            landing = [i for i, node in enumerate(rest_seq)
                       if node == seq[a] and i != a]
            if not landing:
                continue
            at = rnd.choice(landing)
            trial = rest[:at] + block + rest[at:]
        trial_score = tour_score(trial, meta, control, traffic_side, names,
                                 wrong_weight, run_weight)
        delta = trial_score - score
        # Strict hill climbing settles within a few dozen moves, so a
        # cooling schedule is what makes the budget worth spending.
        if delta < -1e-9 or (temperature > 0 and delta < 40 * temperature
                             and rnd.random() < math.exp(-delta
                                                         / max(temperature
                                                               * 40, 1e-9))):
            euler[:] = trial
            score = trial_score
            if score < best_score:
                best_score, best_tour = score, list(euler)
            kept += 1
        temperature = max(0.0, temperature - 1.0 / max(iterations, 1))
    euler[:] = best_tour
    return kept, before, best_score


def traverse(F, R, D, nodes, endpoints, start_node, pin_start=None,
             traffic_side="left", optimise_tour=True,
             wrong_way_penalty=1.0, pair_passes=False, control=None,
             search_iterations=4000, street_first=False):
    """Walk the Euler circuit/path and expand connectors into real
    street segments. Returns a list of segment dicts.

    pin_start: set when a virtual end->start edge is in R (SPEC-2).
    The circuit is rotated so the virtual edge would be last, the edge
    is dropped, and the walk is oriented to begin at pin_start -- what
    remains is the open route start -> ... -> end."""
    meta = tour_meta(R, F, D, nodes, traffic_side)
    if endpoints:
        source = endpoints[0]
        assert nx.has_eulerian_path(R, source=source), \
            "the matching left a graph with no walk from the start"
        first = None
    elif pin_start is not None:
        # SPEC-2: start the circuit at the pinned END and cross the
        # virtual end->start edge immediately, so dropping that first
        # step leaves a walk that already runs start -> ... -> end.
        # (Rotating afterwards and reversing when the circuit happened
        # to cross the virtual edge the other way used to mirror every
        # traversal, throwing away the tour's chosen directions.)
        source = next(n for n in R.neighbors(pin_start)
                      if VIRTUAL_KEY in R[pin_start][n])
        first = VIRTUAL_KEY
        assert nx.is_eulerian(R)
    else:
        assert nx.is_eulerian(R)
        source = start_node if (start_node in R) else next(iter(R.nodes))
        first = None
    euler = euler_tour(R, source, meta, traffic_side, optimise_tour,
                       wrong_way_penalty, first, pair_passes, street_first)
    if optimise_tour and search_iterations:
        names = {d["base"]: F[u][v][d["base"]]["name"]
                 for u, v, k, d in R.edges(keys=True, data=True)
                 if "base" in d}
        head = euler[:1] if pin_start is not None else []
        body = euler[len(head):]
        kept, before, after = local_search_tour(
            body, meta, control, traffic_side, names,
            max(0.0, wrong_way_penalty - 1.0) * WRONG_WAY_TOUR_WEIGHT,
            iterations=search_iterations)
        euler = head + body
        if kept:
            print(f"  tour search: {kept} improvements, "
                  f"{(before - after) / 60:.1f} min of turning, waiting "
                  f"and re-visits removed at no extra distance")

    if pin_start is not None:
        assert euler[0][2] == VIRTUAL_KEY, "virtual edge must be first"
        finish = euler[0][0]           # the virtual edge runs end->start
        euler = euler[1:]
        assert not euler or euler[0][0] == pin_start, \
            "the walk must begin at the pinned start"
        assert not euler or euler[-1][1] == finish, \
            "the walk must finish at the pinned end"
    elif endpoints:
        assert euler[0][0] == endpoints[0], \
            "the walk must begin at the chosen start"
        assert euler[-1][1] == endpoints[1], \
            "the walk must finish at the chosen end"

    segments = []
    passes_seen = defaultdict(int)

    for u, v, key in euler:
        data = R[u][v][key]
        if "paths" in data:                          # connector -> expand
            # each connector carries a path for either direction of
            # travel; they differ once wrong-way costs are in play
            segments.extend(deadhead_segments(D, data["paths"][u]))
        else:                                        # service pass
            base = data["base"]
            edata = F[u][v][base]
            passes_seen[base] += 1
            total = edata["service"]
            reverse = meta[key]["loop_reverse"] if u == v else False
            forward = not reverse if u == v else u == edata["cu"]
            side = data["delivery_side"]
            reference = segment_coords({"u": edata["cu"], "v": edata["cv"],
                                        "wkt": edata["wkt"]}, nodes)
            segments.append(dict(reverse=reverse,
                                 kind="service", edge_id=base,
                                 delivery_side=side,
                                 travel_direction="forward" if forward else "reverse",
                                 travel_side=travel_side(side, forward),
                                 counterflow=counterflow(side, forward,
                                                         edata["oneway"], traffic_side),
                                 side=kerb_side(reference, side, closed=u == v) or side,
                                 u=u, v=v, name=edata["name"],
                                 length=edata["length"], wkt=edata["wkt"],
                                 pass_label=f"{passes_seen[base]}/{total}",
                                 highway=edata["highway"],
                                 maxspeed=edata["maxspeed"],
                                 against=bool(edata["oneway"]) and not forward))
    return segments


def annotate_turns(segments, nodes, traffic_side="left", profile=None,
                   control=None):
    """Record the turn made when entering each segment, and count them.
    Done on the expanded route, so turns inside connectors count too."""
    counts = defaultdict(int)
    previous = None
    for i, s in enumerate(segments):
        chain = segment_coords(s, nodes)
        out = bearing_of(chain[0], chain[1]) if len(chain) > 1 else None
        into = bearing_of(chain[-2], chain[-1]) if len(chain) > 1 else None
        s["turn"] = ("start" if i == 0 else
                     turn_kind(previous, out, traffic_side))
        s["turn_s"] = 0.0 if i == 0 else turn_seconds(
            s["turn"], speed_kmh(s.get("highway", ""), s.get("maxspeed"),
                                 profile) if profile else None)
        s["wait_s"] = (node_delay(control, s["u"], s["v"])
                       if control else 0.0)
        if s["kind"] != "service":
            s["side"] = ""
        if i:
            counts[s["turn"]] += 1
        previous = into if into is not None else previous
    return counts


def direction_metrics(segments):
    """Keep one-way riding and service-side counterflow distinct (they overlap)."""
    service_cf = [s for s in segments
                  if s["kind"] == "service" and s.get("counterflow")]
    return {
        "wrong_way_km": round(sum(s["length"] for s in segments
                                  if s.get("against")) / 1000, 3),
        "service_counterflow_km": round(sum(s["length"] for s in service_cf) / 1000, 3),
        "service_counterflow_segments": len(service_cf),
    }


def operational_metrics(segments):
    """How pleasant the route is to *work*, which distance cannot see.

    A street whose two sides are delivered far apart in the sequence has
    its mail split across two bundles, and a street entered three times
    is three places in the sort frame. Neither costs a metre."""
    service = [s for s in segments if s["kind"] == "service"]
    at = defaultdict(list)
    for i, s in enumerate(service):
        at[s["edge_id"]].append(i)
    both = [v for v in at.values() if len(v) == 2]
    paired = sum(1 for v in both if v[1] - v[0] == 1)
    kerbs = sum(1 for v in both
                if {service[i].get("delivery_side") for i in v} == {"left", "right"})

    runs, streets = 0, set()
    previous = None
    for s in service:
        streets.add(s["name"])
        if s["name"] != previous:
            runs += 1
        previous = s["name"]
    return {
        "paired_pct": round(100 * paired / len(both), 1) if both else 100.0,
        "two_sided": len(both),
        "both_sides": kerbs,
        "street_runs": runs,
        "streets": len(streets),
        "runs_per_street": round(runs / len(streets), 2) if streets else 0,
    }


def estimate_seconds(segments, profile):
    """Rough riding time: metres at the profile's speeds, plus a fixed
    cost per turn. Excludes every stop at a letterbox, which on a real
    round dominates -- useful for comparing routes, not for planning a
    day."""
    table = profile
    riding = sum(s["length"] /
                 (speed_kmh(s.get("highway", ""), s.get("maxspeed"),
                            table) / 3.6)
                 for s in segments)
    turning = sum(s.get("turn_s", 0.0) for s in segments)
    waiting = sum(s.get("wait_s", 0.0) for s in segments)
    return riding, turning, waiting


def hhmm(seconds):
    m = int(round(seconds / 60))
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


# ----------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------

def bearing_label(a, b):
    (lat1, lon1), (lat2, lon2) = a, b
    dy = lat2 - lat1
    dx = (lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    ang = math.degrees(math.atan2(dx, dy)) % 360
    return WINDS[round(ang / 45) % 8]


def cross_streets(F, node, current_name):
    names = {d["name"] for _, _, d in F.edges(node, data=True)}
    names.discard(current_name)
    if not names:
        return "(dead end)"
    return " / ".join(sorted(names)[:2])


def segment_coords(seg, nodes):
    """[(lat, lon), ...] for drawing, honouring curved geometry.
    `reverse` rides it against the way it is drawn, which is the only
    way to tell the two laps of a closed way apart."""
    if seg.get("reverse"):
        return list(reversed(segment_coords(dict(seg, reverse=False),
                                            nodes)))
    return oriented_coords({"u": seg["u"], "v": seg["v"],
                            "geometry_wkt": seg["wkt"]}, nodes)


def travel_label(seg, nodes):
    """Compass direction of travel. A closed way ends where it began,
    so its endpoints say nothing -- the honest answer there is the way
    you set off, which is also what tells its two laps apart."""
    if seg["u"] != seg["v"]:
        return bearing_label(nodes[seg["u"]], nodes[seg["v"]])
    chain = segment_coords(seg, nodes)
    return bearing_label(chain[0], chain[1]) if len(chain) > 1 else ""


def write_route_csv(segments, F, nodes, out_dir: Path):
    path = out_dir / "route.csv"
    cum = 0.0
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["seq", "type", "street", "pass", "side",
                    "direction", "from_cross", "to_cross", "length_m",
                    "cum_km", "against_oneway", "edge_id", "delivery_side",
                    "travel_direction", "travel_side", "counterflow"])
        for i, s in enumerate(segments, 1):
            cum += s["length"]
            w.writerow([
                i, s["kind"], s["name"], s["pass_label"],
                s.get("side", ""), travel_label(s, nodes),
                cross_streets(F, s["u"], s["name"]),
                cross_streets(F, s["v"], s["name"]),
                round(s["length"], 1), round(cum / 1000, 3),
                "yes" if s.get("against") else "", s["edge_id"],
                s.get("delivery_side", ""), s.get("travel_direction", ""),
                s.get("travel_side", ""), "yes" if s.get("counterflow") else "",
            ])
    return path


ROUTE_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>route viewer</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html, body { height: 100%; margin: 0; }
  #map { height: 100%; }
  #panel {
    position: absolute; top: 10px; left: 50px; z-index: 1000;
    background: rgba(255,255,255,.96); border: 1px solid #bbb;
    border-radius: 6px; padding: 10px 12px; width: 366px;
    box-sizing: border-box; max-width: calc(100vw - 60px);
    max-height: calc(100dvh - 20px); overflow: auto; overflow-wrap: anywhere;
    font: 13px/1.5 system-ui, sans-serif; box-shadow: 0 1px 4px rgba(0,0,0,.2);
  }
  #panel b { font-size: 14px; }
  .row { margin-top: 6px; }
  #slider { width: 100%; }
  #info { min-height: 3em; }
  button { cursor: pointer; }
  .sw { display: inline-block; width: 16px; height: 4px;
        vertical-align: middle; margin-right: 3px; }
  #hint { color: #666; margin-top: 6px; }
</style>
</head>
<body>
<div id="map"></div>
<div id="panel">
  <b>route viewer</b>
  <div id="totals"></div>
  <div style="color:#666">Heuristic route; global optimum not guaranteed.</div>
  <div class="row">
    <button id="prev">&#9664;</button>
    <button id="play">play</button>
    <button id="next">&#9654;</button>
    <label style="margin-left:8px"><input type="checkbox" id="follow">
      follow</label>
  </div>
  <input type="range" id="slider" min="1" value="1">
  <div class="row" id="info"></div>
  <div class="row">
    <span class="sw" style="background:#2b6cb0"></span>service
    <span class="sw" style="background:#dd6b20;margin-left:8px"></span>deadhead
    <span class="sw" style="background:#7b341e;margin-left:8px"></span>against one-way
    <span class="sw" style="background:#e53e3e;margin-left:8px"></span>current
    <span class="sw" style="background:#b8c2cc;margin-left:8px"></span>not yet
  </div>
  <div id="hint">drag the slider or press &larr;/&rarr; to walk the
    route step by step; hover any line for its step number</div>
</div>
<script id="route-data" type="application/json">__PAYLOAD__</script>
<script>
"use strict";
const P = JSON.parse(document.getElementById("route-data").textContent);
const S = P.segs;
const N = S.length;

const renderer = L.canvas({tolerance: 5});
const map = L.map("map", {renderer: renderer, maxZoom: 22});
L.tileLayer("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
  {maxZoom: 22, maxNativeZoom: 20,
   attribution: "&copy; OpenStreetMap contributors &copy; CARTO"}).addTo(map);

function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
                  .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

const UNVISITED = {color: "#b8c2cc", weight: 2, opacity: 0.55};
function visitedStyle(s) {
  if (s.w) {  // against mapped flow; this is not an access ruling
    return {color: "#7b341e", weight: s.k === "s" ? 5 : 4, opacity: 1,
            dashArray: "2 4"};
  }
  return s.k === "s"
    ? {color: "#2b6cb0", weight: 4, opacity: 0.9, dashArray: null}
    : {color: "#dd6b20", weight: 3, opacity: 0.9, dashArray: "5 7"};
}

const lines = S.map(function (s, i) {
  const pl = L.polyline(s.c, UNVISITED).addTo(map);
  pl.bindTooltip("#" + (i + 1) + " " + esc(s.n) + " &middot; " +
    (s.k === "s" ? "pass " + esc(s.p) + " &middot; " + serviceSide(s) : "deadhead") +
    " &middot; " + s.l + " m" +
    (s.w ? " &middot; <b>against the one-way</b>" : ""), {sticky: true});
  return pl;
});
map.fitBounds(L.featureGroup(lines).getBounds());

const hl = L.polyline(S[0].c,
  {color: "#e53e3e", weight: 7, opacity: 1}).addTo(map);
const arrow = L.marker(S[0].c[S[0].c.length - 1],
  {interactive: false, icon: L.divIcon({className: "", html: ""})})
  .addTo(map);

const first = S[0].c[0];
const last = S[N - 1].c[S[N - 1].c.length - 1];
const closed = Math.abs(first[0] - last[0]) < 1e-7 &&
               Math.abs(first[1] - last[1]) < 1e-7;
if (closed) {
  L.circleMarker(first, {radius: 8, color: "#2f855a", fillColor: "#48bb78",
    fillOpacity: 1}).addTo(map).bindTooltip("START = END (circuit)");
} else {
  L.circleMarker(first, {radius: 8, color: "#2f855a", fillColor: "#48bb78",
    fillOpacity: 1}).addTo(map).bindTooltip("START");
  L.circleMarker(last, {radius: 8, color: "#9b2c2c", fillColor: "#f56565",
    fillOpacity: 1}).addTo(map).bindTooltip("END");
}

const nCross = S.filter(s => s.t === "cross").length;
const nU = S.filter(s => s.t === "u").length;
document.getElementById("totals").innerHTML =
  "service <b>" + P.service_km.toFixed(2) + " km</b> &middot; total <b>" +
  P.total_km.toFixed(2) + " km</b> &middot; deadhead <b>" +
  (P.total_km - P.service_km).toFixed(2) + " km</b>" +
  (P.time_min ? "<br>riding <b>" + Math.floor(P.time_min / 60) + "h " +
   String(Math.round(P.time_min % 60)).padStart(2, "0") +
   "m</b> (excludes stops) &middot; " + nCross + " crossing turns, " +
   nU + " U-turns" : "") +
  "<br>against mapped one-way <b>" + P.wrong_way_km.toFixed(2) + " km</b>" +
  '<br><span title="Service against reference traffic flow; not an access restriction">' +
  "service counterflow <b>" + P.service_counterflow_km.toFixed(2) + " km</b> (" +
  P.service_counterflow_segments + " segments)</span>";

function arrowAngle(c) {
  const a = c[c.length - 2], b = c[c.length - 1];
  const dy = b[0] - a[0];
  const dx = (b[1] - a[1]) *
    Math.cos((a[0] + b[0]) / 2 * Math.PI / 180);
  return Math.atan2(dx, dy) * 180 / Math.PI;
}

function serviceSide(s) {
  return "deliver <b>" + esc(s.ds) + "</b> of reference (" + esc(s.side) +
    "); <b>" + esc(s.ts) + "</b> while riding " + esc(s.td);
}

let cur = 1;
const slider = document.getElementById("slider");
slider.max = N;

function setStep(k) {
  cur = Math.max(1, Math.min(N, k));
  for (let i = 0; i < N; i++) {
    lines[i].setStyle(i < cur ? visitedStyle(S[i]) : UNVISITED);
  }
  const s = S[cur - 1];
  hl.setLatLngs(s.c);
  const end = s.c[s.c.length - 1];
  arrow.setLatLng(end);
  arrow.setIcon(L.divIcon({className: "", iconSize: [24, 24],
    iconAnchor: [12, 12],
    html: '<div style="transform:rotate(' + arrowAngle(s.c).toFixed(0) +
          'deg);font-size:20px;line-height:24px;text-align:center;' +
          'color:#e53e3e;text-shadow:0 0 3px #fff">&#9650;</div>'}));
  document.getElementById("info").innerHTML =
    "<b>#" + cur + "/" + N + "</b> &middot; " + esc(s.n) + "<br>" +
    (s.k === "s" ? "service pass " + esc(s.p) : "deadhead (connector)") +
    (s.k === "s" ? "<br>" + serviceSide(s) : "") +
    " &middot; " + s.l + " m &middot; heading " + s.dir +
    " &middot; cum " + s.cum.toFixed(3) + " km" +
    (s.t === "cross" ? " &middot; <b>turn across traffic</b>"
     : s.t === "u" ? " &middot; <b>U-turn</b>" : "") +
    (s.cf ? '<br><span title="Traffic-flow reference, not an access restriction"><b>service counterflow</b></span>' : "") +
    (s.w ? '<br><span style="color:#7b341e"><b>against the mapped one-way</b></span>' : "");
  slider.value = cur;
  if (document.getElementById("follow").checked) map.panTo(end);
}

slider.addEventListener("input", function () { setStep(+this.value); });
document.getElementById("prev").onclick = function () { setStep(cur - 1); };
document.getElementById("next").onclick = function () { setStep(cur + 1); };

let timer = null;
document.getElementById("play").onclick = function () {
  if (timer) {
    clearInterval(timer);
    timer = null;
    this.textContent = "play";
    return;
  }
  if (cur >= N) cur = 0;
  this.textContent = "pause";
  timer = setInterval(function () {
    if (cur >= N) {
      clearInterval(timer);
      timer = null;
      document.getElementById("play").textContent = "play";
      return;
    }
    setStep(cur + 1);
  }, 350);
};
document.addEventListener("keydown", function (ev) {
  if (ev.target.tagName === "INPUT") return;
  if (ev.key === "ArrowLeft") { setStep(cur - 1); ev.preventDefault(); }
  if (ev.key === "ArrowRight") { setStep(cur + 1); ev.preventDefault(); }
});

setStep(1);
</script>
</body>
</html>
"""


def write_map(segments, nodes, out_dir: Path, time_min=0.0):
    """Self-contained interactive viewer: slider/play steps through the
    walk, the current segment is highlighted with a direction arrow."""
    segs, cum = [], 0.0
    for s in segments:
        coords = segment_coords(s, nodes)
        cum += s["length"]
        segs.append({
            "n": s["name"],
            "k": "s" if s["kind"] == "service" else "d",
            "p": s["pass_label"],
            "ds": s.get("delivery_side", ""),
            "side": s.get("side", ""),
            "td": s.get("travel_direction", ""),
            "ts": s.get("travel_side", ""),
            "cf": bool(s.get("counterflow")),
            "l": round(s["length"], 1),
            "cum": round(cum / 1000, 3),
            "dir": travel_label(s, nodes),
            "w": 1 if s.get("against") else 0,
            "t": s.get("turn", "straight"),
            "c": [[round(lat, 6), round(lon, 6)] for lat, lon in coords],
        })
    service = sum(s["length"] for s in segments if s["kind"] == "service")
    payload = json.dumps(
        {"segs": segs,
         "service_km": round(service / 1000, 2),
         "total_km": round(cum / 1000, 2),
         "time_min": round(time_min, 1), **direction_metrics(segments)},
        ensure_ascii=False).replace("</", "<\\/")
    path = out_dir / "route_map.html"
    path.write_text(ROUTE_TEMPLATE.replace("__PAYLOAD__", payload),
                    encoding="utf-8")
    return path


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def snap_to_node(latlon_str, candidates, nodes):
    lat, lon = (float(x) for x in latlon_str.split(","))
    return min(candidates,
               key=lambda n: (nodes[n][0] - lat) ** 2 +
                             (nodes[n][1] - lon) ** 2)


def snap_metres(latlon_str, node, nodes):
    """How far the pin had to move to land on a junction. Worth saying
    out loud: the round starts where the node is, not where you
    clicked, and a pin dropped away from the network can move a long
    way without anything looking wrong."""
    lat, lon = (float(x) for x in latlon_str.split(","))
    y, x = nodes[node]
    return math.hypot((y - lat) * 110540.0,
                      (x - lon) * 111320.0 * math.cos(math.radians(lat)))


def summarise_result(res, nodes, args, profile, control=None):
    """`profile` is the common yardstick, not the one this variant was
    optimised with -- otherwise the times beside the options would be
    measured in different units and could not be compared."""
    """The numbers one algorithm's route is judged on."""
    segments = res["segments"]
    turns = annotate_turns(segments, nodes, args.traffic_side, profile,
                           control)
    ops = operational_metrics(segments)
    riding, turning, waiting = estimate_seconds(segments, profile)
    total = sum(s["length"] for s in segments)
    return {
        "algorithm": res["algorithm"],
        "variant": res["variant"]["name"],
        "label": res["variant"]["label"],
        "wrong_way_penalty": res["variant"]["wrong_way_penalty"],
        "wrong_way_source": res["variant"]["wrong_way_source"],
        "total_km": round(total / 1000, 3),
        "deadhead_km": round((total - res["service_len"]) / 1000, 3),
        "time_min": round((riding + turning + waiting) / 60, 1),
        "turning_min": round(turning / 60, 1),
        "waiting_min": round(waiting / 60, 1),
        "turns_cross": turns["cross"], "turns_u": turns["u"],
        **direction_metrics(segments),
        "paired_pct": ops["paired_pct"], "street_runs": ops["street_runs"],
        "turns": turns, "ops": ops, "riding_s": riding,
        "turning_s": turning,
    }


def print_comparison(rows):
    print(f"\n============ ROUTE PREFERENCES ============"
          f"   (all measured with the '{SCORE_PROFILE}' profile)")
    print(f"{'preference':<32}{'total':>9}{'dead':>8}{'time':>8}"
          f"{'X-turn':>8}{'U':>5}{'oneway':>8}{'svc-cf':>8}")
    for r in rows:
        print(f"{r['label'][:30]:<32}{r['total_km']:>8.2f}k"
              f"{r['deadhead_km']:>7.2f}k{hhmm(r['time_min'] * 60):>8}"
              f"{r['turns_cross']:>8}{r['turns_u']:>5}"
              f"{r['wrong_way_km']:>7.2f}k{r['service_counterflow_km']:>7.2f}k")
    print("oneway: against mapped one-ways, all riding; "
          "svc-cf: service against reference traffic flow (overlapping metrics).")
    best = min(rows, key=lambda r: r["time_min"])
    print(f"\nLowest estimated time among these candidates: {best['label']} "
          f"({hhmm(best['time_min'] * 60)}). "
          f"Every option covers the same streets, so what differs is "
          f"only\nthe overhead -- same job, different way of getting "
          f"between the work.")


COMPARE_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>route alternatives</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html, body { height: 100%; margin: 0; }
  #map { height: 100%; }
  #panel { position: absolute; top: 10px; left: 50px; z-index: 1000;
    background: rgba(255,255,255,.97); border: 1px solid #bbb;
    border-radius: 6px; padding: 10px 12px; width: 354px;
    box-sizing: border-box; max-width: calc(100vw - 60px);
    max-height: calc(100dvh - 20px); overflow: auto; overflow-wrap: anywhere;
    font: 13px/1.5 system-ui, sans-serif;
    box-shadow: 0 1px 4px rgba(0,0,0,.2); }
  .opt { border: 1px solid #ddd; border-radius: 5px; padding: 7px 9px;
    margin-top: 7px; cursor: pointer; }
  .opt.on { border-color: #2b6cb0; background: #ebf4fb; }
  .opt b { font-size: 14px; }
  .num { color: #555; }
  .best { color: #2f855a; font-weight: 600; }
  @media (max-width: 600px) { #panel { max-height: 55dvh; } }
</style>
</head>
<body>
<div id="map"></div>
<div id="panel">
  <b>route alternatives</b>
  <div style="color:#666">Heuristic candidates; global optimum not guaranteed.</div>
  <div style="color:#666">same streets, same work &mdash; only the way
    between them differs</div>
  <div id="opts"></div>
</div>
<script id="alt-data" type="application/json">__PAYLOAD__</script>
<script>
"use strict";
const P = JSON.parse(document.getElementById("alt-data").textContent);
const renderer = L.canvas({tolerance: 4});
const map = L.map("map", {renderer: renderer, maxZoom: 22});
L.tileLayer("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
  {maxZoom: 22, maxNativeZoom: 20,
   attribution: "&copy; OpenStreetMap contributors &copy; CARTO"}).addTo(map);

function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
                  .replace(/>/g, "&gt;");
}
const layers = P.routes.map(function (r) {
  const g = L.featureGroup();
  r.segs.forEach(function (s) {
    L.polyline(s.c, s.k === "s"
      ? {color: "#2b6cb0", weight: 4, opacity: 0.9}
      : {color: "#dd6b20", weight: 3, opacity: 0.9, dashArray: "5 7"}
    ).addTo(g);
  });
  return g;
});
const quickest = P.routes.reduce((a, b) => b.time_min < a.time_min ? b : a);

function show(i) {
  layers.forEach(function (g, j) {
    if (j === i) { g.addTo(map); } else if (map.hasLayer(g)) {
      map.removeLayer(g);
    }
  });
  document.querySelectorAll(".opt").forEach(function (el, j) {
    el.classList.toggle("on", j === i);
  });
}
document.getElementById("opts").innerHTML = P.routes.map(function (r, i) {
  const h = Math.floor(r.time_min / 60), m = Math.round(r.time_min % 60);
  return '<div class="opt" data-i="' + i + '"><b>' + esc(r.label) +
    "</b>" + (r === quickest ? ' <span class="best">lowest estimated time shown</span>' : "") +
    '<br><span class="num">' + (h ? h + "h " : "") +
    String(m).padStart(2, "0") + "m &middot; " + r.total_km.toFixed(2) +
    " km &middot; " + r.turns_cross + " crossing turns, " + r.turns_u +
    " U-turns<br>" + r.wrong_way_km.toFixed(2) +
    " km against mapped one-way<br>" + r.service_counterflow_km.toFixed(2) +
    ' km service counterflow <span title="Traffic-flow reference, not an access restriction">(' +
    r.service_counterflow_segments + " segments)</span></span></div>";
}).join("");
document.getElementById("opts").addEventListener("click", function (ev) {
  const el = ev.target.closest(".opt");
  if (el) show(+el.dataset.i);
});
map.fitBounds(layers[0].getBounds());
show(P.routes.indexOf(quickest));
</script>
</body>
</html>
"""


def write_comparison(rows, results, nodes, out_dir: Path):
    """One map, one route per algorithm, switched like a directions app."""
    routes = []
    for row, res in zip(rows, results):
        segs = [{"k": "s" if s["kind"] == "service" else "d",
                 "c": [[round(a, 6), round(b, 6)]
                       for a, b in segment_coords(s, nodes)]}
                for s in res["segments"]]
        routes.append({k: row[k] for k in
                       ("variant", "label", "total_km", "time_min",
                        "turns_cross", "turns_u", "wrong_way_km",
                        "wrong_way_penalty", "wrong_way_source",
                        "service_counterflow_km", "service_counterflow_segments")}
                      | {"segs": segs})
    payload = json.dumps({"routes": routes},
                         ensure_ascii=False).replace("</", "<\\/")
    path = out_dir / "alternatives.html"
    path.write_text(COMPARE_TEMPLATE.replace("__PAYLOAD__", payload),
                    encoding="utf-8")
    # the same numbers without the geometry, for the editor's panel
    (out_dir / "alternatives.json").write_text(json.dumps(
        {"routes": [{k: v for k, v in r.items() if k != "segs"}
                    for r in routes]}, ensure_ascii=False),
        encoding="utf-8")
    return path


def solve_round(variant, nodes, edges, args, profile, profile_name,
                control):
    """One complete solve under one preference. Everything above this is
    shared (the data, the annotation, the endpoints); everything the
    outputs and the comparison need comes back in the result."""
    algorithm = variant["algorithm"]
    profile_name, profile = load_profile(variant["profile"])
    args = argparse.Namespace(**vars(args))
    args.wrong_way_penalty = variant["wrong_way_penalty"]
    F, R, D = build_graphs(edges, profile, args.wrong_way_penalty)
    print(f"  speed profile: {profile_name}"
          + " -- minimising riding time, reporting real metres")
    print(f"  wrong-way penalty x{args.wrong_way_penalty:g} "
          f"({variant['wrong_way_source']}); soft cost only")
    n_service_edges = sum(1 for e in edges if e["service"] > 0)
    if n_service_edges == 0:
        sys.exit("No edges have service 1 or 2 -- nothing to route. "
                 "Edit the service column in edges.csv first.")
    service_len = sum(e["length"] * e["service"] for e in edges)
    print(f"  {len(edges)} edges total | {n_service_edges} service streets "
          f"| mandatory riding: {service_len / 1000:.2f} km")

    pin_start = pin_end = None
    if args.end and args.start:
        # SPEC-2: zero-length virtual required edge end -> start; the
        # Euler circuit is later rotated around it (see traverse).
        pin_end = snap_to_node(args.end, list(F.nodes), nodes)
        pin_start = snap_to_node(args.start, list(F.nodes), nodes)
        print(f"  start snapped to node {pin_start} at "
              f"{nodes[pin_start]}, "
              f"{snap_metres(args.start, pin_start, nodes):.0f} m away")
        print(f"  end snapped to node {pin_end} at {nodes[pin_end]}, "
              f"{snap_metres(args.end, pin_end, nodes):.0f} m away")
        R.add_edge(pin_end, pin_start, key=VIRTUAL_KEY, length=0.0)

    router = make_router(D, nodes, algorithm, args.traffic_side,
                         variant.get("turn_weight", 1.0), control)
    if algorithm == "turn":
        print(f"  routing: turn-aware (line graph, "
              f"{router['L'].number_of_edges()} manoeuvres priced)")
    bridges, bridge_len = ensure_required_connected(
        R, router, pinned_endpoints=pin_start is not None)

    start_node = None
    if args.start and pin_start is None:
        start_node = snap_to_node(args.start, list(R.nodes), nodes)
        print(f"  start snapped to node {start_node} at "
              f"{nodes[start_node]}, "
              f"{snap_metres(args.start, start_node, nodes):.0f} m away")
    if args.end and pin_end is None:
        pin_end = snap_to_node(args.end, list(R.nodes), nodes)
        print(f"  end snapped to node {pin_end} at {nodes[pin_end]}, "
              f"{snap_metres(args.end, pin_end, nodes):.0f} m away")

    n_odd = len(odd_nodes(R))
    print(f"Parity repair: {n_odd} odd-degree nodes to match ...")
    print("Extracting Euler traversal ...")
    tour_opts = {"traffic_side": args.traffic_side,
                 "optimise_tour": not args.no_turn_optimisation,
                 "pair_passes": args.pair_passes,
                 "wrong_way_penalty": args.wrong_way_penalty,
                 "control": control,
                 "search_iterations": 0 if args.no_tour_search
                 else args.search_iterations,
                 "street_first": args.street_first}
    if pin_start is not None:                        # --start + --end
        endpoints, extra = parity_repair(R, router, False)
        segments = traverse(F, R, D, nodes, None, None,
                            pin_start=pin_start, **tour_opts)
        kind = "open path, start & end pinned"
    elif pin_end is not None:                        # --end only
        endpoints, extra = parity_repair(R, router, True, pin_end=pin_end)
        if endpoints:
            segments = traverse(F, R, D, nodes, endpoints, None,
                                **tour_opts)
            kind = "open path, end pinned"
        else:  # no odd nodes: the route is a circuit; close it there
            segments = traverse(F, R, D, nodes, None, pin_end,
                                **tour_opts)
            kind = "closed circuit (starts and ends at --end)"
    else:
        endpoints, extra = parity_repair(R, router, args.open,
                                         pin_start=start_node)
        segments = traverse(F, R, D, nodes, endpoints, start_node,
                            **tour_opts)
        kind = "open path" if endpoints else "closed circuit"

    if args.return_to_start:
        last, first = segments[-1]["v"], segments[0]["u"]
        if last != first:
            try:
                path = route_path(router, last, first)
            except nx.NetworkXNoPath:
                sys.exit("--return-to-start: no path from the end back "
                         "to the start exists in the network.")
            tail = deadhead_segments(D, path)
            segments.extend(tail)
            print(f"  return leg after the end: "
                  f"{sum(s['length'] for s in tail) / 1000:.2f} km "
                  f"back to the start")
            kind += " + return to start"

    try:
        coverage = validate_coverage(edges, segments)
    except ValueError as exc:
        sys.exit(str(exc))
    return {
        "coverage": coverage, "variant": variant, "algorithm": algorithm,
        "segments": segments, "F": F, "kind": kind,
        "profile_name": profile_name, "profile": profile,
        "bridges": bridges, "bridge_len": bridge_len,
        "service_len": service_len, "n_service_edges": n_service_edges,
        "pin_start": pin_start, "edges_total": len(edges),
    }


def history_configuration(args, results, score_profile):
    """Actual resolved experiment, including every candidate in a comparison."""
    from round_history import hash_bytes

    return {
        "start": [float(v) for v in args.start.split(",")] if args.start else None,
        "end": [float(v) for v in args.end.split(",")] if args.end else None,
        "open": args.open, "return_to_start": args.return_to_start,
        "traffic_side": args.traffic_side,
        "pair_passes": args.pair_passes, "street_first": args.street_first,
        "optimise_tour": not args.no_turn_optimisation,
        "search_iterations": 0 if args.no_tour_search else args.search_iterations,
        "variants": [{"name": r["variant"]["name"],
                      "label": r["variant"]["label"],
                      "algorithm": r["algorithm"],
                      "profile_name": r["profile_name"], "profile": r["profile"],
                      "wrong_way_penalty": r["variant"]["wrong_way_penalty"],
                      "wrong_way_source": r["variant"]["wrong_way_source"],
                      "turn_weight": r["variant"].get("turn_weight", 1.0)}
                     for r in results],
        "comparison_scoring_profile": score_profile if len(results) > 1 else None,
        "selection": "minimum_comparison_time" if len(results) > 1 else "single",
        "implementation": {name: hash_bytes((Path(__file__).parent / name).read_bytes())
                           for name in ("solve_route.py", "delivery.py", "split_edge.py")},
        "networkx_version": nx.__version__,
        "python_version": list(sys.version_info[:3]),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 epilog="Required delivery-side coverage is validated. "
                                 "Route optimisation is heuristic; a global optimum is not guaranteed.")
    ap.add_argument("--data", default="data",
                    help="directory with edges.csv + nodes.csv")
    ap.add_argument("--out", default="result", help="output directory")
    ap.add_argument("--open", action="store_true",
                    help="allow start != end (open Euler path); the solver "
                         "chooses endpoints using its matching-cost model; "
                         "no global route optimum is guaranteed")
    ap.add_argument("--start", metavar="LAT,LON",
                    help="pin the start point, snapped to the full "
                         "network (including service-0 streets). Southern-hemisphere "
                         "latitudes are negative, so use the equals "
                         "form: --start=-37.8406,144.9541")
    ap.add_argument("--end", metavar="LAT,LON",
                    help="pin the finish point (e.g. a parcel-handover "
                         "office). With --start: open "
                         "route start -> ... -> end, both snapped to "
                         "the full network (they may lie on service-0 "
                         "streets). Without --start: open route whose "
                         "end is pinned, start chosen by the cost model. "
                         "Mutually exclusive with --open. Equals form "
                         "for southern latitudes: --end=-37.84,144.95")
    ap.add_argument("--profile", default=None, metavar="NAME|FILE",
                    help="speed model for route costs and time estimates. "
                         "'edv' (default) takes the "
                         f"posted limit capped at {EDV_TOP_SPEED:g} km/h "
                         "-- what the vehicle can actually do; 'limits' "
                         "takes the posted limit as it stands. A "
                         "footpath has no posted limit, so both use the "
                         "surface speed there. Or give a JSON file: "
                         '{"speeds": {highway: km/h}, "use_maxspeed": '
                         'true, "cap_kmh": 50}. Reported kilometres are '
                         "always real metres.")
    ap.add_argument("--vehicle", choices=sorted(VEHICLE_WRONG_WAY),
                    default=None,
                    help="counterflow soft-cost preset: tricycle (x3), "
                         "footpath (x1), or carriageway (x12). Overrides "
                         "the saved penalty, but --wrong-way-penalty wins. "
                         "Without either flag use the saved value or x1. "
                         "These presets do not enforce access rules or "
                         "change speeds. Overridden by --variants")
    ap.add_argument("--wrong-way-penalty", type=float, default=None,
                    metavar="FACTOR",
                    help="multiply the cost of riding a one-way street "
                         "against its direction (1.0 = off). This is a "
                         "soft preference; service tasks also prefer the "
                         "normal traffic direction on their selected side "
                         "of two-way streets. Affects connector paths and "
                         "tour choices; service coverage remains "
                         "mandatory. Overrides --vehicle and saved values, "
                         "but is overridden by --variants. Needs real "
                         "oneway data, so extract with --network-type all.")
    ap.add_argument("--variants", metavar="NAMES",
                    help="solve several preferences and lay them side "
                         "by side, the way a directions app offers a "
                         "choice: 'all', or a comma-separated list of "
                         + ", ".join(VARIANT_ORDER) + ". Every option "
                         "covers the same streets; they differ in what "
                         "they treat as expensive. Overrides --profile "
                         "/ --algorithm / --vehicle / --wrong-way-penalty "
                         "and saved preferences, with a notice")
    ap.add_argument("--algorithm", default="node",
                    choices=list(ALGORITHMS),
                    help="how paths are found. 'node' (default) is "
                         "plain Dijkstra, where a turn is free. 'turn' "
                         "routes over the line graph, pricing every "
                         "manoeuvre -- so turning right across traffic "
                         "onto a fast road costs what it costs. "
                         "'all' solves with every algorithm and writes "
                         "them side by side for comparison")
    ap.add_argument("--traffic-side", choices=["left", "right"],
                    default="left",
                    help="which side traffic drives on, so the solver "
                         "knows which turn crosses the oncoming stream "
                         "(default left: AU/UK/JP/NZ)")
    ap.add_argument("--pair-passes", action="store_true",
                    help="while building the tour, prefer taking the "
                         "second pass of a two-sided street straight "
                         "after the first, so its mail stays in one "
                         "bundle. May change distance and turns; implies a turn "
                         "at the far end and Hierholzer splices "
                         "sub-tours between the passes anyway: measured "
                         "+5 points of pairing for +3 min of turning, so "
                         "it is off by default (B3). Delivering both "
                         "kerbs does not depend on this -- that is "
                         "guaranteed either way (E17)")
    ap.add_argument("--street-first", action="store_true",
                    help="prefer finishing a street before moving on, so "
                         "its mail stays in one bundle. May change "
                         "distance; measured on a real round it "
                         "changed street visits 43 -> 44 and crossing "
                         "turns 24 -> 27, so it is off by default")
    ap.add_argument("--no-tour-search", action="store_true",
                    help="skip the heuristic search over Euler tours "
                         "for lower route cost and fewer street re-visits; "
                         "distance and turns may also change")
    ap.add_argument("--search-iterations", type=int, default=4000,
                    metavar="N", help="how hard to search for a better "
                                      "tour (default 4000)")
    ap.add_argument("--no-turn-optimisation", action="store_true",
                    help="take any Euler tour instead of preferring one "
                         "with fewer awkward turns and less wrong-way "
                         "riding. Coverage is still validated; distance "
                         "and turns may change")
    ap.add_argument("--history", nargs="?", const=DEFAULT_HISTORY,
                    metavar="DIR",
                    help="file this solve away as a version (annotation "
                         "+ route + totals) so later runs can be "
                         "compared; bare --history uses "
                         f"{DEFAULT_HISTORY}. Inspect with "
                         "round_history.py list / diff")
    ap.add_argument("--bump", choices=["major", "minor", "patch"],
                    help="force the history version bump instead of "
                         "deriving it from what changed")
    ap.add_argument("--note", default="",
                    help="label for the recorded version, e.g. "
                         '--note "after fixing the sliver gaps"')
    ap.add_argument("--return-to-start", action="store_true",
                    help="after the pinned end, append a least-cost path "
                         "back to the start and include it in the "
                         "route -- a closed tour start -> service -> "
                         "end -> start (e.g. depot -> round -> "
                         "handover office -> depot). Requires --end.")
    args = ap.parse_args()
    if args.end and args.open:
        ap.error("--end is mutually exclusive with --open "
                 "(an --end route is already open)")
    if args.return_to_start and not args.end:
        ap.error("--return-to-start requires --end")

    data_dir, out_dir = Path(args.data), Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Settings picked in the editor (data/endpoints.json) fill in for
    # any flag that was not given, so the browser flow needs no flags.
    ep_file = data_dir / "endpoints.json"
    ep = {}
    if ep_file.exists():
        try:
            ep = json.loads(ep_file.read_text(encoding="utf-8"))
        except ValueError:
            ep = {}
    if not args.start and not args.end:
        if ep.get("start"):
            args.start = "{},{}".format(*ep["start"])
        if ep.get("end"):
            args.end = "{},{}".format(*ep["end"])
            args.return_to_start = (args.return_to_start or
                                    bool(ep.get("return_to_start")))
        if args.start or args.end:
            print(f"  using endpoints from {ep_file} "
                  f"(set in the editor)")
    if args.profile is None:
        args.profile = ep.get("profile") or DEFAULT_PROFILE
    if args.variants:
        names = (VARIANT_ORDER if args.variants == "all"
                 else [n.strip() for n in args.variants.split(",")])
        unknown = [n for n in names if n not in VARIANTS]
        if unknown:
            ap.error(f"unknown preference(s): {', '.join(unknown)} "
                     f"(have: {', '.join(VARIANT_ORDER)})")
        chosen_variants = [dict(VARIANTS[n], name=n,
                                wrong_way_source=f"--variants {n}")
                           for n in names]
        print("  note: --variants uses preset profiles, algorithms and "
              "wrong-way penalties; per-run preferences do not override "
              "presets")
        overridden = []
        if args.vehicle is not None:
            overridden.append("--vehicle")
        if args.wrong_way_penalty is not None:
            overridden.append("--wrong-way-penalty")
        if ep.get("wrong_way_penalty") not in (None, ""):
            overridden.append("endpoints.json wrong_way_penalty")
        if overridden:
            print("  overridden: " + ", ".join(overridden))
    else:
        # no preference asked for: honour the individual flags as given
        try:
            args.wrong_way_penalty, penalty_source = resolve_wrong_way_penalty(
                args.wrong_way_penalty, args.vehicle, ep.get("wrong_way_penalty"))
        except ValueError as exc:
            ap.error(str(exc))
        profile_name, _ = load_profile(args.profile)
        chosen_variants = [{"name": args.algorithm, "label": profile_name,
                            "algorithm": args.algorithm,
                            "profile": args.profile,
                            "wrong_way_penalty": args.wrong_way_penalty,
                            "wrong_way_source": penalty_source,
                            "turn_weight": 1.0}]
    print("Loading network ...")
    inputs = None
    if args.history:
        import round_history
        inputs = round_history.capture_inputs(data_dir)
    nodes, edges, control = load_data(data_dir, inputs)
    profile_name, profile = load_profile(chosen_variants[0]["profile"])
    _, score_profile = load_profile(SCORE_PROFILE)
    results = [solve_round(v, nodes, edges, args, profile, profile_name,
                           control)
               for v in chosen_variants]
    rows = [summarise_result(r, nodes, args,
                             score_profile if len(results) > 1
                             else r["profile"], control) for r in results]
    configuration = history_configuration(args, results, score_profile) if args.history else None
    if len(results) > 1:
        print_comparison(rows)
        for row, res in zip(rows, results):
            sub = out_dir / row["variant"]
            sub.mkdir(parents=True, exist_ok=True)
            write_route_csv(res["segments"], res["F"], nodes, sub)
            write_map(res["segments"], nodes, sub, row["time_min"])
        alt = write_comparison(rows, results, nodes, out_dir)
        print(f"Each preference      : {out_dir}/<preference>/")
        print(f"Side by side         : {alt}")
        # Top-level outputs use the lowest estimated time among these candidates.
        best = min(range(len(rows)), key=lambda i: rows[i]["time_min"])
        results, rows = [results[best]], [rows[best]]
        print(f"Top-level outputs    : {rows[0]['label']}\n")
    chosen = results[0]
    segments = chosen["segments"]
    F, kind = chosen["F"], chosen["kind"]
    bridges, bridge_len = chosen["bridges"], chosen["bridge_len"]
    service_len = chosen["service_len"]
    n_service_edges = chosen["n_service_edges"]
    pin_start = chosen["pin_start"]
    profile, profile_name = chosen["profile"], chosen["profile_name"]

    turns = annotate_turns(segments, nodes, args.traffic_side, profile,
                           control)
    ops = operational_metrics(segments)
    riding_s, turning_s, waiting_s = estimate_seconds(segments, profile)
    directions = direction_metrics(segments)

    total = sum(s["length"] for s in segments)
    deadhead = total - service_len
    route_csv = write_route_csv(segments, F, nodes, out_dir)
    route_map = write_map(segments, nodes, out_dir,
                          (riding_s + turning_s + waiting_s) / 60)

    if args.history:
        import round_history
        version, is_new = round_history.record(
            Path(args.history), data_dir, out_dir=out_dir,
            note=args.note, bump=args.bump,
            inputs=inputs, configuration=configuration,
            summary={
                "mode": kind,
                "service_edges": sum(1 for e in edges if e["service"] > 0),
                "mandatory_km": round(service_len / 1000, 3),
                "total_km": round(total / 1000, 3),
                "deadhead_km": round(deadhead / 1000, 3),
                "islands": (bridges + 1 - (1 if pin_start is not None
                                           else 0)) if bridges else 1,
                "bridges": bridges,
                "bridge_km": round(bridge_len / 1000, 3),
                "segments_service": sum(1 for s in segments
                                        if s["kind"] == "service"),
                "delivery_sides_covered": chosen["coverage"],
                "segments_deadhead": sum(1 for s in segments
                                         if s["kind"] == "deadhead"),
                "start": args.start or "",
                "end": args.end or "",
                "return_to_start": bool(args.return_to_start),
                "edges_total": len(edges),
                "profile": profile_name,
                "variant": chosen["variant"]["name"],
                "wrong_way_penalty": chosen["variant"]["wrong_way_penalty"],
                "wrong_way_source": chosen["variant"]["wrong_way_source"],
                **directions,
                "time_min": round((riding_s + turning_s + waiting_s) / 60, 1),
                "waiting_min": round(waiting_s / 60, 1),
                "paired_pct": ops["paired_pct"],
                "street_runs": ops["street_runs"],
                "runs_per_street": ops["runs_per_street"],
                "turns_cross": turns["cross"],
                "turns_u": turns["u"],
                "turn_optimised": not args.no_turn_optimisation,
            })
        print("  history: " + (f"recorded version {version}" if is_new
                               else f"matches existing version {version}")
              + f" (in {args.history})")

    print(f"""
================ RESULT ({kind}) ================
Route quality            : heuristic; global optimum not guaranteed
Mandatory service riding : {service_len / 1000:7.2f} km   (theoretical lower bound)
Route total              : {total / 1000:7.2f} km
Extra / deadhead         : {deadhead / 1000:7.2f} km   ({100 * deadhead / max(total, 1):.1f}% of route)
Segments                 : {sum(1 for s in segments if s['kind'] == 'service')} service + \
{sum(1 for s in segments if s['kind'] == 'deadhead')} deadhead
Riding time (rough)      : {hhmm(riding_s + turning_s + waiting_s):>9}   \
({hhmm(riding_s)} moving + {hhmm(turning_s)} turning + \
{hhmm(waiting_s)} waiting at crossings; excludes delivery stops)
Turns                    : {turns['cross']} crossing + {turns['u']} U-turn + \
{turns['easy']} easy + {turns['straight']} straight
Against mapped one-way   : {directions['wrong_way_km']:7.2f} km   (all riding; direction reference only)
Service counterflow      : {directions['service_counterflow_km']:7.2f} km   ({directions['service_counterflow_segments']} service segments; overlaps one-way metric)
Both kerbs delivered     : {ops['both_sides']}/{ops['two_sided']}   \
(independent left/right service tasks -- verified)
Both sides back to back  : {ops['paired_pct']:6.1f}%   \
(when paired, one bundle each in the sort frame)
Street runs              : {ops['street_runs']:7d}   \
({ops['streets']} streets, so {ops['runs_per_street']} visits each)
Outputs                  : {route_csv}
                           {route_map}
=================================================
The 'extra' number is the only part optimisation can influence --
the mandatory part is your job itself.""")


if __name__ == "__main__":
    main()
