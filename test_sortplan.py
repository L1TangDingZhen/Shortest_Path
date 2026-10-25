#!/usr/bin/env python3
"""Offline tests for sort_plan.py (no internet, synthetic addresses).

A street delivered on both sides must split its numbers between the two
passes using their explicit physical side. A street delivered once takes
only its chosen side, regardless of travel direction. Addresses on another street must never be attached
just because they are close.

Run:  python test_sortplan.py
"""

import csv
import json
import shutil
import subprocess
import sys
from pathlib import Path
from html.parser import HTMLParser

import sort_plan

BASE = Path(__file__).parent
TMP = BASE / "_test_tmp" / "sortplan"


class SequenceReader(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sequences = []
        self.active = False

    def handle_starttag(self, tag, attrs):
        if tag == "span" and dict(attrs).get("class") in ("nums", "none"):
            self.active = True
            self.sequences.append("")

    def handle_data(self, data):
        if self.active:
            self.sequences[-1] += data

    def handle_endtag(self, tag):
        if tag == "span":
            self.active = False


def assert_exports(result, expected):
    with (result / "sort_plan.csv").open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert [json.loads(row["house_sequence"]) for row in rows] == expected, rows
    assert [int(row["count"]) for row in rows] == [len(ns) for ns in expected]
    assert [row["houses"] for row in rows] == [sort_plan.describe(ns) for ns in expected]
    reader = SequenceReader()
    reader.feed((result / "sort_plan.html").read_text(encoding="utf-8"))
    assert reader.sequences == [" \u2192 ".join(ns) if ns else "-" for ns in expected]
    with (result / "sort_addresses.csv").open(newline="", encoding="utf-8-sig") as f:
        addresses = list(csv.DictReader(f))
    flattened = [(i, n) for i, ns in enumerate(expected) for n in ns]
    assert len(addresses) == len(flattened)
    for seq, (address, (i, number)) in enumerate(zip(addresses, flattened), 1):
        assert address == dict(delivery_seq=str(seq), run_seq=rows[i]["seq"],
                               street=rows[i]["street"], housenumber=number,
                               delivery_side=rows[i]["delivery_side"],
                               side=rows[i]["side"], direction=rows[i]["direction"])


def test_special_numbers(data, result):
    print("== exports preserve gaps, nonmonotonic numbers, suffixes, units and repeats ==")
    north = ["20", "2/16", "18A", "18A", "<12&>", 'Unit "A", 8']
    south = ["4", "10B", "2/8", "2"]
    nodes = sort_plan.load_nodes(data)
    lat, west = nodes["W"]
    _, east = nodes["E"]
    with (data / "addresses.csv").open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["lat", "lon", "housenumber", "street"])
        for offset, numbers in ((0.0001, north), (-0.0001, south)):
            # Deliberately shuffled input: position, not input/numeric order, wins.
            for i in reversed(range(len(numbers))):
                writer.writerow([lat + offset, west + (east - west) * (i + 1) / (len(numbers) + 1),
                                 numbers[i], "Main Street"])
    def run():
        res = subprocess.run([sys.executable, str(BASE / "sort_plan.py"),
                              "--data", str(data), "--result", str(result)],
                             capture_output=True, text=True)
        assert res.returncode == 0, res.stdout + res.stderr
    run()
    assert_exports(result, [north, south[::-1]])
    sheet = (result / "sort_plan.html").read_text(encoding="utf-8")
    assert "&lt;12&amp;&gt;" in sheet and "<12&>" not in sheet

    print("== empty and single-address runs export without inferred numbers ==")
    with (data / "addresses.csv").open(newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header, single = next(reader), next(reader)
    with (data / "addresses.csv").open("w", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerows([header, single])
    run()
    assert_exports(result, [[single[2]], []])
    with (data / "addresses.csv").open("w", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerow(header)
    run()
    assert_exports(result, [[], []])


def test_merged_runs(data, result):
    print("== merged runs retain traversal order; deadhead separates sequences ==")
    base = sort_plan.load_route(result)[0]
    template = sort_plan.load_edges(data)["main"]
    edges = {eid: dict(template, edge_id=eid, service="1", delivery="left")
             for eid in ("first", "second", "third")}
    rows = [dict(base, edge_id=eid, **{"pass": "1/1"}) for eid in edges]
    route = rows[:2] + [dict(base, type="deadhead")] + rows[2:]
    attached = {"first": [("20", "left"), ("18A", "left")],
                "second": [("2/16", "left"), ("20", "left")],
                "third": [("10", "left")]}
    runs = sort_plan.build_runs(route, edges, sort_plan.load_nodes(data), attached)
    assert [r["numbers"] for r in runs] == [["20", "18A", "2/16", "20"], ["10"]]
    sheet = result / "merged.html"
    sort_plan.write_sheet(runs, sheet, "Synthetic merged runs")
    reader = SequenceReader()
    reader.feed(sheet.read_text(encoding="utf-8"))
    assert reader.sequences == ["20 \u2192 18A \u2192 2/16 \u2192 20", "10"]


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
        w.writerow(["seq", "type", "street", "pass", "side", "direction",
                    "from_cross", "to_cross", "length_m", "cum_km",
                    "against_oneway", "edge_id", "delivery_side", "travel_direction"])
        # traffic keeps left: heading E you deliver the north kerb
        w.writerow([1, "service", "Main Street", "1/2", "N", "E",
                    "(dead end)", "(dead end)", 270, 0.27, "", "main", "left", "forward"])
        w.writerow([2, "service", "Main Street", "2/2", "S", "W",
                    "(dead end)", "(dead end)", 270, 0.54, "", "main", "right", "reverse"])


def main():
    if TMP.exists():
        shutil.rmtree(TMP)
    data, result = TMP / "data", TMP / "result"
    write_case(data)
    write_route(result)

    print("== range summaries count entries, not distinct house numbers ==")
    assert sort_plan.describe(["3", "7", "3"]) == "odd 3-7 (3)"
    assert sort_plan.describe(["3", "3"]) == "3"

    print("== runs are built with their house numbers ==")
    res = subprocess.run(
        [sys.executable, str(BASE / "sort_plan.py"), "--data", str(data),
         "--result", str(result)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    with open(result / "sort_plan.csv", newline="",
              encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    # The two kerbs stay separately labelled in the sort sheet.
    assert len(rows) == 2, rows
    assert rows[0]["street"] == "Main Street"
    assert [int(r["count"]) for r in rows] == [3, 3], rows
    assert_exports(result, [["1", "3", "5"], ["6", "4", "2"]])

    print("== the decoy on another street is never attached ==")
    assert "99" not in rows[0]["houses"], rows[0]["houses"]

    print("== each pass takes its explicitly required side ==")
    # split the two passes apart to see them individually
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

    # Direction is explicit, not inferred from the delivered kerb:
    # out along W->E, back along E->W
    route = sort_plan.load_route(result)
    assert [r["side"] for r in route] == ["N", "S"], \
        [r["side"] for r in route]
    assert sort_plan.traversal_directions(route, edges, nodes) == \
        [True, False]
    # so the two passes together cover both sides exactly once
    runs = sort_plan.build_runs(route, edges, nodes, attached)
    assert runs[0]["numbers"] == ["1", "3", "5"]
    assert runs[1]["numbers"] == ["6", "4", "2"]
    describe = sort_plan.describe(runs[0]["numbers"])
    assert describe.startswith("odd 1-5"), describe

    print("== the sheet is written and readable ==")
    sheet = (result / "sort_plan.html").read_text(encoding="utf-8")
    assert "Main Street" in sheet and "sort plan" in sheet.lower()

    test_merged_runs(data, result)
    test_special_numbers(data, result)

    print("== a missing addresses.csv fails with advice ==")
    (data / "addresses.csv").unlink()
    res = subprocess.run(
        [sys.executable, str(BASE / "sort_plan.py"), "--data", str(data),
         "--result", str(result)], capture_output=True, text=True)
    assert res.returncode != 0 and "--addresses" in res.stdout + res.stderr

    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
