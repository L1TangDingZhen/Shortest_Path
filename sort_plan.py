#!/usr/bin/env python3
"""Turn a solved route into a sort plan you can work from (B6).

`route.csv` says which street to ride and in which direction. What a
round actually needs is the house numbers each pass serves, in the
order the mail must be bundled -- that is the difference between a
route and a sort plan.

    python sort_plan.py --data data --result result

Reads route.csv, edges.csv, nodes.csv and addresses.csv (fetch those
with `extract_network.py --addresses`) and writes:

    result/sort_plan.csv    one row per delivery run, with its numbers
    result/sort_plan.html   the same, printable and readable on a phone

How a number is attached to a pass:

  * an address belongs to the nearest edge that carries its street
    name, within --tolerance metres -- the name is what makes this
    reliable, since a point 15 m away on the next street over would
    otherwise win;
  * for a street delivered on both sides, the two passes split by side.
    Traffic keeps left, so a pass serves what lies to the LEFT of the
    direction it is ridden. That is the footpath you are on;
  * for a street delivered once, the pass serves everything on it --
    one pass, both sides, however you choose to zigzag;
  * consecutive runs on the same street are merged, because the sorter
    cares about the street, not the segment.

Coverage is whatever OSM has: the plan reports how many numbers it
found, and a run with none simply shows a dash.
"""

import argparse
import csv
import html
import math
import re
import sys
from collections import defaultdict
from pathlib import Path

from solve_route import kerb_side, segment_coords

TOLERANCE_M = 25.0
MLAT = 110540.0


def load_route(result_dir: Path):
    with open(result_dir / "route.csv", newline="",
              encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def load_edges(data_dir: Path):
    with open(data_dir / "edges.csv", newline="",
              encoding="utf-8-sig") as f:
        return {r["edge_id"]: r for r in csv.DictReader(f)}


def load_nodes(data_dir: Path):
    with open(data_dir / "nodes.csv", newline="",
              encoding="utf-8-sig") as f:
        return {r["node_id"]: (float(r["lat"]), float(r["lon"]))
                for r in csv.DictReader(f)}


def load_addresses(data_dir: Path):
    path = data_dir / "addresses.csv"
    if not path.exists():
        sys.exit(f"{path} not found -- fetch it with:\n"
                 f"    python extract_network.py --polygon … "
                 f"--addresses --out {data_dir}")
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [{"lat": float(r["lat"]), "lon": float(r["lon"]),
                 "number": r["housenumber"], "street": r["street"]}
                for r in csv.DictReader(f)]


def norm_street(name):
    return re.sub(r"\s+", " ", (name or "")).strip().lower()


def house_sort_key(number):
    """'12A' sorts after '12' and before '14'; '1/5' by its last part."""
    tail = number.split("/")[-1]
    m = re.match(r"(\d+)(.*)", tail.strip())
    return (int(m.group(1)), m.group(2)) if m else (10 ** 9, tail)


def house_parity(number):
    m = re.match(r"(\d+)", number.split("/")[-1].strip())
    return int(m.group(1)) % 2 if m else None


def describe(numbers):
    """A run's numbers as a range a person can read off a bundle."""
    if not numbers:
        return "-"
    ordered = sorted(set(numbers), key=house_sort_key)
    if len(ordered) == 1:
        return ordered[0]
    sides = {house_parity(n) for n in ordered}
    side = ("even " if sides == {0} else "odd " if sides == {1} else "")
    return f"{side}{ordered[0]}-{ordered[-1]} ({len(ordered)})"


def point_to_segment(p, a, b, mlon):
    """(distance in metres, which side) of p relative to a->b."""
    ax, ay = (a[1] - p[1]) * mlon, (a[0] - p[0]) * MLAT
    bx, by = (b[1] - p[1]) * mlon, (b[0] - p[0]) * MLAT
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0,
                                              -(ax * dx + ay * dy) / length2))
    near = math.hypot(ax + t * dx, ay + t * dy)
    cross = dx * (-ay) - dy * (-ax)      # >0: p lies left of a->b
    return near, ("left" if cross > 0 else "right")


def attach_addresses(addresses, edges, nodes, tolerance):
    """{edge_id: [(number, side relative to u->v)]}"""
    by_street = defaultdict(list)
    for e in edges.values():
        by_street[norm_street(e["name"])].append(e)
    if not addresses:
        return {}, 0
    mlon = 111320.0 * math.cos(math.radians(addresses[0]["lat"]))

    attached, placed = defaultdict(list), 0
    for a in addresses:
        best = None
        for e in by_street.get(norm_street(a["street"]), ()):
            try:
                u, v = nodes[e["u"]], nodes[e["v"]]
            except KeyError:
                continue
            distance, side = point_to_segment((a["lat"], a["lon"]), u, v,
                                              mlon)
            if distance <= tolerance and (best is None or distance < best[0]):
                best = (distance, e["edge_id"], side)
        if best:
            attached[best[1]].append((a["number"], best[2]))
            placed += 1
    return attached, placed


