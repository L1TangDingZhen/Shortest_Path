#!/usr/bin/env python3
"""End-to-end test on a synthetic suburb grid (no internet needed).

Builds a 3x4 street grid at synthetic coordinates with a mix of
service 2 / 1 / 0 edges plus a dead-end court, runs the solver in both
circuit and open modes, and asserts the core invariants:

  * every service=2 edge is traversed exactly twice, service=1 exactly once
  * consecutive segments share a node (the route is a real walk)
  * circuit mode: route ends where it started
  * open mode: total distance <= circuit total
  * route length >= mandatory lower bound
  * a disconnected service set gets auto-bridged and still yields a walk
  * a service=x edge is excluded: the route never touches it
  * SPEC-2: --start + --end yields an open walk from the snapped start
    to the snapped end; --end alone pins the finish; --end + --open is
    rejected
  * waiting at a signalised crossing is priced where the path is chosen:
    turn-aware routing takes 300 m of clear road over 200 m through a
    signal, plain Dijkstra does not
  * searching over Euler tours never changes the distance
  * a two-sided street is ridden once each way, so both kerbs are
    delivered -- under every preference, not just the lucky ones
  * a closed way (a loop road: one junction, u == v) is ridden one lap
    each way round, inner kerb and outer kerb; with no geometry to
    orient it the solver says so instead of asserting
  * a pinned start really starts there, and a pinned end in the middle
    of a street really finishes there
  * the reverse arc search returns a path that starts where its key says
  * the speed of the road being entered reaches the turn cost
  * explicit vehicle / numeric preferences override saved penalties,
    change the connector used, and remain soft costs
  * variants report their own penalties and record the actual one

Run:  python test_solve.py
"""

import csv
import json
import math
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

BASE = Path(__file__).parent
TMP = BASE / "_test_tmp"


def write_case(data_dir: Path, disconnect=False):
    data_dir.mkdir(parents=True, exist_ok=True)
    lat0, lon0, step = -37.845, 144.950, 0.001
    nodes = {}
    for r in range(3):
        for c in range(4):
            nodes[f"n{r}{c}"] = (lat0 - r * step, lon0 + c * step)
    nodes["court"] = (lat0 + step, lon0 + step)  # dead-end off n01

    def L(a, b):  # rough metres
        (la, lo), (lb, lo2) = nodes[a], nodes[b]
        return round((((la - lb) * 111000) ** 2 +
                      ((lo - lo2) * 111000 * math.cos(math.radians(lat0))) ** 2) ** 0.5, 1)

    edges = []
    # horizontal streets: service 2 ("A Street", "B Street", "C Street")
    for r, nm in enumerate(["A Street", "B Street", "C Street"]):
        for c in range(3):
            u, v = f"n{r}{c}", f"n{r}{c+1}"
            edges.append((f"{u}-{v}-0", nm, u, v, 2))
    # vertical avenues: cols 0 and 3 service 1, middle cols service 0
    for c in range(4):
        s = 1 if c in (0, 3) else 0
        if disconnect:
            s = 0  # rows only linked by service-0 edges -> islands
        for r in range(2):
            u, v = f"n{r}{c}", f"n{r+1}{c}"
            edges.append((f"{u}-{v}-0", f"{c+1} Avenue", u, v, s))
    # dead-end court, both sides delivered
    edges.append(("n01-court-0", "Quiet Court", "n01", "court", 2))

    with open(data_dir / "nodes.csv", "w", newline="",
              encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["node_id", "lat", "lon"])
        for n, (la, lo) in nodes.items():
            w.writerow([n, la, lo])
    with open(data_dir / "edges.csv", "w", newline="",
              encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["edge_id", "name", "highway", "length_m", "service",
                    "note", "u", "v", "geometry_wkt"])
        for eid, nm, u, v, s in edges:
            w.writerow([eid, nm, "residential", L(u, v), s, "", u, v, ""])
    return {eid: s for eid, _, _, _, s in edges}


def write_simple(data_dir: Path, nodes, edges):
    """nodes: {id: (lat, lon)}; edges: (eid, name, u, v, service)."""
    data_dir.mkdir(parents=True, exist_ok=True)
    with open(data_dir / "nodes.csv", "w", newline="",
              encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["node_id", "lat", "lon", "control", "control_group"])
        for n, (la, lo) in nodes.items():
            w.writerow([n, la, lo, "", ""])
    with open(data_dir / "edges.csv", "w", newline="",
              encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["edge_id", "name", "highway", "oneway",
                    "maxspeed_kmh", "length_m", "service", "note",
                    "u", "v", "geometry_wkt"])
        for eid, nm, u, v, sv in edges:
            (la, lo), (lb, lo2) = nodes[u], nodes[v]
            m = round((((la - lb) * 111000) ** 2 +
                       ((lo - lo2) * 111000 *
                        math.cos(math.radians(la))) ** 2) ** 0.5, 1)
            w.writerow([eid, nm, "residential", "", "", m, sv, "",
                        u, v, ""])
    return {e[0]: (e[2], e[3]) for e in edges}, \
        {e[0]: e[4] for e in edges}


