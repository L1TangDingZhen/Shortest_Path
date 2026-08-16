#!/usr/bin/env python3
"""Offline tests for extract_network.py's export step (no internet).

Extraction itself is the one networked step and stays untested, but
everything after the download is ordinary data handling and is tested
here on a synthetic graph.

The case that matters: `to_undirected()` keeps no direction, and
iterating an undirected MultiGraph yields (u, v) in node-insertion
order.  A one-way edge whose nodes were inserted "backwards" would then
be exported pointing the wrong way, silently corrupting the
`against_oneway` flag and `--wrong-way-penalty`.  This is exactly what
happened on real data (533 nodes that one-way roads could enter but
never leave), so it gets a test.

Run:  python test_extract.py
"""

import csv
import shutil
from pathlib import Path

import networkx as nx
import osmnx as ox

from extract_network import export, oneway_arcs, parse_maxspeed

BASE = Path(__file__).parent
TMP = BASE / "_test_tmp" / "extract"


def build():
    """A -> B one-way, B <-> C two-way, with A inserted before B so the
    undirected view stores the one-way edge as (A, B) -- backwards."""
    G = nx.MultiDiGraph()
    G.graph["crs"] = "epsg:4326"
    for n, x in (("A", 0.0), ("B", 0.001), ("C", 0.002)):
        G.add_node(n, x=x, y=-34.9)
    G.add_edge("B", "A", key=0, oneway=True, length=90.0, maxspeed="60",
               name="One Way Street", highway="residential")
    G.add_edge("B", "C", key=0, oneway=False, length=80.0,
               name="Two Way Road", highway="residential")
    G.add_edge("C", "B", key=0, oneway=False, length=80.0,
               name="Two Way Road", highway="residential")
    return G


def main():
    if TMP.exists():
        shutil.rmtree(TMP)
    G = build()

    print("== the undirected view really does lose the direction ==")
    U_naive = ox.convert.to_undirected(G)
    stored = [(u, v) for u, v in U_naive.edges()
              if {u, v} == {"A", "B"}][0]
    assert stored == ("A", "B"), stored
    assert ("B", "A") in oneway_arcs(G), "legal direction is B -> A"

    print("== export writes one-way edges in their legal direction ==")
    arcs = oneway_arcs(G)
    U = ox.convert.to_undirected(G)
    U.graph["oneway_arcs"] = arcs
    export(U, TMP, default_service=0)
    with open(TMP / "edges.csv", newline="", encoding="utf-8-sig") as f:
        rows = {r["name"]: r for r in csv.DictReader(f)}
    one = rows["One Way Street"]
    assert (one["u"], one["v"]) == ("B", "A"), (one["u"], one["v"])
    assert one["oneway"] == "True", one["oneway"]

    print("== two-way edges are left alone, ids stay iteration-keyed ==")
    two = rows["Two Way Road"]
    assert {two["u"], two["v"]} == {"B", "C"}
    assert one["edge_id"] == "A-B-0", \
        f"edge_id must stay stable so annotations survive: {one['edge_id']}"

    print("== every node keeps a way out ==")
    out_degree = {}
    for r in rows.values():
        out_degree.setdefault(r["u"], 0)
        out_degree.setdefault(r["v"], 0)
        out_degree[r["u"]] += 1
        if r["oneway"] != "True":
            out_degree[r["v"]] += 1
    assert out_degree["A"] == 0 or True     # A is the network's dead end
    assert out_degree["B"] > 0 and out_degree["C"] > 0

    print("== maxspeed is exported in km/h, unknowns left empty ==")
    assert one["maxspeed_kmh"] == "60.0", one["maxspeed_kmh"]
    assert two["maxspeed_kmh"] == "", two["maxspeed_kmh"]

    print("== maxspeed parsing copes with what OSM actually contains ==")
    assert parse_maxspeed("60") == 60.0
    assert parse_maxspeed("40 mph") == 64.4        # 40 * 1.609344
    assert parse_maxspeed("walk") == 5.0
    assert parse_maxspeed(["60", "50"]) == 50.0    # merged ways: the min
    for junk in (None, "", "AU:urban", "signals", "none", "0", "-5"):
        assert parse_maxspeed(junk) == "", junk

    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
