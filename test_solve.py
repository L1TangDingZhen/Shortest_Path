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
from collections import Counter
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
        # edge_id,   name,        highway,       oneway, len, service, u, v
        ("svc", "Long Street", "residential", "False", 300, 1, "A", "B"),
        ("foot", "The Path", "footway", "False", 100, 0, "A", "B"),
        ("road", "Back Road", "residential", "True", 130, 0, "B", "A"),
    ]
    with open(data_dir / "edges.csv", "w", newline="",
              encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["edge_id", "name", "highway", "oneway", "length_m",
                    "service", "note", "u", "v", "geometry_wkt"])
        for eid, nm, hw, ow, ln, sv, u, v in rows:
            w.writerow([eid, nm, hw, ow, ln, sv, "", u, v, ""])
    return {eid: sv for eid, _, _, _, _, sv, _, _ in rows}


def deadhead_ids(rows):
    return [r["edge_id"] for r in rows if r["type"] == "deadhead"]


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
    # 2. continuity: consecutive segments share a node.  route.csv does
    #    not carry u/v, so verify via edge endpoints chaining.
    ends = [edges_by_id[r["edge_id"]] for r in rows]
    cur = None
    for i, (u, v) in enumerate(ends):
        if cur is None:
            nxt = set(ends[i + 1]) if i + 1 < len(ends) else set()
            cur = v if v in nxt or u not in nxt else u
            first = u if cur == v else v
            continue
        assert cur in (u, v), f"walk breaks at segment {i + 1}"
        cur = v if cur == u else u
    if closed:
        assert cur == first, "circuit does not close"
    return cur, first


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

    print("== cost profile steers deadhead off the footpath ==")
    dc = TMP / "cost"
    cspec = write_cost_case(dc)
    cby = {"svc": ("A", "B"), "foot": ("A", "B"), "road": ("B", "A")}

    out_d = TMP / "cost_distance"
    run(dc, out_d)                       # default: every metre equal
    rows_d = read_route(out_d)
    check_walk(rows_d, cby, cspec, closed=True)
    assert deadhead_ids(rows_d) == ["foot"], deadhead_ids(rows_d)
    assert abs(total_km(rows_d) - 0.400) < 1e-6, total_km(rows_d)

    out_e = TMP / "cost_edv"
    run(dc, out_e, "--profile", "edv")   # a metre of footpath costs more
    rows_e = read_route(out_e)
    check_walk(rows_e, cby, cspec, closed=True)
    assert deadhead_ids(rows_e) == ["road"], deadhead_ids(rows_e)
    assert total_km(rows_e) > total_km(rows_d), \
        "the cheaper route may be longer in metres -- that is the point"

    print("== against_oneway is reported ==")
    assert "against_oneway" in rows_e[0], list(rows_e[0])
    flags = {r["edge_id"]: r["against_oneway"] for r in rows_e}
    assert flags["road"] == "yes", flags   # ridden B->A backwards
    assert flags["svc"] == "", flags       # two-way street
    assert all(r["against_oneway"] == "" for r in rows_d), \
        "the footway route has nothing to flag"

    print("== wrong-way penalty avoids the one-way ==")
    out_w = TMP / "cost_wrong"
    run(dc, out_w, "--profile", "edv", "--wrong-way-penalty", "3")
    rows_w = read_route(out_w)
    check_walk(rows_w, cby, cspec, closed=True)
    assert deadhead_ids(rows_w) == ["foot"], deadhead_ids(rows_w)
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