def write_loop_case(data_dir: Path, geometry=True):
    """A closed way -- one junction, u == v -- on a short feeder.
    OSM is full of these: loop roads and cul-de-sac circles."""
    data_dir.mkdir(parents=True, exist_ok=True)
    cx, cy, r = 144.9510, -37.8450, 0.0006
    ring = [(cx, cy + r)]
    for i in range(1, 13):
        a = 2 * math.pi * i / 12
        ring.append((cx + r * math.sin(a), cy + r * math.cos(a)))
    wkt = "LINESTRING (" + ", ".join(f"{x} {y}" for x, y in ring) + ")"
    with open(data_dir / "nodes.csv", "w", newline="",
              encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["node_id", "lat", "lon", "control", "control_group"])
        w.writerow(["J", cy + r, cx, "", ""])
        w.writerow(["K", cy + r + 0.001, cx, "", ""])
    with open(data_dir / "edges.csv", "w", newline="",
              encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["edge_id", "name", "highway", "oneway",
                    "maxspeed_kmh", "length_m", "service", "note",
                    "u", "v", "geometry_wkt"])
        w.writerow(["loop", "Ring Close", "residential", "", "", 377, 2,
                    "", "J", "J", wkt if geometry else ""])
        w.writerow(["stub", "Feeder Street", "residential", "", "", 111,
                    1, "", "J", "K", ""])
    return {"loop": ("J", "J"), "stub": ("J", "K")}, \
        {"loop": 2, "stub": 1}


def write_cost_case(data_dir: Path):
    """One long service street plus two ways back: a short footway and
    a slightly longer road that is one-way against the direction of
    travel. Which one the deadhead takes reveals the cost profile."""
    data_dir.mkdir(parents=True, exist_ok=True)
    nodes = {"A": (-37.8450, 144.9500), "B": (-37.8450, 144.9530)}
    with open(data_dir / "nodes.csv", "w", newline="",
              encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["node_id", "lat", "lon"])
        for n, (la, lo) in nodes.items():
            w.writerow([n, la, lo])
    rows = [
        # id,   name,   highway, oneway, maxspeed, len, service, u, v
        ("svc", "Long Street", "residential", "False", "", 300, 1, "A", "B"),
        ("foot", "The Path", "footway", "False", "", 100, 0, "A", "B"),
        ("road", "Back Road", "residential", "True", "50", 130, 0, "B", "A"),
        ("fast", "Quick Road", "secondary", "False", "80", 200, 0, "A", "B"),
    ]
    with open(data_dir / "edges.csv", "w", newline="",
              encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["edge_id", "name", "highway", "oneway", "maxspeed_kmh",
                    "length_m", "service", "note", "u", "v",
                    "geometry_wkt"])
        for eid, nm, hw, ow, ms, ln, sv, u, v in rows:
            w.writerow([eid, nm, hw, ow, ms, ln, sv, "", u, v, ""])
    return {eid: sv for eid, _, _, _, _, _, sv, _, _ in rows}


def write_delay_case(data_dir: Path):
    """One street to deliver and two ways back: a short one through a
    signalised crossing, a longer one without. Which one the deadhead
    takes says whether waiting is priced where the path is chosen."""
    data_dir.mkdir(parents=True, exist_ok=True)
    # both detours bend the same way and barely at all, so turn cost is
    # zero on each and only length and waiting decide
    nodes = [("A", -37.84500, 144.9500, "", ""),
             ("B", -37.84500, 144.9530, "", ""),
             ("M", -37.84505, 144.9515, "signals", "1"),
             ("N", -37.84510, 144.9515, "", "")]
    with open(data_dir / "nodes.csv", "w", newline="",
              encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["node_id", "lat", "lon", "control", "control_group"])
        w.writerows(nodes)
    # 200 m through the signal (14 s riding + 25 s waiting) against
    # 300 m clear (22 s); going back along the street itself is 72 s
    rows = [("svc", "Long Street", "residential", 1000, 1, "A", "B"),
            ("sig_a", "Quick Way", "residential", 100, 0, "A", "M"),
            ("sig_b", "Quick Way", "residential", 100, 0, "M", "B"),
            ("free_a", "Clear Way", "residential", 150, 0, "A", "N"),
            ("free_b", "Clear Way", "residential", 150, 0, "N", "B")]
    with open(data_dir / "edges.csv", "w", newline="",
              encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["edge_id", "name", "highway", "oneway", "maxspeed_kmh",
                    "length_m", "service", "note", "u", "v",
                    "geometry_wkt"])
        for eid, nm, hw, ln, sv, u, v in rows:
            w.writerow([eid, nm, hw, "False", "", ln, sv, "", u, v, ""])
    return {eid: sv for eid, _, _, _, sv, _, _ in rows}


