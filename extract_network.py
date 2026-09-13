#!/usr/bin/env python3
"""Extract the street network for a postal round from OpenStreetMap.

Outputs (into --out directory):
    edges.csv     one row per street segment.  EDIT THE `service` COLUMN:
                      2 = deliver both sides (traverse twice)  [default]
                      1 = deliver one side only; confirm delivery=left/right
                          in a single pass (traverse once)
                      0 = not my street, but usable as a shortcut
                      x = never use (excluded from routing entirely)
    nodes.csv     intersection coordinates (used by the solver)
    preview.html  interactive map -- hover any segment to see its
                  edge_id, name, road type and length, so you know
                  which row to edit

Area selection, either:
    --place  one or more OSM place names, e.g.
             "Albert Park, Victoria, Australia"
    --polygon a GeoJSON file with your exact round boundary
             (draw one at https://geojson.io and save it)

Usage:
    python extract_network.py --place "Albert Park, Victoria, Australia" --out data
    python extract_network.py --polygon round.geojson --out data
"""

import argparse
import csv
import json
import sys
from pathlib import Path

import networkx as nx


# ----------------------------------------------------------------------

def load_polygon(path, buffer_deg):
    from shapely.geometry import shape
    from shapely.ops import unary_union
    with open(path, encoding="utf-8") as f:
        gj = json.load(f)
    if gj.get("type") == "FeatureCollection":
        feats = gj["features"]
    elif gj.get("type") == "Feature":
        feats = [gj]
    else:
        feats = [{"geometry": gj}]
    geoms = [shape(ft["geometry"]) for ft in feats]
    return unary_union(geoms).buffer(buffer_deg)


def places_polygon(places, buffer_deg):
    import osmnx as ox
    gdf = ox.geocode_to_gdf(places)
    try:
        merged = gdf.union_all()
    except AttributeError:  # older geopandas
        merged = gdf.geometry.unary_union
    return merged.buffer(buffer_deg)


def parse_maxspeed(val):
    """OSM `maxspeed` -> km/h, or "" when it says nothing usable.

    Values seen in the wild: "60", "40 mph", "walk", "AU:urban",
    "signals", and lists when osmnx merged several ways. A footway has
    no maxspeed at all, which is why the solver keeps its own speed for
    surfaces you ride rather than drive."""
    if val is None:
        return ""
    if isinstance(val, (list, tuple)):
        speeds = [parse_maxspeed(v) for v in val]
        speeds = [s for s in speeds if s != ""]
        return min(speeds) if speeds else ""
    text = str(val).strip().lower()
    if text in ("walk", "walking"):
        return 5.0
    mph = text.endswith("mph")
    number = text[:-3].strip() if mph else text
    try:
        kmh = float(number)
    except ValueError:
        return ""            # "AU:urban", "signals", "none", ...
    if kmh <= 0:
        return ""
    return round(kmh * 1.609344, 1) if mph else kmh


def norm(val, fallback=""):
    """OSM attributes may be str, list or missing."""
    if val is None:
        return fallback
    if isinstance(val, (list, tuple)):
        return "; ".join(str(x) for x in val)
    return str(val)


def oneway_arcs(G):
    """{(u, v)} of the LEGAL direction of every one-way edge, taken
    from the directed graph before it is collapsed.

    Needed because `to_undirected()` keeps no direction, and iterating
    an undirected MultiGraph yields (u, v) in node-insertion order --
    so roughly half of the one-way edges would be exported pointing
    the wrong way, which silently corrupts anything that reasons about
    direction (the `against_oneway` flag, `--wrong-way-penalty`)."""
    return {(u, v) for u, v, d in G.edges(data=True) if d.get("oneway")}


CONTROL_RADIUS_M = 40.0     # nodes this close are one intersection


def node_control(d):
    """What makes you stop at this node, if anything. A signalised
    crossing is tagged either way round, so both are checked."""
    highway = norm(d.get("highway"))
    crossing = norm(d.get("crossing"))
    if highway == "traffic_signals" or crossing == "traffic_signals":
        return "signals"
    if highway == "crossing":
        return "crossing"
    if highway in ("give_way", "stop"):
        return highway
    return ""


