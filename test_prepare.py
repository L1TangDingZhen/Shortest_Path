#!/usr/bin/env python3
"""Offline tests for prepare_round.py (B1: reproducible rounds).

Scenario mirrors real use: annotate a synthetic round (service edits +
an edge split), --export the snapshot, simulate a re-extraction that
wipes everything, apply the snapshot, and assert the annotation is
back bit-for-bit -- then run the solver on the restored data.  Also:
new edges keep their default and are reported; stale overrides warn.

Run:  python test_prepare.py
"""

import csv
import shutil
import subprocess
import sys
from pathlib import Path

from test_solve import write_case, check_walk, read_route, run
from test_split import run_split, set_service

BASE = Path(__file__).parent
TMP = BASE / "_test_tmp" / "prepare"

PARENT = "n00-n01-0"


def run_prepare(data_dir, round_dir, *extra):
    res = subprocess.run(
        [sys.executable, str(BASE / "prepare_round.py"),
         "--data", str(data_dir), "--round", str(round_dir), *extra],
        capture_output=True, text=True)
    assert res.returncode == 0, res.stderr or res.stdout
    return res.stdout


def services(data_dir):
    with open(data_dir / "edges.csv", newline="",
              encoding="utf-8-sig") as f:
        return {r["edge_id"]: (r["service"] or "").strip()
                for r in csv.DictReader(f)}


def main():
    if TMP.exists():
        shutil.rmtree(TMP)
    rdir = TMP / "round"

    print("== annotate: edits + a split ==")
    d1 = TMP / "data"
    write_case(d1)
    res = run_split(d1, "-37.84498,144.9505")          # split A Street
    assert res.returncode == 0, res.stderr
    set_service(d1, PARENT + "#b", 0)                  # outer child
    set_service(d1, "n00-n10-0", 0)                    # drop an avenue
    set_service(d1, "n01-court-0", "x")                # ban the court
    annotated = services(d1)

    print("== export snapshot ==")
    out = run_prepare(d1, rdir, "--export")
    assert "1 split" in out, out
    with open(rdir / "service_overrides.csv", newline="",
              encoding="utf-8-sig") as f:
        ov = {r["edge_id"]: r["service"] for r in csv.DictReader(f)}
    assert ov == annotated, "overrides must snapshot every edge"
    with open(rdir / "splits.csv", newline="",
              encoding="utf-8-sig") as f:
        sp = list(csv.DictReader(f))
    assert len(sp) == 1 and sp[0]["edge_id"] == PARENT

    print("== simulate re-extraction, then apply ==")
    shutil.rmtree(d1)
    write_case(d1)                                     # wiped: defaults
    assert services(d1) != annotated
    out = run_prepare(d1, rdir)
    assert services(d1) == annotated, \
        "apply must restore the annotation exactly"
    assert (d1 / "edges.csv.bak").exists()

    print("== solver runs on the restored data ==")
    spec = {eid: (0 if s in ("0", "x") else int(s))
            for eid, s in annotated.items() if s != "x"}
    edges_by_id = {}
    with open(d1 / "edges.csv", newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            edges_by_id[r["edge_id"]] = (r["u"], r["v"])
    sol = TMP / "solved"
    run(d1, sol)
    check_walk(read_route(sol), edges_by_id, spec, closed=True)

    print("== an edge whose id changed is re-found by geometry ==")
    # a different --network-type splits ways at different junctions, so
    # ids vanish; here split_edge stands in for that re-splitting
    d2 = TMP / "regeom"
    rdir2 = TMP / "round2"
    write_case(d2)
    set_service(d2, "n00-n01-0", 1)          # annotate, then snapshot
    set_service(d2, "n10-n11-0", "x")
    run_prepare(d2, rdir2, "--export")
    shutil.rmtree(d2)
    write_case(d2)                            # "re-extraction": defaults
    for parent in ("n00-n01-0", "n10-n11-0"):
        res = run_split(d2, "-37.84498,144.9505" if parent.startswith("n00")
                        else "-37.84598,144.9505", "--edge", parent)
        assert res.returncode == 0, res.stderr
    out = run_prepare(d2, rdir2)
    assert "re-found by geometry" in out, out
    svc = services(d2)
    assert "n00-n01-0" not in svc, "parent should have been split away"
    assert svc["n00-n01-0#a"] == "1" and svc["n00-n01-0#b"] == "1", \
        f"both halves should inherit the annotation: {svc['n00-n01-0#a']}, " \
        f"{svc['n00-n01-0#b']}"
    assert svc["n10-n11-0#a"] == "x" and svc["n10-n11-0#b"] == "x"
    # an untouched neighbour must not be dragged in
    assert svc["n01-n02-0"] == "2", "geometry match leaked onto a neighbour"

    print("== new edges keep default; stale overrides warn ==")
    with open(d1 / "edges.csv", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        header = list(reader.fieldnames)
    with open(d1 / "edges.csv", "a", newline="",
              encoding="utf-8-sig") as f:
        csv.DictWriter(f, fieldnames=header).writerow(
            {h: {"edge_id": "corridor-0", "name": "New Corridor",
                 "highway": "residential", "length_m": "50",
                 "service": "0", "u": "n00", "v": "n20",
                 "geometry_wkt": ""}.get(h, "") for h in header})
    with open(rdir / "service_overrides.csv", "a", newline="",
              encoding="utf-8-sig") as f:
        csv.writer(f).writerow(["ghost-99", "2", "Ghost Street"])
    out = run_prepare(d1, rdir)
    assert "1 edges had no override" in out, out
    assert "could not be placed" in out and "Ghost Street" in out, out
    assert services(d1)["corridor-0"] == "0", \
        "new edge must keep its extraction default"

    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