def deadhead_ids(rows):
    return [r["edge_id"] for r in rows if r["type"] == "deadhead"]


def wrong_way_m(rows):
    return sum(float(r["length_m"]) for r in rows
               if r["against_oneway"] == "yes")


def walk_directions(edges, rows):
    """(edge_id, travelled_forward) per row, by chaining the walk."""
    out, cur = [], None
    for i, r in enumerate(rows):
        u, v = edges[r["edge_id"]]["u"], edges[r["edge_id"]]["v"]
        if cur is None:
            nxt = rows[i + 1]["edge_id"] if i + 1 < len(rows) else None
            ends = ((edges[nxt]["u"], edges[nxt]["v"]) if nxt else ())
            cur = v if (v in ends and u not in ends) else (
                u if v not in ends else v)
        else:
            cur = v if cur == u else u
        out.append((r["edge_id"], cur == v))
    return out


def check_oneway_flags(data_dir: Path, rows):
    """against_oneway must be exactly 'this one-way was ridden v->u'."""
    with open(data_dir / "edges.csv", newline="",
              encoding="utf-8-sig") as f:
        edges = {r["edge_id"]: r for r in csv.DictReader(f)}
    for r, (eid, forward) in zip(rows, walk_directions(edges, rows)):
        oneway = (edges[eid]["oneway"] or "").strip().lower() == "true"
        expected = "yes" if (oneway and not forward) else ""
        assert r["against_oneway"] == expected, \
            f"{eid}: flag {r['against_oneway']!r}, expected {expected!r}"