def traversal_directions(route, edges, nodes):
    """True where a row was ridden u->v.

    route.csv names the kerb each pass delivered, and the kerb is what
    fixes the direction: riding u->v where traffic keeps left serves
    the side 90 degrees to its left. Reading it per row beats chaining
    the whole walk, which had to guess the first row's direction and
    fell back to calling every pass u->v when the chain broke -- which
    put a whole round's mail on one side of every street."""
    forward = []
    for r in route:
        e = edges[r["edge_id"]]
        uv = kerb_side(segment_coords({"wkt": e.get("geometry_wkt") or "",
                                       "u": e["u"], "v": e["v"]}, nodes))
        forward.append(not r.get("side") or r["side"] == uv)
    return forward


def build_runs(route, edges, nodes, attached):
    """One row per continuous delivery run, with the numbers it serves."""
    runs = []
    forward = traversal_directions(route, edges, nodes)
    for row, travelled_uv in zip(route, forward):
        if row["type"] != "service":
            continue
        edge = edges[row["edge_id"]]
        passes = int(edge["service"]) if edge["service"].isdigit() else 1
        # traffic keeps left, so a pass serves the side it is riding on:
        # left of u->v, which is right of v->u
        wanted = "left" if travelled_uv else "right"
        numbers = [n for n, side in attached.get(row["edge_id"], ())
                   if passes == 1 or side == wanted]
        if runs and runs[-1]["street"] == row["street"]:
            runs[-1]["numbers"] += numbers
            runs[-1]["metres"] += float(row["length_m"])
            runs[-1]["to_cross"] = row["to_cross"]
            runs[-1]["cum_km"] = row["cum_km"]
        else:
            runs.append({"seq": len(runs) + 1, "street": row["street"],
                         "pass": row["pass"], "direction": row["direction"],
                         "from_cross": row["from_cross"],
                         "to_cross": row["to_cross"],
                         "metres": float(row["length_m"]),
                         "cum_km": row["cum_km"], "numbers": list(numbers)})
    return runs


SHEET = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>sort plan</title>
<style>
 body {{ font: 15px/1.45 system-ui, sans-serif; margin: 0 auto;
        max-width: 760px; padding: 12px; color: #111; }}
 h1 {{ font-size: 18px; margin: 0 0 2px; }}
 .sub {{ color: #666; font-size: 13px; margin-bottom: 10px; }}
 ol {{ list-style: none; padding: 0; margin: 0; counter-reset: run; }}
 li {{ border-top: 1px solid #ddd; padding: 7px 0 7px 34px;
       position: relative; }}
 li::before {{ counter-increment: run; content: counter(run);
       position: absolute; left: 0; top: 7px; color: #888;
       font-variant-numeric: tabular-nums; }}
 .street {{ font-weight: 600; }}
 .nums {{ color: #1a5e20; font-weight: 600; }}
 .none {{ color: #999; }}
 .meta {{ color: #666; font-size: 13px; }}
 @media print {{ li {{ page-break-inside: avoid; }} .sub {{ color: #000; }} }}
</style></head><body>
<h1>Sort plan</h1>
<div class="sub">{summary}</div>
<ol>{rows}</ol>
</body></html>
"""


def write_sheet(runs, path, summary):
    rows = []
    for r in runs:
        numbers = describe(r["numbers"])
        rows.append(
            "<li><span class='street'>{street}</span> "
            "<span class='meta'>pass {p} &middot; {d} &middot; "
            "{m:.0f} m &middot; {km} km</span><br>"
            "<span class='{cls}'>{nums}</span> "
            "<span class='meta'>{a} &rarr; {b}</span></li>".format(
                street=html.escape(r["street"]), p=html.escape(r["pass"]),
                d=html.escape(r["direction"]), m=r["metres"],
                km=r["cum_km"], nums=html.escape(numbers),
                cls="nums" if r["numbers"] else "none",
                a=html.escape(r["from_cross"]),
                b=html.escape(r["to_cross"])))
    path.write_text(SHEET.format(summary=html.escape(summary),
                                 rows="\n".join(rows)), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", default="data")
    ap.add_argument("--result", default="result")
    ap.add_argument("--tolerance", type=float, default=TOLERANCE_M,
                    help="how far an address may sit from its street "
                         f"(default {TOLERANCE_M:g} m)")
    args = ap.parse_args()
    data_dir, result_dir = Path(args.data), Path(args.result)

    route = load_route(result_dir)
    edges = load_edges(data_dir)
    nodes = load_nodes(data_dir)
    addresses = load_addresses(data_dir)
    attached, placed = attach_addresses(addresses, edges, nodes,
                                        args.tolerance)
    runs = build_runs(route, edges, nodes, attached)

    with_numbers = sum(1 for r in runs if r["numbers"])
    total = sum(len(r["numbers"]) for r in runs)
    summary = (f"{len(runs)} delivery runs, {total} house numbers on "
               f"{with_numbers} of them. {placed} of {len(addresses)} "
               f"addresses in the area sit on a street of this round.")

    csv_path = result_dir / "sort_plan.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["seq", "street", "pass", "direction", "from_cross",
                    "to_cross", "length_m", "cum_km", "houses", "count"])
        for r in runs:
            w.writerow([r["seq"], r["street"], r["pass"], r["direction"],
                        r["from_cross"], r["to_cross"], round(r["metres"], 1),
                        r["cum_km"], describe(r["numbers"]),
                        len(r["numbers"])])
    sheet = result_dir / "sort_plan.html"
    write_sheet(runs, sheet, summary)
    print(f"{summary}\n  {csv_path}\n  {sheet}")


if __name__ == "__main__":
    main()
