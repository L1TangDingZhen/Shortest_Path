#!/usr/bin/env python3
"""Make a round reproducible after re-extraction (B1).

Re-running extract_network.py regenerates edges.csv and silently wipes
every hand-edited `service` value and every edge split (E8).  This tool
keeps the annotation in two small declarative files inside the
gitignored round directory and replays them onto a fresh extraction:

    round.local/service_overrides.csv   edge_id,service,name,highway,
                                        length_m,geometry_wkt
        -- the complete annotation snapshot.  Annotated rows (service
           1/2/x) also carry road type and geometry, so an edge whose
           id changed can still be found by where it is on the ground
    round.local/splits.csv              at_lat,at_lon,edge_id
        -- every split point, replayed via split_edge.py in order
           (parents before children, so nested splits compose)
    round.local/endpoints.json          the round's start/end, as picked
        -- in the editor; copied back to data/ on apply

Workflow:

    python prepare_round.py --data data --round round.local --export
        snapshot the CURRENT data/edges.csv (run this BEFORE
        re-extracting -- e.g. before enlarging the polygon to cover a
        depot / handover office)

    python extract_network.py --polygon round.local/my_round.geojson \
        --default-service 0 --out data
        re-extract; with default 0, streets that are new to the
        enlarged area arrive as connectors, not as service

    python prepare_round.py --data data --round round.local
        (default mode) replay splits, then apply the overrides;
        edges without an override keep the extraction default --
        review them in the editor (they are the newly added streets)

Backups: edges.csv.bak / nodes.csv.bak are written before applying.

edge_ids come from OSM way/node ids and are stable while the map is,
but a different --network-type changes which junctions exist, so
osmnx splits ways differently and some ids disappear.  Overrides that
match no id are therefore retried **by geometry**: any new edge lying
along the old one (same name and road type, within a few metres) picks
up its value.  Whatever still cannot be placed is reported, never
silently dropped.
"""

import argparse
import csv
import re
import shutil
import subprocess
import sys
from pathlib import Path

from split_edge import parse_wkt_linestring, project_onto_chain

BASE = Path(__file__).parent
NOTE_RE = re.compile(r"split from (\S+) at (-?[0-9.]+),(-?[0-9.]+)")
GEOM_TOL_M = 6.0      # how far a new edge may sit off the old geometry
LEN_SLACK = 1.25      # a re-found piece may not be much longer


def read_edges(data_dir: Path):
    with open(data_dir / "edges.csv", newline="",
              encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames), list(reader)


def read_nodes(data_dir: Path):
    with open(data_dir / "nodes.csv", newline="",
              encoding="utf-8-sig") as f:
        return {r["node_id"]: (float(r["lat"]), float(r["lon"]))
                for r in csv.DictReader(f)}