def read_route(out_dir: Path):
    with open(out_dir / "route.csv", newline="",
              encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def run(data_dir, out_dir, *extra):
    cmd = [sys.executable, str(BASE / "solve_route.py"),
           "--data", str(data_dir), "--out", str(out_dir), *extra]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(res.stdout)
        print(res.stderr)
        raise AssertionError(f"solver failed: {' '.join(extra) or 'circuit'}")
    return res.stdout


def check_walk(rows, edges_by_id, service_spec, closed):
    # 1. service multiplicities are exact
    counts = Counter(r["edge_id"] for r in rows if r["type"] == "service")
    for eid, s in service_spec.items():
        assert counts.get(eid, 0) == s, \
            f"{eid}: expected {s} service passes, got {counts.get(eid, 0)}"
    # deadhead may only use existing edges
    for r in rows:
        assert r["edge_id"] in service_spec, f"unknown edge {r['edge_id']}"
    # 1b. a street delivered on both sides must be ridden once each
    #     way, or one kerb gets the mail twice and the other never
    sides = defaultdict(list)
    for r in rows:
        if r["type"] == "service" and service_spec.get(r["edge_id"]) == 2:
            sides[r["edge_id"]].append(r["side"])
    for eid, got in sides.items():
        u, v = edges_by_id[eid]
        if u == v and got == ["", ""]:
            continue        # a closed way with no shape: nothing to name
        assert len(got) == 2 and "" not in got and got[0] != got[1], \
            f"{eid}: both passes deliver the {got[0]} kerb -- " \
            f"the other side of the street gets nothing"
    # 2. continuity: consecutive segments share a node.  route.csv
    #    carries no u/v, so the walk is rebuilt from edge endpoints --
    #    and the very first segment's direction is genuinely ambiguous
    #    (pass pairing makes segments 1 and 2 the same edge, so "which
    #    end did we start at" cannot be read off the next segment).
    #    Try both and accept the one that chains.
    ends = [edges_by_id[r["edge_id"]] for r in rows]
    failure = None
    for start in (0, 1):
        first, cur = ends[0][start], ends[0][1 - start]
        for i, (u, v) in enumerate(ends[1:], 1):
            if cur == u:
                cur = v
            elif cur == v:
                cur = u
            else:
                failure = f"walk breaks at segment {i + 1}"
                break
        else:
            if closed and cur != first:
                failure = "circuit does not close"
                continue
            return cur, first
    raise AssertionError(failure or "walk breaks")


def total_km(rows):
    return float(rows[-1]["cum_km"])


def test_vehicle_preferences():
    data = TMP / "vehicle_data"
    by_id, spec = write_simple(
        data, {"A": (-37.845, 144.950), "B": (-37.845, 144.951),
               "C": (-37.845, 144.952)},
        [("svc", "Service Street", "A", "B", 1),
         ("reverse", "One Way", "C", "B", 0),
         ("detour", "Detour Street", "B", "C", 0)])
    with open(data / "edges.csv", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fields, edges = reader.fieldnames, list(reader)
    for edge in edges:
        edge["length_m"] = 200 if edge["edge_id"] == "detour" else 100
        edge["oneway"] = "True" if edge["edge_id"] == "reverse" else "False"
    with open(data / "edges.csv", "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(edges)

    def solve(name, saved, flags, factor, source, connector):
        (data / "endpoints.json").write_text(json.dumps(saved),
                                           encoding="utf-8")
        out, hist = TMP / name, TMP / (name + "_history")
        stdout = run(data, out, "--start=-37.845,144.950",
                     "--end=-37.845,144.952", "--no-tour-search",
                     "--history", str(hist), *flags)
        rows = read_route(out)
        check_walk(rows, by_id, spec, closed=False)
        assert deadhead_ids(rows) == [connector], (name, rows)
        assert f"wrong-way penalty x{factor:g} ({source})" in stdout, stdout
        summary = json.loads(next(hist.glob("*/summary.json"))
                             .read_text(encoding="utf-8"))
        assert summary["wrong_way_penalty"] == factor, (name, summary)
        assert summary["wrong_way_source"] == source, (name, summary)
        if connector == "reverse":
            assert wrong_way_m(rows) == 100, rows
        return stdout

    print("== vehicle preferences override saved values and steer the route ==")
    solve("vehicle_default", {}, [], 1, "default", "reverse")
    solve("vehicle_saved", {"wrong_way_penalty": 3}, [], 3,
          "endpoints.json", "detour")
    for vehicle, factor in (("footpath", 1), ("tricycle", 3),
                            ("carriageway", 12)):
        for saved in ({}, {"wrong_way_penalty": 3},
                      {"wrong_way_penalty": 7}):
            name = f"vehicle_{vehicle}_{saved.get('wrong_way_penalty', 'empty')}"
            solve(name, saved, ["--vehicle", vehicle], factor,
                  f"--vehicle {vehicle}",
                  "reverse" if factor == 1 else "detour")
    solve("vehicle_numeric", {"wrong_way_penalty": 3},
          ["--wrong-way-penalty", "7"], 7, "--wrong-way-penalty", "detour")
    solve("vehicle_numeric_wins", {"wrong_way_penalty": 3},
          ["--vehicle", "carriageway", "--wrong-way-penalty", "1"],
          1, "--wrong-way-penalty", "reverse")

    print("== variants announce overrides and history uses the actual factor ==")
    stdout = solve("vehicle_variant", {"wrong_way_penalty": 3},
                   ["--variants", "fastest", "--vehicle", "carriageway",
                    "--wrong-way-penalty", "7"],
                   1, "--variants fastest", "reverse")
    assert "--variants uses preset" in stdout, stdout
    assert "overridden: --vehicle, --wrong-way-penalty, " \
           "endpoints.json wrong_way_penalty" in stdout, stdout

    print("== even a strong preference permits a necessary reverse traversal ==")
    for edge in edges:
        if edge["edge_id"] == "detour":
            edge["service"] = "x"
    with open(data / "edges.csv", "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(edges)
    solve("vehicle_required_reverse", {}, ["--vehicle", "carriageway"],
          12, "--vehicle carriageway", "reverse")

    print("== invalid saved preferences fail with their source ==")
    for invalid in (0.5, "NaN", "Infinity", "not a number"):
        (data / "endpoints.json").write_text(
            json.dumps({"wrong_way_penalty": invalid}), encoding="utf-8")
        res = subprocess.run(
            [sys.executable, str(BASE / "solve_route.py"),
             "--data", str(data), "--out", str(TMP / "vehicle_invalid")],
            capture_output=True, text=True)
        assert res.returncode != 0, res.stdout
        assert "endpoints.json: wrong-way penalty must be a finite number" \
               in res.stderr, res.stderr
    solve("vehicle_overrides_invalid_saved", {"wrong_way_penalty": "bad"},
          ["--vehicle", "footpath"], 1, "--vehicle footpath", "reverse")


def main():
    if TMP.exists():
        shutil.rmtree(TMP)
    data = TMP / "data"
    spec = write_case(data)
    edges_by_id = {}
    with open(data / "edges.csv", newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            edges_by_id[r["edge_id"]] = (r["u"], r["v"])
    bound = 0.0
    with open(data / "edges.csv", newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            bound += float(r["length_m"]) * int(r["service"])

    print("== circuit mode ==")
    out1 = TMP / "circuit"
    run(data, out1)
    rows1 = read_route(out1)
    check_walk(rows1, edges_by_id, spec, closed=True)
    assert total_km(rows1) * 1000 >= bound - 1, "route below lower bound?!"
    assert (out1 / "route_map.html").exists()
    html = (out1 / "route_map.html").read_text(encoding="utf-8")
    m = re.search(r'<script id="route-data" type="application/json">'
                  r"(.*?)</script>", html, re.DOTALL)
    viewer = json.loads(m.group(1))
    assert len(viewer["segs"]) == len(rows1), "viewer payload out of sync"

    print("== turn optimisation is free (same distance, same work) ==")
    out_raw = TMP / "circuit_raw"
    run(data, out_raw, "--no-turn-optimisation")
    rows_raw = read_route(out_raw)
    check_walk(rows_raw, edges_by_id, spec, closed=True)
    assert abs(total_km(rows_raw) - total_km(rows1)) < 1e-6, \
        f"every Euler tour has the same length: {total_km(rows_raw)} " \
        f"vs {total_km(rows1)}"
    assert len(rows_raw) == len(rows1), "same number of traversals"

    print("== pass pairing puts both sides back to back, free ==")
    out_pair = TMP / "paired"
    stdout = run(data, out_pair, "--pair-passes")
    rows_pair = read_route(out_pair)
    check_walk(rows_pair, edges_by_id, spec, closed=True)
    assert abs(total_km(rows_pair) - total_km(rows1)) < 1e-6, \
        "pairing must not change the distance"

    def paired_pct(rows):
        at = defaultdict(list)
        for i, r in enumerate([r for r in rows if r["type"] == "service"]):
            at[r["edge_id"]].append(i)
        both = [v for v in at.values() if len(v) == 2]
        return 100 * sum(1 for v in both if v[1] - v[0] == 1) / len(both)

    assert paired_pct(rows_pair) >= paired_pct(rows1), \
        f"pairing {paired_pct(rows_pair):.0f}% vs default {paired_pct(rows1):.0f}%"
    assert "Both sides back to back" in stdout, stdout

    print("== turn-aware routing covers the same work ==")
    out_turn = TMP / "turn"
    run(data, out_turn, "--algorithm", "turn")
    rows_turn = read_route(out_turn)
    check_walk(rows_turn, edges_by_id, spec, closed=True)
    assert total_km(rows_turn) * 1000 >= bound - 1

    print("== --variants lays the preferences side by side ==")
    out_all = TMP / "alts"
    variants_hist = TMP / "alts_history"
    stdout = run(data, out_all, "--variants",
                 "shortest,fastest,recommended", "--vehicle", "footpath",
                 "--wrong-way-penalty", "7", "--history", str(variants_hist))
    assert "ROUTE PREFERENCES" in stdout and "Quickest:" in stdout, stdout
    assert "overridden: --vehicle, --wrong-way-penalty" in stdout, stdout
    for name in ("shortest", "fastest", "recommended"):
        assert (out_all / name / "route.csv").exists(), name
        check_walk(read_route(out_all / name), edges_by_id, spec,
                   closed=True)
    page = (out_all / "alternatives.html").read_text(encoding="utf-8")
    payload = json.loads(re.search(
        r'<script id="alt-data" type="application/json">(.*?)</script>',
        page, re.DOTALL).group(1))
    assert len(payload["routes"]) == 3, payload["routes"]
    assert {r["variant"] for r in payload["routes"]} == \
        {"shortest", "fastest", "recommended"}
    assert all(r["segs"] for r in payload["routes"])
    assert {r["variant"]: r["wrong_way_penalty"] for r in payload["routes"]} \
        == {"shortest": 1, "fastest": 1, "recommended": 3}
    stored = json.loads(next(variants_hist.glob("*/summary.json"))
                        .read_text(encoding="utf-8"))
    selected = next(r for r in payload["routes"]
                    if r["variant"] == stored["variant"])
    assert stored["wrong_way_penalty"] == selected["wrong_way_penalty"], stored
    assert stored["wrong_way_source"] == selected["wrong_way_source"], stored
    # the top-level outputs describe one of the alternatives
    assert (out_all / "route.csv").exists()
    res = subprocess.run(
        [sys.executable, str(BASE / "solve_route.py"), "--data", str(data),
         "--out", str(TMP / "badvar"), "--variants", "nonsense"],
        capture_output=True, text=True)
    assert res.returncode != 0, "unknown preference must be rejected"

    print("== waiting at crossings is priced where paths are chosen ==")
    dd = TMP / "delay"
    dspec = write_delay_case(dd)
    dby = {"svc": ("A", "B"), "sig_a": ("A", "M"), "sig_b": ("M", "B"),
           "free_a": ("A", "N"), "free_b": ("N", "B")}

    out_nd, hist = TMP / "delay_node", TMP / "delay_hist"
    stdout = run(dd, out_nd, "--algorithm", "node",
                 "--history", str(hist), "--note", "delay")
    rows_nd = read_route(out_nd)
    check_walk(rows_nd, dby, dspec, closed=True)
    # plain Dijkstra cannot see the signal: it takes the short way
    assert set(deadhead_ids(rows_nd)) == {"sig_a", "sig_b"}, \
        deadhead_ids(rows_nd)
    assert "waiting at crossings" in stdout, stdout
    # 25 s rounds to "0m" on screen, so check the number that was stored
    summary = json.loads(next(hist.glob("*/summary.json"))
                         .read_text(encoding="utf-8"))
    assert summary["waiting_min"] > 0, summary
    assert summary["time_min"] > 0

    out_td = TMP / "delay_turn"
    run(dd, out_td, "--algorithm", "turn")
    rows_td = read_route(out_td)
    check_walk(rows_td, dby, dspec, closed=True)
    # 100 m + a 25 s wait is dearer than 150 m of clear footway
    assert set(deadhead_ids(rows_td)) == {"free_a", "free_b"}, \
        deadhead_ids(rows_td)
    assert total_km(rows_td) > total_km(rows_nd), \
        "avoiding the signal is longer -- that is the trade"

    print("== the tour search never breaks or lengthens the tour ==")
    out_ns = TMP / "nosearch"
    run(data, out_ns, "--no-tour-search")
    rows_ns = read_route(out_ns)
    check_walk(rows_ns, edges_by_id, spec, closed=True)
    assert abs(total_km(rows_ns) - total_km(rows1)) < 1e-6, \
        f"searching tours must not change distance: {total_km(rows_ns)} " \
        f"vs {total_km(rows1)}"

    print("== open mode ==")
    out2 = TMP / "open"
    run(data, out2, "--open")
    rows2 = read_route(out2)
    check_walk(rows2, edges_by_id, spec, closed=False)
    assert total_km(rows2) <= total_km(rows1) + 1e-6, \
        "open route should never be longer than the circuit"

    print("== --start snapping ==")
    out3 = TMP / "start"
    run(data, out3, "--start=-37.8472,144.9532")  # near n23
    rows3 = read_route(out3)
    check_walk(rows3, edges_by_id, spec, closed=True)

    print("== disconnected service set (auto-bridge) ==")
    data2 = TMP / "data_disc"
    spec2 = write_case(data2, disconnect=True)
    out4 = TMP / "disc"
    stdout = run(data2, out4)
    assert "bridge" in stdout, "expected auto-bridge warning"
    rows4 = read_route(out4)
    check_walk(rows4, edges_by_id, spec2, closed=True)

    print("== excluded edge (service=x) ==")
    data3 = TMP / "data_x"
    spec3 = write_case(data3)
    banned = "n02-n12-0"  # a middle-avenue connector
    with open(data3 / "edges.csv", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        header = list(reader.fieldnames)
        erows = list(reader)
    for r in erows:
        if r["edge_id"] == banned:
            r["service"] = "x"
    with open(data3 / "edges.csv", "w", newline="",
              encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        w.writerows(erows)
    out5 = TMP / "excl"
    stdout = run(data3, out5)
    assert "excluded" in stdout, "expected exclusion note"
    rows5 = read_route(out5)
    assert all(r["edge_id"] != banned for r in rows5), \
        "route used an excluded (service=x) edge"
    spec3.pop(banned)
    check_walk(rows5, edges_by_id, spec3, closed=True)

    print("== pinned start + end (SPEC-2) ==")
    out6 = TMP / "pinned"
    run(data, out6, "--start=-37.845,144.950", "--end=-37.8471,144.9529")
    rows6 = read_route(out6)
    end6, first6 = check_walk(rows6, edges_by_id, spec, closed=False)
    assert first6 == "n00", f"route should start at n00, got {first6}"
    assert end6 == "n23", f"route should end at n23, got {end6}"
    assert total_km(rows6) * 1000 >= bound - 1

    print("== pinned end only ==")
    out7 = TMP / "endonly"
    run(data, out7, "--end=-37.8471,144.9529")
    rows7 = read_route(out7)
    end7, _ = check_walk(rows7, edges_by_id, spec, closed=False)
    assert end7 == "n23", f"route should end at n23, got {end7}"

    print("== closed tour: --start + --end + --return-to-start ==")
    out8 = TMP / "tour"
    run(data, out8, "--start=-37.845,144.950", "--end=-37.8471,144.9529",
        "--return-to-start")
    rows8 = read_route(out8)
    end8, first8 = check_walk(rows8, edges_by_id, spec, closed=True)
    assert first8 == "n00", f"tour should start at n00, got {first8}"
    assert total_km(rows8) >= total_km(rows6), \
        "the tour includes a return leg, it cannot be shorter"

    print("== endpoints.json picked up automatically ==")
    data4 = TMP / "data_ep"
    write_case(data4)
    (data4 / "endpoints.json").write_text(json.dumps({
        "start": [-37.845, 144.950], "end": [-37.8471, 144.9529],
        "return_to_start": True}), encoding="utf-8")
    out9 = TMP / "auto"
    stdout = run(data4, out9)                    # no --start/--end flags
    assert "using endpoints from" in stdout, stdout
    assert "return to start" in stdout, stdout
    rows9 = read_route(out9)
    end9, first9 = check_walk(rows9, edges_by_id, spec, closed=True)
    assert first9 == "n00", f"auto start should be n00, got {first9}"

    print("== speed profile steers deadhead off the footpath ==")
    dc = TMP / "cost"
    cspec = write_cost_case(dc)
    cby = {"svc": ("A", "B"), "foot": ("A", "B"), "road": ("B", "A"),
           "fast": ("A", "B")}

    out_e = TMP / "cost_edv"
    run(dc, out_e, "--profile", "edv")
    rows_e = read_route(out_e)
    check_walk(rows_e, cby, cspec, closed=True)
    # 130 m at 50 km/h beats 100 m of footpath at 10, and beats the
    # 200 m road the vehicle cannot use at its full 80
    assert deadhead_ids(rows_e) == ["road"], deadhead_ids(rows_e)
    assert total_km(rows_e) > 0.400, \
        "the quicker route is longer than the shortest -- that is the point"

    print("== 'limits' uses the posted limit, 'edv' caps at the vehicle ==")
    out_l = TMP / "cost_limits"
    run(dc, out_l, "--profile", "limits")
    rows_l = read_route(out_l)
    check_walk(rows_l, cby, cspec, closed=True)
    # 200 m at 80 km/h is quicker than 130 m at 50 -- but only for a
    # vehicle that can do 80, which is exactly what the two differ on
    assert deadhead_ids(rows_l) == ["fast"], deadhead_ids(rows_l)
    assert total_km(rows_l) > total_km(rows_e)
    rows_d = rows_e

    print("== against_oneway agrees with the direction actually ridden ==")
    assert "against_oneway" in rows_e[0], list(rows_e[0])
    assert all(r["against_oneway"] == "" for r in rows_d), \
        "the footway route has nothing to flag"
    for rows in (rows_d, rows_e):
        check_oneway_flags(dc, rows)

    print("== the tour prefers riding the one-way legally ==")
    # both directions cover the same edges, so choosing the legal one
    # is free -- an unguided tour has no reason to prefer it
    out_raw = TMP / "cost_raw"
    run(dc, out_raw, "--profile", "edv", "--no-turn-optimisation")
    rows_raw = read_route(out_raw)
    assert abs(total_km(rows_raw) - total_km(rows_e)) < 1e-6, \
        "turn optimisation must not change the distance"
    assert wrong_way_m(rows_e) <= wrong_way_m(rows_raw), \
        f"optimised tour rides {wrong_way_m(rows_e)} m the wrong way, " \
        f"unguided only {wrong_way_m(rows_raw)} m"

    print("== wrong-way penalty avoids the one-way ==")
    out_w = TMP / "cost_wrong"
    run(dc, out_w, "--profile", "edv", "--wrong-way-penalty", "3")
    rows_w = read_route(out_w)
    check_walk(rows_w, cby, cspec, closed=True)
    assert all(r["against_oneway"] == "" for r in rows_w)

    test_vehicle_preferences()

    print("== a pinned start really starts there ==")
    chain = {"A": (-37.845, 144.950), "B": (-37.845, 144.951),
             "C": (-37.845, 144.952)}
    cdir = TMP / "chain"
    cby2, cspec2 = write_simple(cdir, chain,
                                [("e1", "Chain Street", "A", "B", 1),
                                 ("e2", "Chain Street", "B", "C", 1)])
    out_s = TMP / "chain_start"
    run(cdir, out_s, "--open", "--start=-37.845,144.952")
    rows_s = read_route(out_s)
    assert rows_s[0]["edge_id"] == "e2" and rows_s[0]["direction"] == "W", \
        f"pinned start ignored: first step {rows_s[0]['edge_id']} " \
        f"heading {rows_s[0]['direction']}"

    print("== a pinned end in mid-street really finishes there ==")
    out_m = TMP / "chain_end"
    run(cdir, out_m, "--end=-37.845,144.951")
    rows_m = read_route(out_m)
    last = rows_m[-1]
    assert (last["edge_id"], last["direction"]) in (("e1", "E"), ("e2", "W")), \
        f"pinned end ignored: last step {last['edge_id']} heading " \
        f"{last['direction']}"
    # A-B-C with the finish at B: ride the whole chain, then come back
    # over one edge. Three segments, no more -- anything shorter cannot
    # finish at B, and anything longer is not the cheapest way to.
    one = float(rows_m[0]["length_m"])
    assert len(rows_m) == 3 and abs(total_km(rows_m) - 3 * one / 1000) < 1e-3, \
        f"expected 3 segments of {one} m, got {len(rows_m)} totalling " \
        f"{total_km(rows_m):.3f} km"

    print("== both kerbs of a two-sided street, under every preference ==")
    # Two disjoint ways round mean the tour CAN take the middle street
    # twice the same way, and the turn-comfort chooser prefers to.
    theta = {"u": (-37.845, 144.950), "v": (-37.845, 144.952),
             "m1": (-37.844, 144.951), "m2": (-37.846, 144.951)}
    tdir = TMP / "theta"
    tby, tspec = write_simple(tdir, theta, [
        ("E", "Both Sides Street", "u", "v", 2),
        ("p1a", "North Way", "u", "m1", 1),
        ("p1b", "North Way", "m1", "v", 1),
        ("p2a", "South Way", "u", "m2", 1),
        ("p2b", "South Way", "m2", "v", 1)])
    for extra in ([], ["--no-turn-optimisation"], ["--no-tour-search"],
                  ["--variants", "all"], ["--algorithm", "turn"]):
        out_t = TMP / ("theta" + (extra[0].strip("-") if extra else "def"))
        run(tdir, out_t, *extra)
        rows_t = read_route(out_t)
        check_walk(rows_t, tby, tspec, closed=True)

    print("== a closed way is ridden one lap each way round ==")
    ldir = TMP / "loop"
    lby, lspec = write_loop_case(ldir)
    out_l = TMP / "loop_out"
    run(ldir, out_l)
    rows_l = read_route(out_l)
    check_walk(rows_l, lby, lspec, closed=True)
    laps = [r["side"] for r in rows_l if r["edge_id"] == "loop"
            and r["type"] == "service"]
    assert sorted(laps) == ["inner", "outer"], laps
    ways = [r["direction"] for r in rows_l if r["edge_id"] == "loop"
            and r["type"] == "service"]
    assert len(set(ways)) == 2, f"both laps set off {ways[0]}"

    print("== a closed way with no geometry says so, and does not crash ==")
    bdir = TMP / "loop_bare"
    bby, bspec = write_loop_case(bdir, geometry=False)
    out_b = TMP / "loop_bare_out"
    stdout = run(bdir, out_b)
    rows_b = read_route(out_b)
    check_walk(rows_b, bby, bspec, closed=True)
    assert "closed way" in stdout and "no geometry" in stdout, stdout
    assert all(r["side"] == "" for r in rows_b
               if r["edge_id"] == "loop"), "a shapeless ring has no kerbs"

    print("== the reverse arc search agrees with its own keys ==")
    sys.path.insert(0, str(BASE))
    import solve_route as S
    sn = {"A": (0.0, 0.0), "B": (0.0, 0.001), "C": (0.0, 0.002)}
    se = [dict(edge_id="e1", u="A", v="B", name="Chain Street",
               highway="residential", oneway=False, maxspeed=None,
               length=100.0, service=1, wkt=""),
          dict(edge_id="e2", u="B", v="C", name="Chain Street",
               highway="residential", oneway=False, maxspeed=None,
               length=100.0, service=1, wkt="")]
    F, R, D = S.build_graphs(se, S.load_profile("distance")[1])
    for kind in ("node", "turn"):
        _, paths = S.route_from(S.make_router(D, sn, kind), ["C"],
                                reverse=True)
        for n, arcs in paths.items():
            assert not arcs or arcs[0][0] == n, \
                f"{kind}: path keyed {n} starts at {arcs[0][0]}"
            assert not arcs or arcs[-1][1] == "C", \
                f"{kind}: path keyed {n} ends at {arcs[-1][1]}, not C"

    print("== the speed of the road entered reaches the turn cost ==")
    se[0]["maxspeed"] = se[1]["maxspeed"] = 60.0
    F, R, D = S.build_graphs(se, S.load_profile("edv")[1])
    meta = S.tour_meta(R, F, D, sn)
    speeds = {k: m.get("kmh") for k, m in meta.items()}
    assert set(speeds.values()) == {50.0}, \
        f"the tour chooser sees {speeds} -- graded turn costs are inert"
    assert S.turn_seconds("cross", 60.0) > S.turn_seconds("cross", 20.0), \
        "turning across a fast road must cost more than across a slow one"

    print("== bad profile / penalty are rejected ==")
    for bad in (["--profile", "nonsense"], ["--wrong-way-penalty", "0.5"],
                ["--wrong-way-penalty", "nan"], ["--wrong-way-penalty", "inf"],
                ["--vehicle", "nonsense"]):
        res = subprocess.run(
            [sys.executable, str(BASE / "solve_route.py"), "--data",
             str(dc), "--out", str(TMP / "reject2"), *bad],
            capture_output=True, text=True)
        assert res.returncode != 0, f"{bad} should have been rejected"

    print("== --end with --open rejected ==")
    res = subprocess.run(
        [sys.executable, str(BASE / "solve_route.py"),
         "--data", str(data), "--out", str(TMP / "reject"),
         "--open", "--end=-37.8471,144.9529"],
        capture_output=True, text=True)
    assert res.returncode != 0, "--end plus --open must be rejected"

    print(f"""
ALL TESTS PASSED
  circuit total : {total_km(rows1):.3f} km   (lower bound {bound / 1000:.3f} km)
  open total    : {total_km(rows2):.3f} km
""")


if __name__ == "__main__":
    main()
