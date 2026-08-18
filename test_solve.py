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

    print("== bad profile / penalty are rejected ==")
    for bad in (["--profile", "nonsense"], ["--wrong-way-penalty", "0.5"]):
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
