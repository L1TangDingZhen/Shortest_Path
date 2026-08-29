#!/usr/bin/env python3
"""Offline tests for sort_plan.py (no internet, synthetic addresses).

A street delivered on both sides must split its numbers between the two
passes -- the side you are riding on, which under left-hand traffic is
the left of the direction travelled. A street delivered once takes them
all. Addresses belonging to a different street must never be attached
just because they are close.

Run:  python test_sortplan.py
"""

import csv
import shutil
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).parent
TMP = BASE / "_test_tmp" / "sortplan"


def write_case(data_dir: Path):
    """One street east-west, houses on both sides, plus a decoy address
    15 m away that belongs to another street."""
    data_dir.mkdir(parents=True, exist_ok=True)
    with open(data_dir / "nodes.csv", "w", newline="",
              encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["node_id", "lat", "lon", "control", "control_group"])
        w.writerow(["W", -37.8450, 144.9500, "", ""])
        w.writerow(["E", -37.8450, 144.9530, "", ""])
    with open(data_dir / "edges.csv", "w", newline="",
              encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["edge_id", "name", "highway", "oneway", "maxspeed_kmh",
                    "length_m", "service", "note", "u", "v",
                    "geometry_wkt"])
        w.writerow(["main", "Main Street", "residential", "False", "",
                    270, 2, "", "W", "E", ""])
    with open(data_dir / "addresses.csv", "w", newline="",
              encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["lat", "lon", "housenumber", "street"])
        # north of the line (left of W->E): odds. south: evens.
        for i, lon in enumerate((144.9505, 144.9515, 144.9525)):
            w.writerow([-37.84490, lon, str(1 + 2 * i), "Main Street"])
            w.writerow([-37.84510, lon, str(2 + 2 * i), "Main Street"])
        w.writerow([-37.84487, 144.9510, "99", "Other Street"])


def write_route(result_dir: Path):
    """Both passes of the one street, ridden out and back."""
    result_dir.mkdir(parents=True, exist_ok=True)
    with open(result_dir / "route.csv", "w", newline="",
              encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["seq", "type", "street", "pass", "direction",
                    "from_cross", "to_cross", "length_m", "cum_km",
                    "against_oneway", "edge_id"])
        w.writerow([1, "service", "Main Street", "1/2", "E", "(dead end)",
                    "(dead end)", 270, 0.27, "", "main"])
        w.writerow([2, "service", "Main Street", "2/2", "W", "(dead end)",
                    "(dead end)", 270, 0.54, "", "main"])


def main():
    if TMP.exists():
        shutil.rmtree(TMP)
    data, result = TMP / "data", TMP / "result"
    write_case(data)
    write_route(result)

    print("== runs are built with their house numbers ==")
    res = subprocess.run(
        [sys.executable, str(BASE / "sort_plan.py"), "--data", str(data),
         "--result", str(result)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    with open(result / "sort_plan.csv", newline="",
              encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    # consecutive runs on one street merge: the sorter wants the street
    assert len(rows) == 1, rows
    assert rows[0]["street"] == "Main Street"
    assert int(rows[0]["count"]) == 6, rows[0]

    print("== the decoy on another street is never attached ==")
    assert "99" not in rows[0]["houses"], rows[0]["houses"]

    print("== each pass takes the side it rides on ==")
    # split the two passes apart to see them individually
    import sort_plan
    edges = sort_plan.load_edges(data)
    nodes = sort_plan.load_nodes(data)
    addresses = sort_plan.load_addresses(data)
    attached, placed = sort_plan.attach_addresses(addresses, edges, nodes,
                                                  sort_plan.TOLERANCE_M)
    assert placed == 6, f"only Main Street's six belong: {placed}"
    sides = {side for _, side in attached["main"]}
    assert sides == {"left", "right"}, sides
    left = sorted(n for n, side in attached["main"] if side == "left")
    assert left == ["1", "3", "5"], left      # north of W->E is left

    # direction comes from chaining the whole walk, not from one row:
    # out along W->E, back along E->W
    route = sort_plan.load_route(result)
    assert sort_plan.traversal_directions(route, edges) == [True, False]
    # so the two passes together cover both sides exactly once
    runs = sort_plan.build_runs(route, edges, nodes, attached)
    assert sorted(runs[0]["numbers"], key=int) == \
        ["1", "2", "3", "4", "5", "6"], runs[0]["numbers"]
    describe = sort_plan.describe(runs[0]["numbers"])
    assert describe.startswith("1-6"), describe

    print("== the sheet is written and readable ==")
    sheet = (result / "sort_plan.html").read_text(encoding="utf-8")
    assert "Main Street" in sheet and "sort plan" in sheet.lower()

    print("== a missing addresses.csv fails with advice ==")
    (data / "addresses.csv").unlink()
    res = subprocess.run(
        [sys.executable, str(BASE / "sort_plan.py"), "--data", str(data),
         "--result", str(result)], capture_output=True, text=True)
    assert res.returncode != 0 and "--addresses" in res.stdout + res.stderr

    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
