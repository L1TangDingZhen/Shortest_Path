#!/usr/bin/env python3
"""Extract the street network for a postal round from OpenStreetMap.

Outputs (into --out directory):
    edges.csv     one row per street segment.  EDIT THE `service` COLUMN:
                      2 = deliver both sides (traverse twice)  [default]
                      1 = deliver one side only, or zigzag both sides
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


def download_graph(poly, network_type):
    import osmnx as ox
    print("Downloading network from OpenStreetMap ...")
    G = ox.graph_from_polygon(poly, network_type=network_type,
                              simplify=True, truncate_by_edge=True)
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
    with open(nodes_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["node_id", "lat", "lon"])
        for n, d in G.nodes(data=True):
            w.writerow([n, d["y"], d["x"]])

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
    ap.add_argument("--out", default="data", help="output directory")
    args = ap.parse_args()

    if not args.place and not args.polygon:
        ap.error("give --place or --polygon")

    if args.polygon:
        poly = load_polygon(args.polygon, args.buffer)
    else:
        poly = places_polygon(args.place, args.buffer)

    G = download_graph(poly, args.network_type)
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