def edge_points(row, nodes):
    """(start, middle, end) of an edge as (lat, lon), geometry-aware."""
    coords = parse_wkt_linestring(row.get("geometry_wkt") or "")
    if not coords:
        try:
            coords = [nodes[row["u"]], nodes[row["v"]]]
        except KeyError:
            return None
    return coords[0], coords[len(coords) // 2], coords[-1]


def geometric_fallback(rows, stale, nodes):
    """Re-apply annotations whose edge_id disappeared, by finding the
    new edges that lie along the old geometry. Returns (n_applied,
    list of overrides that could not be placed)."""
    if not stale:
        return 0, []
    by_id = {r["edge_id"]: r for r in rows}
    claims = {}          # edge_id -> set of service values proposed
    unplaced = []

    for old in stale:
        coords = parse_wkt_linestring(old["geometry_wkt"])
        if not coords:
            unplaced.append(old)
            continue
        lats = [c[0] for c in coords]
        lons = [c[1] for c in coords]
        pad = GEOM_TOL_M / 110540.0
        box = (min(lats) - pad, max(lats) + pad,
               min(lons) - pad * 1.25, max(lons) + pad * 1.25)
        try:
            max_len = float(old["length_m"]) * LEN_SLACK + 5
        except ValueError:
            max_len = float("inf")

        # osmnx merges ways that have no junction between them, giving
        # "A Street; B Street" / "residential; trunk"; a network type
        # with more junctions splits them apart again, so accept any
        # component of the merged name/type.
        names = {p.strip() for p in old["name"].split(";")}
        types = {p.strip() for p in old["highway"].split(";")}
        hits = []
        for row in rows:
            if (row.get("name") or "").strip() not in names:
                continue
            if (row.get("highway") or "").strip() not in types:
                continue
            if float(row.get("length_m") or 0) > max_len:
                continue
            pts = edge_points(row, nodes)
            if not pts:
                continue
            if not all(box[0] <= lat <= box[1] and box[2] <= lon <= box[3]
                       for lat, lon in pts):
                continue
            if all(project_onto_chain(coords, p)[0] <= GEOM_TOL_M
                   for p in pts):
                hits.append(row["edge_id"])
        if hits:
            for eid in hits:
                claims.setdefault(eid, set()).add(old["service"])
        else:
            unplaced.append(old)

    applied = 0
    for eid, values in claims.items():
        if len(values) > 1:
            print(f"  warning: {eid} lies under several old edges with "
                  f"different values ({', '.join(sorted(values))}) -- "
                  f"left at the extraction default, set it by hand")
            continue
        by_id[eid]["service"] = values.pop()
        applied += 1
    return applied, unplaced


def export(data_dir: Path, round_dir: Path):
    _, rows = read_edges(data_dir)
    nodes = read_nodes(data_dir)
    round_dir.mkdir(parents=True, exist_ok=True)

    ov_path = round_dir / "service_overrides.csv"
    with open(ov_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["edge_id", "service", "name", "highway", "length_m",
                    "geometry_wkt"])
        for r in rows:
            service = (r.get("service") or "").strip()
            if service.lower() in ("1", "2", "x"):
                # annotated: keep enough to re-find it geometrically
                wkt = r.get("geometry_wkt") or ""
                if not wkt:
                    try:
                        (ay, ax), (by, bx) = nodes[r["u"]], nodes[r["v"]]
                        wkt = f"LINESTRING ({ax} {ay}, {bx} {by})"
                    except KeyError:
                        wkt = ""
                extra = [r.get("highway") or "", r.get("length_m") or "",
                         wkt]
            else:
                extra = ["", "", ""]
            w.writerow([r["edge_id"], service, r.get("name") or "",
                        *extra])

    splits = {}
    for r in rows:
        m = NOTE_RE.search(r.get("note") or "")
        if m and r["edge_id"].startswith(m.group(1) + "#"):
            splits[(m.group(1), m.group(2), m.group(3))] = None
    ordered = sorted(splits, key=lambda k: (k[0].count("#"), k[0]))
    sp_path = round_dir / "splits.csv"
    with open(sp_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["at_lat", "at_lon", "edge_id"])
        for src, lat, lon in ordered:
            w.writerow([lat, lon, src])

    ep_src = data_dir / "endpoints.json"
    if ep_src.exists():
        shutil.copy2(ep_src, round_dir / "endpoints.json")
        print(f"Exported route endpoints    -> "
              f"{round_dir / 'endpoints.json'}")

    print(f"Exported {len(rows)} service values -> {ov_path}")
    print(f"Exported {len(ordered)} split(s)      -> {sp_path}")
    print("Safe to re-extract now; apply with:  "
          f"python prepare_round.py --data {data_dir} --round {round_dir}")


def apply(data_dir: Path, round_dir: Path):
    ov_path = round_dir / "service_overrides.csv"
    sp_path = round_dir / "splits.csv"
    if not ov_path.exists():
        sys.exit(f"{ov_path} not found -- run --export first "
                 f"(before re-extracting).")

    shutil.copy2(data_dir / "edges.csv", data_dir / "edges.csv.bak")
    shutil.copy2(data_dir / "nodes.csv", data_dir / "nodes.csv.bak")

    n_split = n_split_failed = n_split_done = 0
    if sp_path.exists():
        present = {r["edge_id"] for r in read_edges(data_dir)[1]}
        with open(sp_path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if (row["edge_id"] not in present
                        and row["edge_id"] + "#a" in present):
                    n_split_done += 1     # already split (apply re-run)
                    continue
                res = subprocess.run(
                    [sys.executable, str(BASE / "split_edge.py"),
                     "--data", str(data_dir),
                     f"--at={row['at_lat']},{row['at_lon']}",
                     "--edge", row["edge_id"]],
                    capture_output=True, text=True)
                if res.returncode == 0:
                    n_split += 1
                else:
                    n_split_failed += 1
                    print(f"  warning: could not replay split of "
                          f"{row['edge_id']} at {row['at_lat']},"
                          f"{row['at_lon']}:\n    "
                          f"{(res.stderr or res.stdout).strip()}")

    overrides, detail = {}, {}
    with open(ov_path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            overrides[row["edge_id"]] = (row.get("service") or "").strip()
            detail[row["edge_id"]] = {
                "edge_id": row["edge_id"],
                "service": (row.get("service") or "").strip(),
                "name": row.get("name") or "",
                "highway": row.get("highway") or "",
                "length_m": row.get("length_m") or "",
                "geometry_wkt": row.get("geometry_wkt") or "",
            }

    header, rows = read_edges(data_dir)
    applied = 0
    for r in rows:
        if r["edge_id"] in overrides:
            r["service"] = overrides.pop(r["edge_id"])
            applied += 1
    fresh = len(rows) - applied

    # Whatever is left is stale: its id vanished (a different
    # --network-type splits ways at different junctions). Re-find the
    # annotated ones by geometry -- one old edge often becomes several.
    stale_annotated = [detail[eid] for eid in overrides
                       if detail.get(eid, {}).get("service", "").lower()
                       in ("1", "2", "x")]
    stale_plain = len(overrides) - len(stale_annotated)
    regained, unplaced = geometric_fallback(
        rows, stale_annotated, read_nodes(data_dir))
    applied += regained
    with open(data_dir / "edges.csv", "w", newline="",
              encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        w.writerows(rows)

    ep_stored = round_dir / "endpoints.json"
    if ep_stored.exists():
        shutil.copy2(ep_stored, data_dir / "endpoints.json")
        print("Restored route endpoints -> "
              f"{data_dir / 'endpoints.json'}")

    print(f"\nApplied {applied} service overrides"
          + (f", replayed {n_split} split(s)" if n_split else "")
          + (f", {n_split_failed} split(s) FAILED" if n_split_failed
             else "") + ".")
    if fresh:
        print(f"{fresh} edges had no override (new streets from the "
              f"enlarged area) -- they keep the extraction default. "
              f"Review them in the editor (make_editor.py --serve).")
    if regained:
        print(f"{regained} edge(s) whose id had changed were re-found by "
              f"geometry (an old edge often becomes several new ones).")
    if unplaced:
        print(f"warning: {len(unplaced)} annotated edge(s) could not be "
              f"placed at all -- re-mark them in the editor:")
        for old in unplaced[:8]:
            print(f"    service={old['service']}  "
                  f"{old['name'][:34]} ({old['length_m']} m)")
    if stale_plain > 0:
        print(f"({stale_plain} stale override(s) were plain connectors "
              f"(service 0) -- nothing lost, they default to 0.)")
    print("Backups: edges.csv.bak, nodes.csv.bak")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", default="data",
                    help="directory with edges.csv + nodes.csv")
    ap.add_argument("--round", default="round.local", dest="round_dir",
                    help="directory holding the declarative round "
                         "config (gitignored)")
    ap.add_argument("--export", action="store_true",
                    help="snapshot the current annotation instead of "
                         "applying the stored one")
    args = ap.parse_args()
    if args.export:
        export(Path(args.data), Path(args.round_dir))
    else:
        apply(Path(args.data), Path(args.round_dir))


if __name__ == "__main__":
    main()
