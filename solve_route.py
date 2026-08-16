#!/usr/bin/env python3
"""Solve a postal delivery round as a Rural Postman Problem.

Input:  a data directory containing edges.csv and nodes.csv produced by
        extract_network.py, AFTER you have edited the `service` column:

            2 = I deliver both sides  -> street must be traversed twice
            1 = I deliver one side only (or I zigzag both sides in a
                single pass)          -> street traversed once
            0 = not my street, but I may ride along it (connector)
            x = never use this edge   -> removed from the network
                entirely (not even as a deadhead shortcut)

Output: route.csv       ordered traversal list -- the basis for the new
                        sort sequence
        route_map.html  interactive route viewer: step through the walk
                        with a slider/play button, current segment
                        highlighted with a direction arrow
        console summary service km vs total km vs extra (deadhead) km

Algorithm (4 phases -- exact optimum, not a heuristic, whenever the
service streets form one connected piece):

  Phase 1  Build the full network F (all edges, including service=0)
           and the required multigraph R (each edge repeated `service`
           times).  F is the world you can ride through; R is the work
           you must do.

  Phase 2  Parity repair.  A single continuous route can only exist if
           every node of R has even degree (Euler, 1736).  Nodes with
           odd degree are exactly where a route is forced to break.
           Compute shortest paths between all odd nodes over F, then a
           minimum-weight perfect matching (blossom algorithm, via
           networkx) picks the cheapest set of extra connections.
           These connections are the ONLY extra distance in the final
           route -- everything else is mandatory work.

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
remains is the jointly optimal open route start -> ... -> end.  The
endpoints are snapped to the full network F, so they may lie on
non-service streets (e.g. a depot or a parcel-handover office reached
via service-0 corridor streets); if they sit off the service
component, the auto-bridge connects them and the near-optimal warning
applies as usual.
"""

import argparse
import csv
import itertools
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import networkx as nx

from round_history import DEFAULT_HISTORY

try:
    from shapely import wkt as shapely_wkt
except ImportError:  # geometry is optional; straight lines still work
    shapely_wkt = None


# ----------------------------------------------------------------------
# Input
# ----------------------------------------------------------------------

def load_data(data_dir: Path):
    """Read nodes.csv and edges.csv. Node ids are kept as strings."""
    nodes = {}
    with open(data_dir / "nodes.csv", newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            nodes[row["node_id"]] = (float(row["lat"]), float(row["lon"]))

    edges, blank_service, excluded = [], 0, 0
    with open(data_dir / "edges.csv", newline="", encoding="utf-8-sig") as f:
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
                "wkt": row.get("geometry_wkt") or "",
            })
    if blank_service:
        print(f"  note: {blank_service} edges had a blank service value "
              f"-> treated as 0 (connector only)")
    if excluded:
        print(f"  note: {excluded} edges excluded (service=x) -> the "
              f"route will never use them, not even as shortcuts")
    return nodes, edges


# ----------------------------------------------------------------------
# Cost profiles -- what "shortest" means
# ----------------------------------------------------------------------
#
# `length` is always real metres: it is what the mandatory distance,
# route.csv and every printed kilometre are made of.  `cost` is what
# the optimiser minimises.  With the default profile the two are the
# same and the route is the shortest one.  With a speed profile a
# metre of footpath costs more than a metre of road, so the optimiser
# prefers roads for deadhead -- the result is then the cheapest route
# under that profile, which may be slightly longer in metres.

# Surfaces you ride *on* rather than drive along: a footway has no
# posted limit (0% of them carry one in OSM), and what you do there is
# set by the vehicle and the pedestrians, not by the law. Everywhere
# else the posted limit is the honest number.
FOOT_SURFACES = {"footway", "path", "pedestrian", "steps", "corridor",
                 "track", "cycleway"}

EDV_SURFACE = {"footway": 10, "path": 10, "pedestrian": 10,
               "cycleway": 15, "track": 10, "corridor": 6, "steps": 2}