def control_groups(G):
    """One intersection is several tagged nodes -- 4.5 of them on
    average in the maintainer's area -- so charging a wait per node
    would over-count by that much. Cluster them, and the solver can
    charge once per group entered."""
    import math
    controlled = [(n, node_control(d)) for n, d in G.nodes(data=True)
                  if node_control(d)]
    if not controlled:
        return {}
    lat0 = G.nodes[controlled[0][0]]["y"]
    mlat, mlon = 110540.0, 111320.0 * math.cos(math.radians(lat0))
    cell = {}
    size = CONTROL_RADIUS_M
    for n, _ in controlled:                      # grid buckets, then union
        d = G.nodes[n]
        cell.setdefault((int(d["y"] * mlat / size),
                         int(d["x"] * mlon / size)), []).append(n)
    parent = {n: n for n, _ in controlled}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for (cy, cx), members in cell.items():
        near = [m for dy in (-1, 0, 1) for dx in (-1, 0, 1)
                for m in cell.get((cy + dy, cx + dx), [])]
        for a in members:
            for b in near:
                if a == b:
                    continue
                da, db = G.nodes[a], G.nodes[b]
                if math.hypot((da["y"] - db["y"]) * mlat,
                              (da["x"] - db["x"]) * mlon) <= size:
                    parent[find(a)] = find(b)
    ids, out = {}, {}
    for n, kind in controlled:
        root = find(n)
        out[n] = (kind, ids.setdefault(root, len(ids) + 1))
    return out


def export_addresses(poly, out_dir: Path):
    """Every point in the area that carries a house number, so a pass
    along a street can be turned into the house numbers it serves.

    OSM rather than G-NAF: it is already the source of the network, it
    needs no 1.5 GB download, and 97% of the points here carry a street
    name. Coverage is whatever the local mappers have done -- the sort
    plan says how many numbers it found per run so you can see."""
    import osmnx as ox
    print("Downloading addresses from OpenStreetMap ...")
    gdf = ox.features_from_polygon(poly, tags={"addr:housenumber": True})
    path = out_dir / "addresses.csv"
    out_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["lat", "lon", "housenumber", "street"])
        for _, row in gdf.iterrows():
            street = row.get("addr:street")
            number = row.get("addr:housenumber")
            if not isinstance(number, str) or not isinstance(street, str):
                continue
            point = row.geometry.centroid          # ways -> their centre
            w.writerow([round(point.y, 7), round(point.x, 7),
                        number.strip(), street.strip()])
            n += 1
    print(f"  {n} addresses -> {path}")
    return path


def keep_control_nodes(G):
    """Simplify, but not over the things that make you stop.

    Simplification straightens a way by deleting the nodes along it,
    and a traffic signal or a give-way sitting mid-block is one of
    those nodes. Losing it loses the wait: on the maintainer's round it
    silently removed 58 whole intersections, mostly give-way and stop
    lines on side roads. Marking them first makes them junctions in
    their own right, which costs about 3% more edges and gets all of
    them back."""
    import osmnx as ox
    for _, d in G.nodes(data=True):
        kind = node_control(d)
        if kind:
            d["control"] = kind
    before = len({g for _, g in control_groups(G).values()})
    G = ox.simplify_graph(G, node_attrs_include=["control"])
    after = len({g for _, g in control_groups(G).values()})
    print(f"  simplified to {G.number_of_nodes()} nodes, "
          f"{G.number_of_edges()} edges; {after} controlled "
          f"intersections kept of {before}")
    if after < before:
        print(f"  warning: {before - after} controlled intersection(s) "
              f"were still lost in simplification -- their waiting time "
              f"will be missing from every route")
    return G


def download_graph(poly, network_type):
    import osmnx as ox
    # node tags are dropped by default; these are what makes you wait
    ox.settings.useful_tags_node = list(
        set(ox.settings.useful_tags_node) | {"highway", "crossing"})
    print("Downloading network from OpenStreetMap ...")
    G = ox.graph_from_polygon(poly, network_type=network_type,
                              simplify=False, truncate_by_edge=True)
    G = keep_control_nodes(G)
    arcs = oneway_arcs(G)
    G = ox.convert.to_undirected(G)
    G.graph["oneway_arcs"] = arcs
    # keep the largest connected component only
    if not nx.is_connected(G):
        biggest = max(nx.connected_components(G), key=len)
        dropped = G.number_of_nodes() - len(biggest)
        G = G.subgraph(biggest).copy()
        print(f"  dropped {dropped} nodes in disconnected fragments")
    return G


