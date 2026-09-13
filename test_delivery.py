#!/usr/bin/env python3
"""Offline regression tests for physical delivery sides across the workflow."""

import csv
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import delivery
import make_editor
import prepare_round
import round_history
import solve_route as solver
import sort_plan
from test_solve import read_route, run, write_simple


BASE = Path(__file__).parent
TMP = BASE / "_test_tmp" / "delivery"
NODES = {"A": (-37.845, 144.950), "B": (-37.845, 144.951),
         "C": (-37.844, 144.951)}


def read_edges(data):
    with open(data / "edges.csv", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames), list(reader)


def write_edges(data, fields, rows):
    with open(data / "edges.csv", "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def rejected(fn):
    try:
        fn()
    except ValueError:
        return
    raise AssertionError("invalid service coverage or configuration was accepted")


def main():
    if TMP.exists():
        shutil.rmtree(TMP)
    TMP.mkdir(parents=True)

    print("== one side keeps its identity when travel or traffic direction changes ==")
    data = TMP / "single"
    write_simple(data, NODES, [("only", "Test Street", "A", "B", 1)])
    for start, end, direction in (("A", "B", "forward"), ("B", "A", "reverse")):
        for traffic in ("left", "right"):
            out = TMP / (direction + traffic)
            run(data, out, "--start=" + ",".join(map(str, NODES[start])),
                "--end=" + ",".join(map(str, NODES[end])), "--traffic-side", traffic)
            rows = read_route(out)
            assert len(rows) == 1
            row = rows[0]
            assert row["delivery_side"] == "left" and row["side"] == "N", row
            assert row["travel_direction"] == direction, row
            assert row["travel_side"] == ("left" if direction == "forward" else "right")
            assert (row["counterflow"] == "yes") == (row["travel_side"] != traffic)
            runs = sort_plan.build_runs(rows, sort_plan.load_edges(data), NODES,
                                        {"only": [("1", "left"), ("2", "right")]})
            assert runs[0]["numbers"] == ["1"], runs

    print("== soft direction cost changes the first task, never its side ==")
    preference_data = TMP / "preference"
    write_simple(preference_data, NODES, [("both", "Test Street", "A", "B", 2)])
    for traffic in ("left", "right"):
        out = TMP / ("preferred_" + traffic)
        run(preference_data, out, "--start=" + ",".join(map(str, NODES["A"])),
            "--traffic-side", traffic, "--wrong-way-penalty", "12")
        rows = read_route(out)
        assert len(rows) == 2
        assert rows[0]["delivery_side"] == traffic, rows
        assert all(row["travel_side"] == traffic and not row["counterflow"] for row in rows)
        assert {row["delivery_side"] for row in rows} == {"left", "right"}

    print("== same-direction passes cover different explicit side tasks ==")
    edges = [dict(edge_id=eid, name=eid, u=u, v=v, service=2,
                  delivery="both", highway="residential", maxspeed=None,
                  oneway=True, length=100.0, wkt="")
             for eid, u, v in (("ab", "A", "B"), ("bc", "B", "C"), ("ca", "C", "A"))]
    F, R, D = solver.build_graphs(edges, solver.load_profile("edv")[1])
    tour = [(u, v, f"{eid}|p{p}") for p in (1, 2)
            for eid, u, v in (("ab", "A", "B"), ("bc", "B", "C"), ("ca", "C", "A"))]
    with patch.object(solver, "euler_tour", return_value=tour):
        segments = solver.traverse(F, R, D, NODES, None, "A",
                                   optimise_tour=False, search_iterations=0)
    assert delivery.validate_coverage(edges, segments) == 6
    ab = [s for s in segments if s["edge_id"] == "ab"]
    assert [s["travel_direction"] for s in ab] == ["forward", "forward"]
    assert {s["delivery_side"] for s in ab} == {"left", "right"}
    assert not any(s["against"] for s in segments)
    bad = [dict(s) for s in segments]
    bad[3]["delivery_side"] = "left"
    rejected(lambda: delivery.validate_coverage(edges, bad))
    rejected(lambda: delivery.validate_coverage(edges, segments[:-1]))
    rejected(lambda: delivery.validate_coverage(edges, segments + [dict(segments[0], edge_id="unexpected")]))

    print("== diagonal and curved geometry use the local reference tangent ==")
    diagonal = [(-37.845, 144.950), (-37.844, 144.951)]
    assert delivery.point_side(diagonal, (-37.84445, 144.95045))[1] == "left"
    assert delivery.point_side(list(reversed(diagonal)),
                               (-37.84445, 144.95045))[1] == "right"
    curve = [NODES["A"], NODES["B"], NODES["C"]]
    assert delivery.point_side(curve, (-37.8445, 144.9509))[1] == "left"
    curved_edge = dict(edge_id="bend", name="Bent Street", u="A", v="C",
                       service="2", delivery="both",
                       geometry_wkt="LINESTRING (144.951 -37.844, 144.951 -37.845, 144.950 -37.845)")
    attached, placed = sort_plan.attach_addresses(
        [dict(lat=-37.8445, lon=144.9509, number="1", street="Bent Street"),
         dict(lat=-37.8445, lon=144.9511, number="2", street="Bent Street")],
        {"bend": curved_edge}, NODES, 25)
    assert placed == 2 and set(attached["bend"]) == {("1", "left"), ("2", "right")}
    loop = curve + [(-37.844, 144.950), NODES["A"]]
    assert delivery.point_side(loop, (-37.8445, 144.9509))[1] == "left"
    assert delivery.point_side(loop, (-37.8445, 144.9511))[1] == "right"
    attached, placed = sort_plan.attach_addresses(
        [dict(lat=-37.8445, lon=144.951, number="ambiguous", street="Bent Street")],
        {"bend": curved_edge}, NODES, 25)
    assert placed == 0 and not attached, "a point on the reference has no known side"

    print("== non-service neighbours and the unselected side keep their addresses ==")
    single_edge = dict(edge_id="main", name="Test Street", u="A", v="B",
                       service="1", delivery="left", geometry_wkt="")
    nodes_with_neighbour = dict(NODES, D=(-37.845, 144.952))
    for service in ("0", "x"):
        neighbour = dict(edge_id="neighbour", name="Test Street", u="B", v="D",
                          service=service, delivery="none", geometry_wkt="")
        attached, placed = sort_plan.attach_addresses(
            [dict(lat=-37.84495, lon=144.9505, number="1", street="Test Street"),
             dict(lat=-37.84505, lon=144.9505, number="2", street="Test Street"),
             dict(lat=-37.84495, lon=144.9511, number="3", street="Test Street")],
            {"main": single_edge, "neighbour": neighbour}, nodes_with_neighbour, 25)
        assert placed == 1 and attached == {"main": [("1", "left")]}, attached

    print("== one-way ring can serve inner and outer without reversing travel ==")
    loop_edges = [dict(edge_id="ring", name="Loop Street", u="A", v="A", service=2,
                       delivery="both", highway="residential", maxspeed=None,
                       oneway=True, length=400.0,
                       wkt=prepare_round.to_wkt(loop))]
    F, R, D = solver.build_graphs(loop_edges, solver.load_profile("edv")[1])
    laps = solver.traverse(F, R, D, NODES, None, "A", search_iterations=0)
    assert delivery.validate_coverage(loop_edges, laps) == 2
    assert {s["side"] for s in laps} == {"inner", "outer"}
    assert all(s["travel_direction"] == "forward" and not s["against"] for s in laps)
    for lap in laps:
        assert solver.segment_coords(lap, NODES) == loop

    print("== legacy one-side data must be confirmed, never guessed ==")
    fields, rows = read_edges(data)
    fields.remove("delivery")
    for row in rows:
        row.pop("delivery")
    write_edges(data, fields, rows)
    result = subprocess.run([sys.executable, str(BASE / "solve_route.py"),
                             "--data", str(data), "--out", str(TMP / "pending")],
                            capture_output=True, text=True)
    assert result.returncode != 0 and "unspecified delivery" in result.stderr
    assert not (TMP / "pending" / "route.csv").exists()
    header, editable = make_editor.load(data)
    assert header == fields + ["delivery"]
    assert editable[0]["delivery"] == "unspecified"
    posted = [dict(e["row"], delivery="right") for e in editable]

    def csv_text(items):
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=header)
        writer.writeheader()
        writer.writerows(items)
        return out.getvalue()

    make_editor.save_csv(data, csv_text(posted))
    assert read_edges(data)[1][0]["delivery"] == "right"
    before = (data / "edges.csv").read_bytes()
    rejected(lambda: make_editor.save_csv(data, csv_text([dict(posted[0], u="B", v="A")])))
    assert (data / "edges.csv").read_bytes() == before
    rejected(lambda: delivery.delivery_mode(dict(service=1, delivery="both")))
    rejected(lambda: delivery.delivery_mode(dict(service=1, delivery="zigzag")))
    assert delivery.delivery_mode(dict(service=" ")) == "none"

    print("== splitting preserves the reference side in both children ==")
    split = subprocess.run([sys.executable, str(BASE / "split_edge.py"),
                            "--data", str(data), "--edge", "only",
                            "--at=-37.845,144.9505"], capture_output=True, text=True)
    assert split.returncode == 0, split.stderr
    _, children = read_edges(data)
    assert len(children) == 2 and all(r["delivery"] == "right" for r in children)

    print("== re-extraction keeps the physical side when u/v reverses ==")
    restore = TMP / "restore"
    snapshot = TMP / "snapshot"
    write_simple(restore, NODES, [("road", "Test Street", "A", "B", 1)])
    prepare_round.export(restore, snapshot)
    fields, rows = read_edges(restore)
    rows[0].update(u="B", v="A", service="0", delivery="none")
    write_edges(restore, fields, rows)
    prepare_round.apply(restore, snapshot)
    assert read_edges(restore)[1][0]["delivery"] == "right"
    rows[0].update(edge_id="replacement", service="0", delivery="none")
    write_edges(restore, fields, rows)
    prepare_round.apply(restore, snapshot)
    assert read_edges(restore)[1][0]["delivery"] == "right"

    print("== side-only edits are distinct history versions ==")
    hist = TMP / "history"
    summary = dict(mode="test", total_km=1.0, mandatory_km=0.1,
                    service_edges=1, deadhead_km=0.9)
    _, first = round_history.record(hist, restore, summary)
    fields, rows = read_edges(restore)
    rows[0]["delivery"] = "left"
    write_edges(restore, fields, rows)
    _, second = round_history.record(hist, restore, summary)
    assert first and second
    index = round_history.read_index(hist)
    assert len(index) == 2
    saved = round_history.read_service(hist / index[-1]["id"])
    assert saved["replacement"]["delivery"] == "left"

    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