PROFILES = {
    "distance": None,                    # every metre equal
    "edv": {                             # one speed per road type
        "speeds": {**EDV_SURFACE,
                   "service": 15, "living_street": 15,
                   "residential": 18, "unclassified": 18,
                   "tertiary": 20, "tertiary_link": 20,
                   "secondary": 20, "secondary_link": 20,
                   "primary": 20, "primary_link": 20,
                   "trunk": 20, "trunk_link": 20},
        "use_maxspeed": False},
    "limits": {                          # footpath fixed, road = posted
        "speeds": {**EDV_SURFACE,
                   # only used where OSM has no maxspeed
                   "service": 25, "living_street": 20,
                   "residential": 50, "unclassified": 50,
                   "tertiary": 50, "tertiary_link": 50,
                   "secondary": 60, "secondary_link": 60,
                   "primary": 60, "primary_link": 60,
                   "trunk": 60, "trunk_link": 60},
        "use_maxspeed": True},
}
DEFAULT_SPEED = 15.0
REPORT_PROFILE = "limits"    # what the printed time is measured with


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
    """What the optimiser minimises: metres under `distance`, seconds
    under any speed profile."""
    if profile is None:
        return length
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
        F.add_edge(e["u"], e["v"], key=e["edge_id"],
                   length=e["length"], name=e["name"],
                   service=e["service"], wkt=e["wkt"],
                   highway=e["highway"], oneway=e["oneway"],
                   maxspeed=e["maxspeed"], cu=e["u"], cv=e["v"])
        common = dict(length=e["length"], name=e["name"], wkt=e["wkt"],
                      highway=e["highway"], oneway=e["oneway"],
                      maxspeed=e["maxspeed"])
        D.add_edge(e["u"], e["v"], key=e["edge_id"], cost=cost,
                   against=False, **common)
        D.add_edge(e["v"], e["u"], key=e["edge_id"],
                   cost=cost * wrong_way if e["oneway"] else cost,
                   against=e["oneway"], **common)
        for p in range(1, e["service"] + 1):
            R.add_edge(e["u"], e["v"], key=f'{e["edge_id"]}|p{p}',
                       base=e["edge_id"], length=e["length"])
    return F, R, D


def cheapest_arc(D, x, y):
    """(key, data) of the arc actually taken between adjacent nodes."""
    return min(D[x][y].items(), key=lambda kv: kv[1]["cost"])


def path_length_m(D, path):
    """Real metres along a node path (whose cost may be weighted)."""
    return sum(cheapest_arc(D, x, y)[1]["length"]
               for x, y in zip(path, path[1:]))


def both_ways(D, a, b):
    """Cheapest node path a->b and b->a. Connectivity is symmetric --
    every edge contributes both arcs -- so both always exist."""
    return {a: nx.shortest_path(D, a, b, weight="cost"),
            b: nx.shortest_path(D, b, a, weight="cost")}


def ensure_required_connected(R, D, pinned_endpoints=False):
    """If the required work falls into several islands, bridge them with
    the cheapest paths through the full network. Prints a warning, since
    the result is then near-optimal rather than provably optimal.

    Growing from one component and always attaching the nearest other
    one is Prim's algorithm, so the bridges form a minimum spanning
    tree over component-to-component shortest paths -- optimal among
    tree-shaped connections, though not necessarily the RPP optimum
    (a Steiner-style branch off a bridge, or connectivity gained for
    free from the parity matching, can beat it)."""
    comps = list(nx.connected_components(R))
    bridges = 0
    bridge_len = 0.0
    while len(comps) > 1:
        base = set(comps[0])
        others = set().union(*comps[1:])
        best = None
        # cheapest link either way out of the growing component
        for graph, reverse in ((D, False), (D.reverse(copy=False), True)):
            dist, paths = nx.multi_source_dijkstra(graph, sources=base,
                                                   weight="cost")
            for n, c in dist.items():
                if n in others and (best is None or c < best[0]):
                    path = paths[n]
                    best = (c, list(reversed(path)) if reverse else path)
        if best is None:
            sys.exit("Service streets are disconnected and no rideable "
                     "edges link them. Extract a larger area, or check "
                     "that a linking street was not excluded (service=x).")
        _, path = best
        a, b = path[0], path[-1]
        R.add_edge(a, b, key=f"bridge|{bridges}", paths=both_ways(D, a, b))
        bridges += 1
        bridge_len += path_length_m(D, path)
        comps = list(nx.connected_components(R))
    if bridges:
        islands = bridges + 1 - (1 if pinned_endpoints else 0)
        extra = " + the pinned start/end" if pinned_endpoints else ""
        print(f"  warning: required work formed {bridges + 1} components "
              f"({islands} service island(s){extra}); linked by "
              f"{bridges} bridge(s) totalling {bridge_len / 1000:.2f} km."
              f"\n           Bridging is a minimum spanning tree over the "
              f"components, so the route is near-optimal: only that "
              f"{bridge_len / 1000:.2f} km is not provably minimal."
              f"\n           Separate delivery areas are fine -- that "
              f"bridge is riding you would do anyway. Only fix islands "
              f"caused by annotation gaps (a short service=0 sliver "
              f"inside a street you do deliver); never mark a street "
              f"you do not deliver just to merge islands.")
    return bridges, bridge_len


