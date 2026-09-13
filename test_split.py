#!/usr/bin/env python3
"""Offline tests for split_edge.py (SPEC-1 acceptance tests).

Reuses the synthetic grid from test_solve.  Asserts:

  1. mid-edge split: parent row gone, two children present, child
     lengths sum to the parent within 1%, node added, service inherited
  2. solver invariants still hold on the split data after setting one
     child to 0 (full solve; reuses check_walk)
  3. snap case: a point ~3 m from an intersection changes nothing and
     exits 0 with the "coincides with intersection" message
  4. mis-click guard: a point ~100+ m from any edge exits non-zero
  5. splitting a child (#a) works, including via --edge

Run:  python test_split.py
"""

import csv
import shutil
import subprocess
import sys
from pathlib import Path

from test_solve import write_case, check_walk, read_route, run

BASE = Path(__file__).parent
TMP = BASE / "_test_tmp" / "split"

PARENT = "n00-n01-0"      # A Street, n00 (-37.845, 144.950) -> n01 (+0.001 lon)


def run_split(data_dir, at, *extra):
    return subprocess.run(
        [sys.executable, str(BASE / "split_edge.py"),
         "--data", str(data_dir), f"--at={at}", *extra],
        capture_output=True, text=True)


def read_edges(data_dir):
    with open(data_dir / "edges.csv", newline="",
              encoding="utf-8-sig") as f:
        return {r["edge_id"]: r for r in csv.DictReader(f)}


def read_nodes(data_dir):
    with open(data_dir / "nodes.csv", newline="",
              encoding="utf-8-sig") as f:
        return {r["node_id"]: (float(r["lat"]), float(r["lon"]))
                for r in csv.DictReader(f)}


def set_service(data_dir, edge_id, value):
    with open(data_dir / "edges.csv", newline="",
              encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        header = list(reader.fieldnames)
        rows = list(reader)
    for r in rows:
        if r["edge_id"] == edge_id:
            r["service"] = str(value)
            if "delivery" in header:
                r["delivery"] = {"0": "none", "1": "left", "2": "both", "x": "none"}.get(str(value), "")
    with open(data_dir / "edges.csv", "w", newline="",
              encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        w.writerows(rows)


def main():
    if TMP.exists():
        shutil.rmtree(TMP)

    print("== 1. mid-edge split ==")
    d1 = TMP / "mid"
    write_case(d1)
    parent_len = float(read_edges(d1)[PARENT]["length_m"])
    res = run_split(d1, "-37.84498,144.9505")   # ~44 m along, 2 m off-line
    assert res.returncode == 0, res.stderr
    edges = read_edges(d1)
    assert PARENT not in edges, "parent row must be gone"
    a, b = edges[PARENT + "#a"], edges[PARENT + "#b"]
    child_sum = float(a["length_m"]) + float(b["length_m"])
    assert abs(child_sum - parent_len) <= 0.01 * parent_len, \
        f"child lengths {child_sum} vs parent {parent_len}"
    nodes = read_nodes(d1)
    assert "s1" in nodes, "synthetic node s1 missing"
    assert a["u"] == "n00" and a["v"] == "s1"
    assert b["u"] == "s1" and b["v"] == "n01"
    assert a["service"] == b["service"] == "2", "service must be inherited"
    assert "split from" in a["note"] and "split from" in b["note"]

    print("== 5. splitting a child composes (via --edge) ==")
    res = run_split(d1, "-37.84499,144.95022", "--edge", PARENT + "#a")
    assert res.returncode == 0, res.stderr
    edges = read_edges(d1)
    assert PARENT + "#a" not in edges
    assert PARENT + "#a#a" in edges and PARENT + "#a#b" in edges
    assert "s2" in read_nodes(d1), "second synthetic node missing"

    print("== 2. solver invariants hold on split data ==")
    d2 = TMP / "solve"
    spec = write_case(d2)
    res = run_split(d2, "-37.84498,144.9505")
    assert res.returncode == 0, res.stderr
    set_service(d2, PARENT + "#b", 0)      # boundary: only #a is mine
    spec.pop(PARENT)
    spec[PARENT + "#a"] = 2
    spec[PARENT + "#b"] = 0
    edges_by_id = {eid: (r["u"], r["v"])
                   for eid, r in read_edges(d2).items()}
    out = TMP / "solve_out"
    run(d2, out)
    rows = read_route(out)
    check_walk(rows, edges_by_id, spec, closed=True)

    print("== 3. snap to intersection changes nothing ==")
    d3 = TMP / "snap"
    write_case(d3)
    before_e = (d3 / "edges.csv").read_bytes()
    before_n = (d3 / "nodes.csv").read_bytes()
    res = run_split(d3, "-37.845,144.950034")   # ~3 m east of n00
    assert res.returncode == 0, res.stderr
    assert "coincides with intersection" in res.stdout
    assert (d3 / "edges.csv").read_bytes() == before_e
    assert (d3 / "nodes.csv").read_bytes() == before_n

    print("== 4. mis-click aborts ==")
    d4 = TMP / "misclick"
    write_case(d4)
    before_e = (d4 / "edges.csv").read_bytes()
    res = run_split(d4, "-37.86,144.9505")      # ~1.6 km south of the grid
    assert res.returncode != 0, "mis-click must exit non-zero"
    assert "mis-click" in (res.stdout + res.stderr)
    assert (d4 / "edges.csv").read_bytes() == before_e

    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