# ----------------------------------------------------------------------

def export(G, out_dir: Path, default_service=2):
    out_dir.mkdir(parents=True, exist_ok=True)

    nodes_path = out_dir / "nodes.csv"
    control = control_groups(G)
    with open(nodes_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["node_id", "lat", "lon", "control", "control_group"])
        for n, d in G.nodes(data=True):
            kind, group = control.get(n, ("", ""))
            w.writerow([n, d["y"], d["x"], kind, group])
    n_ctrl = len(control)
    n_groups = len({g for _, g in control.values()})
    if n_ctrl:
        print(f"  {n_ctrl} controlled nodes in {n_groups} intersections "
              f"(signals, crossings, give way, stop)")

    edges_path = out_dir / "edges.csv"
    arcs = G.graph.get("oneway_arcs") or set()
    rows = []
    reoriented = 0
    for u, v, k, d in G.edges(keys=True, data=True):
        name = norm(d.get("name")) or f"(unnamed {norm(d.get('highway'), 'way')})"
        # edge_id stays keyed on the iteration order (it is opaque, and
        # keeping it stable keeps existing annotations valid), but the
        # u/v columns are flipped to the legal direction when the
        # undirected view happened to store a one-way backwards.
        edge_id = f"{u}-{v}-{k}"
        if (v, u) in arcs and (u, v) not in arcs:
            u, v = v, u
            reoriented += 1
        geom = d.get("geometry")
        if geom is not None:
            wkt = geom.wkt
        else:
            wkt = (f"LINESTRING ({G.nodes[u]['x']} {G.nodes[u]['y']}, "
                   f"{G.nodes[v]['x']} {G.nodes[v]['y']})")
        rows.append({
            "edge_id": edge_id,
            "name": name,
            "highway": norm(d.get("highway")),
            "oneway": norm(d.get("oneway")),
            "maxspeed_kmh": parse_maxspeed(d.get("maxspeed")),
            "length_m": round(float(d["length"]), 1),
            "service": default_service,
            "note": "",
            "u": u,
            "v": v,
            "geometry_wkt": wkt,
            "delivery": {0: "none", 1: "unspecified", 2: "both"}[default_service],
        })
    if reoriented:
        print(f"  {reoriented} one-way edges written in their legal "
              f"direction (the undirected view had them backwards)")
    rows.sort(key=lambda r: (r["name"], r["edge_id"]))
    with open(edges_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    return nodes_path, edges_path, rows


HIGHWAY_COLORS = {
    "residential": "#2b8cbe", "living_street": "#2b8cbe",
    "unclassified": "#2b8cbe", "tertiary": "#08519c",
    "secondary": "#08306b", "primary": "#08306b", "trunk": "#08306b",
    "footway": "#e6550d", "path": "#e6550d", "pedestrian": "#e6550d",
    "steps": "#a50f15", "cycleway": "#31a354", "service": "#969696",
    "track": "#969696",
}


def preview_map(G, rows, out_dir: Path):
    import folium
    from shapely import wkt as shapely_wkt

    ys = [d["y"] for _, d in G.nodes(data=True)]
    xs = [d["x"] for _, d in G.nodes(data=True)]
    m = folium.Map(location=[sum(ys) / len(ys), sum(xs) / len(xs)],
                   zoom_start=15, tiles="cartodbpositron")
    folium.LatLngPopup().add_to(m)   # click anywhere -> shows lat/lon
    for r in rows:
        line = shapely_wkt.loads(r["geometry_wkt"])
        pts = [(y, x) for x, y in line.coords]
        hw = r["highway"].split(";")[0].strip()
        folium.PolyLine(
            pts, weight=4, opacity=0.85,
            color=HIGHWAY_COLORS.get(hw, "#636363"),
            tooltip=(f'{r["name"]} | {r["highway"]} | '
                     f'{r["length_m"]} m | id {r["edge_id"]}'),
        ).add_to(m)
    m.fit_bounds([[min(ys), min(xs)], [max(ys), max(xs)]])
    legend = """
     <div style="position: fixed; bottom: 20px; left: 20px; z-index: 9999;
                 background: white; padding: 8px 12px; border: 1px solid #999;
                 font-size: 12px; line-height: 1.6;">
       <b>road type</b><br>
       <span style="color:#2b8cbe">&#9644;</span> residential /
       <span style="color:#08519c">&#9644;</span> tertiary /
       <span style="color:#08306b">&#9644;</span> main road<br>
       <span style="color:#e6550d">&#9644;</span> footway&#47;path /
       <span style="color:#31a354">&#9644;</span> cycleway /
       <span style="color:#969696">&#9644;</span> service&#47;track<br>
       <span style="color:#a50f15">&#9644;</span> steps (set service=0!)
     </div>"""
    m.get_root().html.add_child(folium.Element(legend))
    path = out_dir / "preview.html"
    m.save(str(path))
    return path


# ----------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--place", nargs="+",
                    help='OSM place name(s), e.g. "Albert Park, '
                         'Victoria, Australia"')
    ap.add_argument("--polygon",
                    help="GeoJSON file with your round boundary "
                         "(draw at geojson.io)")
    ap.add_argument("--network-type", default="all",
                    choices=["all", "walk", "bike", "drive"],
                    help="which OSM ways to include (default: all -- "
                         "footpaths AND carriageways. `walk` looks "
                         "right for an EDV but drops the roadway "
                         "pieces inside big junctions, so 'straight "
                         "through the intersection' stops existing and "
                         "routes detour over pedestrian crossings; it "
                         "also reports every way as two-way)")
    ap.add_argument("--buffer", type=float, default=0.0006,
                    help="boundary buffer in degrees (~60 m) so that "
                         "roads on the area boundary are kept")
    ap.add_argument("--default-service", type=int, default=2,
                    choices=[0, 1, 2],
                    help="initial value of the service column (default 2)")
    ap.add_argument("--addresses", action="store_true",
                    help="also fetch house numbers, so sort_plan.py can "
                         "turn each pass into the numbers it serves")
    ap.add_argument("--out", default="data", help="output directory")
    args = ap.parse_args()

    if not args.place and not args.polygon:
        ap.error("give --place or --polygon")

    if args.polygon:
        poly = load_polygon(args.polygon, args.buffer)
    else:
        poly = places_polygon(args.place, args.buffer)

    G = download_graph(poly, args.network_type)
    if args.addresses:
        export_addresses(poly, Path(args.out))
    out_dir = Path(args.out)
    nodes_path, edges_path, rows = export(G, out_dir, args.default_service)
    prev_path = preview_map(G, rows, out_dir)

    total_km = sum(r["length_m"] for r in rows) / 1000
    by_hw = {}
    for r in rows:
        hw = r["highway"].split(";")[0].strip()
        by_hw[hw] = by_hw.get(hw, 0) + 1
    hw_summary = ", ".join(f"{k}:{v}" for k, v in
                           sorted(by_hw.items(), key=lambda kv: -kv[1]))

    print(f"""
Extracted {len(rows)} street segments / {G.number_of_nodes()} intersections
Total network length: {total_km:.1f} km
By road type: {hw_summary}

Files written:
  {edges_path}   <-- EDIT THE service COLUMN (2 / 1 / 0), then run solve_route.py
  {nodes_path}
  {prev_path}   <-- open in a browser; hover segments to identify edge_ids

Editing tips:
  * open edges.csv in Excel, it is sorted by street name -- a whole street
    can usually be set in one block
  * streets that are not yours -> 0   (they stay usable as shortcuts)
  * one-side-only streets (e.g. a divided arterial where you serve
    a single carriageway) -> 1
  * footway / path segments are orange on the preview: keep the ones you
    actually ride, zero the rest; ALWAYS set 'steps' to x (excluded --
    never routed through, not even as a shortcut)
  * with --network-type all, check for ways you cannot ride at all
    (motorway, steps, corridor) and set them to x -- unlike `walk`,
    this network does not filter them out for you""")


if __name__ == "__main__":
    main()