# ----------------------------------------------------------------------
# Phase 2 -- parity repair
# ----------------------------------------------------------------------

def odd_nodes(R):
    return sorted(n for n in R.nodes if R.degree(n) % 2 == 1)


def pairwise_shortest(D, targets):
    """Cheapest cost and node path for every ORDERED pair of targets,
    over the full network (service-0 edges are fair game). Costs are
    asymmetric once a wrong-way penalty is in play, so both directions
    are kept."""
    dist, path = {}, {}
    tset = set(targets)
    for s in targets:
        d, p = nx.single_source_dijkstra(D, s, weight="cost")
        for t in tset:
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
    direction -- exact without a wrong-way penalty, a lower bound with
    one (see NF2 / B10 in docs/DEVELOPMENT.md)."""
    out = {}
    for (a, b), cost in dist.items():
        out[(a, b)] = out[(b, a)] = min(cost, dist[(b, a)])
    return out


def min_matching(nodes_subset, dist):
    """Minimum-weight perfect matching on a complete graph over the odd
    nodes. This is the step that guarantees optimality."""
    if not nodes_subset:
        return [], 0.0
    K = nx.Graph()
    for a, b in itertools.combinations(nodes_subset, 2):
        K.add_edge(a, b, weight=dist[(a, b)])
    M = nx.min_weight_matching(K)
    cost = sum(dist[(a, b)] for a, b in M)
    return list(M), cost


def parity_repair(R, D, open_route, start_node):
    """Add matching connectors to R. Returns (endpoints, extra_cost).
    endpoints is (s, t) for an open route, else None."""
    odd = odd_nodes(R)
    if not odd:
        return None, 0.0

    dist, path = pairwise_shortest(D, odd)
    sdist = symmetrise(dist)

    def add_connectors(pairs):
        for i, (a, b) in enumerate(pairs):
            R.add_edge(a, b, key=f"match|{i}",
                       paths={a: path[(a, b)], b: path[(b, a)]})

    if not open_route:
        pairs, cost = min_matching(odd, sdist)
        add_connectors(pairs)
        return None, cost

    # Open route: leave the best pair of odd nodes unmatched -- they
    # become the start and end. If a pin was given, fix one endpoint
    # to the odd node nearest to it (network distance over F).
    if len(odd) == 2:
        return tuple(odd), 0.0
    candidates = []
    if start_node is not None:
        if start_node in odd:
            s_fixed = start_node
        else:
            d, _ = nx.single_source_dijkstra(D, start_node, weight="cost")
            s_fixed = min((n for n in odd if n in d),
                          key=lambda n: d[n], default=odd[0])
        pairs_to_try = [(s_fixed, t) for t in odd if t != s_fixed]
    else:
        pairs_to_try = list(itertools.combinations(odd, 2))
    for s, t in pairs_to_try:
        rest = [n for n in odd if n not in (s, t)]
        pairs, cost = min_matching(rest, sdist)
        candidates.append((cost, (s, t), pairs))
    cost, endpoints, pairs = min(candidates, key=lambda c: c[0])
    add_connectors(pairs)
    return endpoints, cost


# ----------------------------------------------------------------------
# Phase 3 + 4 -- Euler traversal and expansion
# ----------------------------------------------------------------------

VIRTUAL_KEY = "virtual|endpin"


def deadhead_segments(D, node_path):
    """Expand a node path into deadhead segments, taking the cheapest
    arc between each pair of adjacent nodes."""
    segs = []
    for x, y in zip(node_path, node_path[1:]):
        key, data = cheapest_arc(D, x, y)
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
# Every Euler tour over the same augmented graph covers exactly the
# same edges, so they all have exactly the same length.  Which one we
# take is therefore free: we can spend it on the things distance does
# not capture -- awkward turns and riding one-way streets backwards.
# Hierholzer's algorithm stays correct whichever unused edge is picked
# next (unlike Fleury, which needs bridge avoidance), so a greedy
# preference costs nothing and risks nothing.
#
# One caveat on "same length": a connector's two directions are two
# separately computed shortest paths, and a wrong-way penalty can make
# them differ in metres. Without a penalty they are the same path
# reversed and the tour choice is exactly free; with one, the total can
# shift by a few metres.
#
# The wrong-way weight is tied to --wrong-way-penalty rather than being
# a constant: preferring straight continuations pulls the tour INTO the
# long one-way chains of a divided arterial, so unless the rider has
# actually asked to avoid wrong-way riding, the tour should not trade
# turns for it (measured: a weak weight made wrong-way worse, not
# better).

TURN_SECONDS = {"straight": 0.0, "easy": 4.0, "cross": 12.0, "u": 25.0}
STRAIGHT_DEG = 30      # within this, it is not a turn
U_TURN_DEG = 150       # beyond this, it is a U-turn
WRONG_WAY_TOUR_WEIGHT = 0.5   # seconds per metre, per unit of penalty


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


def bearing_of(a, b):
    return None if a == b else (
        math.degrees(math.atan2(
            (b[1] - a[1]) * math.cos(math.radians((a[0] + b[0]) / 2)),
            b[0] - a[0])) % 360)


def tour_meta(R, F, D, nodes):
    """Per required-edge geometry facts the tour chooser needs: the
    bearing leaving u, the bearing arriving at v, and how many metres
    of that direction run against a one-way."""
    meta = {}
    blank = {"out": None, "in": None, "against_m": 0.0}
    for u, v, key, data in R.edges(keys=True, data=True):
        if "base" not in data and "paths" not in data:
            meta[key] = {u: blank, v: blank}   # the virtual end->start
            continue
        if "paths" in data:
            chains = {d: [nodes[n] for n in p]
                      for d, p in data["paths"].items()}
            against = {}
            for d, path in data["paths"].items():
                against[d] = sum(
                    a[1]["length"] for a in
                    (cheapest_arc(D, x, y) for x, y in zip(path, path[1:]))
                    if a[1]["against"])
        else:
            edata = F[u][v][data["base"]]
            chain = segment_coords({"wkt": edata["wkt"], "u": u, "v": v},
                                   nodes)
            chains = {u: chain, v: list(reversed(chain))}
            wrong = edata["length"] if edata["oneway"] else 0.0
            against = {u: 0.0 if not edata["oneway"] or u == edata["cu"]
                       else wrong,
                       v: 0.0 if not edata["oneway"] or v == edata["cu"]
                       else wrong}
        info = {}
        for start, chain in chains.items():
            info[start] = {
                "out": bearing_of(chain[0], chain[1]) if len(chain) > 1
                else None,
                "in": bearing_of(chain[-2], chain[-1]) if len(chain) > 1
                else None,
                "against_m": against.get(start, 0.0),
            }
        meta[key] = info
    return meta


def euler_tour(R, source, meta, traffic_side="left", optimise=True,
               wrong_way_penalty=1.0, first_key=None):
    """Hierholzer, picking the next edge by turn comfort and by not
    riding one-way streets backwards. Returns [(u, v, key), ...].

    The choice never changes the tour's length -- see the note above --
    so this is pure gain; with optimise=False the first available edge
    is taken, which is what an unguided Euler tour does."""
    adj = defaultdict(list)
    for u, v, key in R.edges(keys=True):
        adj[u].append((v, key))
        if u != v:
            adj[v].append((u, key))
    used = set()
    wrong_weight = max(0.0, wrong_way_penalty - 1.0) * WRONG_WAY_TOUR_WEIGHT

    def run_against(node, key):
        """Metres against a one-way committed to by taking `key` from
        `node`: most nodes have degree 2 and offer no further choice,
        so entering a chain the wrong way commits to all of it. Judging
        only the first edge is what made the tour prefer to slide into
        long backwards runs on a divided arterial."""
        total, seen = 0.0, set()
        while key is not None and key not in seen:
            seen.add(key)
            total += meta.get(key, {}).get(node, {}).get("against_m", 0.0)
            nxt = next((n for n, k in adj[node] if k == key), None)
            if nxt is None or len(adj[nxt]) != 2:
                break
            node, key = nxt, next((k for _, k in adj[nxt] if k != key),
                                  None)
        return total

    def choose(node, bearing_in):
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
            score = (TURN_SECONDS[turn_kind(bearing_in, info.get("out"),
                                            traffic_side)]
                     + wrong_weight * (run_against(node, key)
                                       if wrong_weight else 0.0))
            if best is None or score < best[0]:
                best = (score, i, nbr, key)
        return None if best is None else best[1:]

    stack = [(source, None, None)]        # node, key used to get here
    out = []
    while stack:
        node, in_key, bearing_in = stack[-1]
        picked = choose(node, bearing_in)
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


def traverse(F, R, D, nodes, endpoints, start_node, pin_start=None,
             traffic_side="left", optimise_tour=True,
             wrong_way_penalty=1.0):
    """Walk the Euler circuit/path and expand connectors into real
    street segments. Returns a list of segment dicts.

    pin_start: set when a virtual end->start edge is in R (SPEC-2).
    The circuit is rotated so the virtual edge would be last, the edge
    is dropped, and the walk is oriented to begin at pin_start -- what
    remains is the open route start -> ... -> end."""
    meta = tour_meta(R, F, D, nodes)
    if endpoints:
        source = endpoints[0]
        assert nx.has_eulerian_path(R, source=source)
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
                       wrong_way_penalty, first)

    if pin_start is not None:
        assert euler[0][2] == VIRTUAL_KEY, "virtual edge must be first"
        euler = euler[1:]
        assert not euler or euler[0][0] == pin_start

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
            segments.append(dict(kind="service", edge_id=base,
                                 u=u, v=v, name=edata["name"],
                                 length=edata["length"], wkt=edata["wkt"],
                                 pass_label=f"{passes_seen[base]}/{total}",
                                 highway=edata["highway"],
                                 maxspeed=edata["maxspeed"],
                                 against=bool(edata["oneway"])
                                 and u != edata["cu"]))
    return segments


def annotate_turns(segments, nodes, traffic_side="left"):
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
        if i:
            counts[s["turn"]] += 1
        previous = into if into is not None else previous
    return counts


def estimate_seconds(segments, profile):
    """Rough riding time: metres at the profile's speeds, plus a fixed
    cost per turn. Excludes every stop at a letterbox, which on a real
    round dominates -- useful for comparing routes, not for planning a
    day."""
    table = profile or PROFILES[REPORT_PROFILE]
    riding = sum(s["length"] /
                 (speed_kmh(s.get("highway", ""), s.get("maxspeed"),
                            table) / 3.6)
                 for s in segments)
    turning = sum(TURN_SECONDS.get(s.get("turn", "straight"), 0.0)
                  for s in segments)
    return riding, turning


def hhmm(seconds):
    m = int(round(seconds / 60))
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


# ----------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------

WINDS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


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
    """[(lat, lon), ...] for drawing, honouring curved geometry."""
    if seg["wkt"] and shapely_wkt is not None:
        try:
            line = shapely_wkt.loads(seg["wkt"])
            pts = [(y, x) for x, y in line.coords]
            # orient to travel direction
            if nodes[seg["u"]] and _closer(pts[-1], nodes[seg["u"]], pts[0]):
                pts = list(reversed(pts))
            return pts
        except Exception:
            pass
    return [nodes[seg["u"]], nodes[seg["v"]]]


def _closer(p, target, other):
    d1 = (p[0] - target[0]) ** 2 + (p[1] - target[1]) ** 2
    d2 = (other[0] - target[0]) ** 2 + (other[1] - target[1]) ** 2
    return d1 < d2


def write_route_csv(segments, F, nodes, out_dir: Path):
    path = out_dir / "route.csv"
    cum = 0.0
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["seq", "type", "street", "pass", "direction",
                    "from_cross", "to_cross", "length_m", "cum_km",
                    "against_oneway", "edge_id"])
        for i, s in enumerate(segments, 1):
            cum += s["length"]
            w.writerow([
                i, s["kind"], s["name"], s["pass_label"],
                bearing_label(nodes[s["u"]], nodes[s["v"]]),
                cross_streets(F, s["u"], s["name"]),
                cross_streets(F, s["v"], s["name"]),
                round(s["length"], 1), round(cum / 1000, 3),
                "yes" if s.get("against") else "", s["edge_id"],
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
    border-radius: 6px; padding: 10px 12px; width: 340px;
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
  if (s.w) {  // travelled against a one-way: ride the footpath here
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
    (s.k === "s" ? "pass " + esc(s.p) : "deadhead") +
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

const wrongKm = S.reduce((a, s) => a + (s.w ? s.l : 0), 0) / 1000;
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
  (wrongKm > 0 ? '<br><span style="color:#7b341e">' + wrongKm.toFixed(2) +
   " km against a one-way (use the footpath)</span>" : "");

function arrowAngle(c) {
  const a = c[c.length - 2], b = c[c.length - 1];
  const dy = b[0] - a[0];
  const dx = (b[1] - a[1]) *
    Math.cos((a[0] + b[0]) / 2 * Math.PI / 180);
  return Math.atan2(dx, dy) * 180 / Math.PI;
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
    " &middot; " + s.l + " m &middot; heading " + s.dir +
    " &middot; cum " + s.cum.toFixed(3) + " km" +
    (s.t === "cross" ? " &middot; <b>turn across traffic</b>"
     : s.t === "u" ? " &middot; <b>U-turn</b>" : "") +
    (s.w ? '<br><span style="color:#7b341e"><b>against the one-way</b>'
         + " &mdash; ride the footpath here</span>" : "");
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
            "l": round(s["length"], 1),
            "cum": round(cum / 1000, 3),
            "dir": bearing_label(nodes[s["u"]], nodes[s["v"]]),
            "w": 1 if s.get("against") else 0,
            "t": s.get("turn", "straight"),
            "c": [[round(lat, 6), round(lon, 6)] for lat, lon in coords],
        })
    service = sum(s["length"] for s in segments if s["kind"] == "service")
    payload = json.dumps(
        {"segs": segs,
         "service_km": round(service / 1000, 2),
         "total_km": round(cum / 1000, 2),
         "time_min": round(time_min, 1)},
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


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", default="data",
                    help="directory with edges.csv + nodes.csv")
    ap.add_argument("--out", default="result", help="output directory")
    ap.add_argument("--open", action="store_true",
                    help="allow start != end (open Euler path); the solver "
                         "picks the endpoint pair that minimises total "
                         "distance")
    ap.add_argument("--start", metavar="LAT,LON",
                    help="preferred start point, snapped to the nearest "
                         "service-street node. Southern-hemisphere "
                         "latitudes are negative, so use the equals "
                         "form: --start=-37.8406,144.9541")
    ap.add_argument("--end", metavar="LAT,LON",
                    help="pin the finish point (e.g. a parcel-handover "
                         "office). With --start: jointly optimal open "
                         "route start -> ... -> end, both snapped to "
                         "the full network (they may lie on service-0 "
                         "streets). Without --start: open route whose "
                         "end is pinned, start chosen optimally. "
                         "Mutually exclusive with --open. Equals form "
                         "for southern latitudes: --end=-37.84,144.95")
    ap.add_argument("--profile", default=None, metavar="NAME|FILE",
                    help="what 'shortest' means. 'distance' (default) "
                         "weighs every metre equally. 'edv' weighs by "
                         "how fast a delivery vehicle covers each road "
                         "type, so deadhead prefers carriageways to "
                         "footpaths. Or give a JSON file of "
                         "{highway: km/h}. Reported kilometres are "
                         "always real metres either way.")
    ap.add_argument("--wrong-way-penalty", type=float, default=None,
                    metavar="FACTOR",
                    help="multiply the cost of riding a one-way street "
                         "against its direction (1.0 = off, the "
                         "default; try 3). Only steers deadhead: "
                         "service passes are mandatory whichever way "
                         "they are ridden. Needs real oneway data, so "
                         "extract with --network-type all. Riding the "
                         "footpath is direction-free, so leave this off "
                         "unless you ride on the carriageway.")
    ap.add_argument("--traffic-side", choices=["left", "right"],
                    default="left",
                    help="which side traffic drives on, so the solver "
                         "knows which turn crosses the oncoming stream "
                         "(default left: AU/UK/JP/NZ)")
    ap.add_argument("--no-turn-optimisation", action="store_true",
                    help="take any Euler tour instead of preferring one "
                         "with fewer awkward turns and less wrong-way "
                         "riding. All Euler tours are the same length, "
                         "so this only makes the route less pleasant")
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
                    help="after the pinned end, ride the shortest path "
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
        args.profile = ep.get("profile") or "distance"
    if args.wrong_way_penalty is None:
        try:
            args.wrong_way_penalty = float(ep.get("wrong_way_penalty") or 1)
        except (TypeError, ValueError):
            args.wrong_way_penalty = 1.0

    if args.wrong_way_penalty < 1.0:
        ap.error("--wrong-way-penalty must be >= 1.0")

    print("Loading network ...")
    nodes, edges = load_data(data_dir)
    profile_name, profile = load_profile(args.profile)
    F, R, D = build_graphs(edges, profile, args.wrong_way_penalty)
    if profile is not None or args.wrong_way_penalty > 1.0:
        print(f"  cost profile: {profile_name}"
              + (f", wrong-way penalty x{args.wrong_way_penalty:g}"
                 if args.wrong_way_penalty > 1.0 else "")
              + " -- optimising weighted cost, reporting real metres")
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
        print(f"  start snapped to node {pin_start} at {nodes[pin_start]}")
        print(f"  end snapped to node {pin_end} at {nodes[pin_end]}")
        R.add_edge(pin_end, pin_start, key=VIRTUAL_KEY, length=0.0)

    bridges, bridge_len = ensure_required_connected(
        R, D, pinned_endpoints=pin_start is not None)

    start_node = None
    if args.start and pin_start is None:
        start_node = snap_to_node(args.start, list(R.nodes), nodes)
        print(f"  start snapped to node {start_node} "
              f"at {nodes[start_node]}")
    if args.end and pin_end is None:
        pin_end = snap_to_node(args.end, list(R.nodes), nodes)
        print(f"  end snapped to node {pin_end} at {nodes[pin_end]}")

    n_odd = len(odd_nodes(R))
    print(f"Parity repair: {n_odd} odd-degree nodes to match ...")
    print("Extracting Euler traversal ...")
    tour_opts = {"traffic_side": args.traffic_side,
                 "optimise_tour": not args.no_turn_optimisation,
                 "wrong_way_penalty": args.wrong_way_penalty}
    if pin_start is not None:                        # --start + --end
        endpoints, extra = parity_repair(R, D, False, None)
        segments = traverse(F, R, D, nodes, None, None,
                            pin_start=pin_start, **tour_opts)
        kind = "open path, start & end pinned"
    elif pin_end is not None:                        # --end only
        endpoints, extra = parity_repair(R, D, True, pin_end)
        if endpoints:
            # orient the walk so it ENDS at the pin (or, if parity
            # forces both endpoints, at the forced endpoint nearest it)
            e_pin = snap_to_node(args.end, list(endpoints), nodes)
            endpoints = tuple(n for n in endpoints if n != e_pin) \
                + (e_pin,)
            segments = traverse(F, R, D, nodes, endpoints, None,
                                **tour_opts)
            kind = "open path, end pinned"
        else:  # no odd nodes: the route is a circuit; close it there
            segments = traverse(F, R, D, nodes, None, pin_end,
                                **tour_opts)
            kind = "closed circuit (starts and ends at --end)"
    else:
        endpoints, extra = parity_repair(R, D, args.open, start_node)
        segments = traverse(F, R, D, nodes, endpoints, start_node,
                            **tour_opts)
        kind = "open path" if endpoints else "closed circuit"

    if args.return_to_start:
        last, first = segments[-1]["v"], segments[0]["u"]
        if last != first:
            try:
                path = nx.shortest_path(D, last, first, weight="cost")
            except nx.NetworkXNoPath:
                sys.exit("--return-to-start: no path from the end back "
                         "to the start exists in the network.")
            tail = deadhead_segments(D, path)
            segments.extend(tail)
            print(f"  return leg after the end: "
                  f"{sum(s['length'] for s in tail) / 1000:.2f} km "
                  f"back to the start")
            kind += " + return to start"

    turns = annotate_turns(segments, nodes, args.traffic_side)
    riding_s, turning_s = estimate_seconds(segments, profile)
    wrong_km = sum(s["length"] for s in segments if s.get("against")) / 1000

    total = sum(s["length"] for s in segments)
    deadhead = total - service_len
    route_csv = write_route_csv(segments, F, nodes, out_dir)
    route_map = write_map(segments, nodes, out_dir,
                          (riding_s + turning_s) / 60)

    if args.history:
        import round_history
        version, is_new = round_history.record(
            Path(args.history), data_dir, out_dir=out_dir,
            note=args.note, bump=args.bump,
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
                "segments_deadhead": sum(1 for s in segments
                                         if s["kind"] == "deadhead"),
                "start": args.start or "",
                "end": args.end or "",
                "return_to_start": bool(args.return_to_start),
                "edges_total": len(edges),
                "profile": profile_name,
                "wrong_way_penalty": args.wrong_way_penalty,
                "wrong_way_km": round(wrong_km, 3),
                "time_min": round((riding_s + turning_s) / 60, 1),
                "turns_cross": turns["cross"],
                "turns_u": turns["u"],
                "turn_optimised": not args.no_turn_optimisation,
            })
        print("  history: " + (f"recorded version {version}" if is_new
                               else f"unchanged since version {version}")
              + f" (in {args.history})")

    print(f"""
================ RESULT ({kind}) ================
Mandatory service riding : {service_len / 1000:7.2f} km   (theoretical lower bound)
Route total              : {total / 1000:7.2f} km
Extra / deadhead         : {deadhead / 1000:7.2f} km   ({100 * deadhead / max(total, 1):.1f}% of route)
Segments                 : {sum(1 for s in segments if s['kind'] == 'service')} service + \
{sum(1 for s in segments if s['kind'] == 'deadhead')} deadhead
Riding time (rough)      : {hhmm(riding_s + turning_s):>9}   \
({hhmm(riding_s)} moving + {hhmm(turning_s)} turning; excludes every stop)
Turns                    : {turns['cross']} crossing + {turns['u']} U-turn + \
{turns['easy']} easy + {turns['straight']} straight
Against a one-way        : {wrong_km:7.2f} km   (ride the footpath there)
Outputs                  : {route_csv}
                           {route_map}
=================================================
The 'extra' number is the only part optimisation can influence --
the mandatory part is your job itself.""")


if __name__ == "__main__":
    main()
