#!/usr/bin/env python3
"""Generate a browser-based click-editor for the `service` column.

Reads edges.csv + nodes.csv from --data and writes editor.html next to
them.  Open it in a browser:

    * every edge is drawn over a basemap, coloured by service
      (blue = 2 both sides, orange = 1 one side, grey dashed = 0)
    * click to cycle 2 -> 1 -> 0 -> 2 (ctrl/cmd-click sets 0 directly
      -- the common "not my street" case; shift-click toggles x =
      excluded, an edge the route may never use, not even as a
      shortcut).  Picking always targets the edge nearest to the
      cursor -- line thickness and draw order never steal the click --
      and the hovered edge is thickened + captioned first, so you see
      what a click will change.  Zoom reaches building level (basemap
      upscales past its native 20).
    * search by street name or road type ("trunk", "steps", ...),
      then bulk-set every matched edge at once
    * right-click anywhere to set that point as the round's START or
      END, or just copy the coordinates
    * with --serve: Solve runs the solver on the saved endpoints and
      shows the route map without touching the command line
    * live counters: edges per service value, mandatory km, unsaved edits
    * Export downloads an updated edges.csv (all columns preserved, only
      `service` changed) -- replace data/edges.csv with it and re-run
      solve_route.py

Two modes:

  --serve (recommended)   run a local server; the page is rebuilt from
      the CSVs on every reload (never stale) and the Save button writes
      straight back to data/edges.csv (previous version kept as
      edges.csv.bak).  Local only: binds 127.0.0.1.

  default (no --serve)    write a static data/editor.html snapshot;
      Export downloads an edges.csv for you to copy over data/edges.csv
      -- then re-run make_editor.py, or the page shows stale data.

No round data leaves your machine either way.  (The basemap tiles and
the Leaflet library load from public CDNs, so the map needs internet.)

Usage:
    python make_editor.py --data data --serve
    python make_editor.py --data data
"""

import argparse
import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).parent
ENDPOINTS = "endpoints.json"
DEFAULT_HISTORY = "round.local/history"


def parse_wkt_linestring(wkt):
    """'LINESTRING (x y, x y, ...)' -> [[lat, lon], ...] or None."""
    m = re.match(r"\s*LINESTRING\s*\((.+)\)\s*$", wkt, re.IGNORECASE)
    if not m:
        return None
    pts = []
    for pair in m.group(1).split(","):
        xy = pair.split()
        if len(xy) != 2:
            return None
        try:
            x, y = float(xy[0]), float(xy[1])
        except ValueError:
            return None
        # 6 decimals is ~0.1 m: far below any editing decision, and it
        # keeps the embedded payload roughly a third smaller
        pts.append([round(y, 6), round(x, 6)])   # WKT is lon lat
    return pts if len(pts) >= 2 else None


def load(data_dir: Path):
    nodes = {}
    with open(data_dir / "nodes.csv", newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            nodes[row["node_id"]] = [float(row["lat"]), float(row["lon"])]

    edges, blank = [], 0
    with open(data_dir / "edges.csv", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        header = list(reader.fieldnames)
        for row in reader:
            raw = (row.get("service") or "").strip()
            if raw == "":
                blank += 1
                service = 0
            elif raw.lower() == "x":
                service = "x"
            else:
                try:
                    service = int(raw)
                except ValueError:
                    sys.exit(f"edges.csv: bad service value {raw!r} "
                             f"on edge {row['edge_id']} "
                             f"(must be 0, 1, 2 or x)")
            if service not in (0, 1, 2, "x"):
                sys.exit(f"edges.csv: service={service} on edge "
                         f"{row['edge_id']} (must be 0, 1, 2 or x)")
            coords = parse_wkt_linestring(row.get("geometry_wkt") or "")
            if coords is None:
                try:
                    coords = [nodes[row["u"]], nodes[row["v"]]]
                except KeyError as e:
                    sys.exit(f"edges.csv: edge {row['edge_id']} has no "
                             f"geometry and node {e} is not in nodes.csv")
            edges.append({"row": row, "service": service, "coords": coords})
    if blank:
        print(f"  note: {blank} edges had a blank service value "
              f"-> shown as 0 (connector only)")
    return header, edges


def load_endpoints(data_dir: Path):
    """Round endpoints picked in the editor: {"start": [lat, lon],
    "end": [...], "return_to_start": bool}.  Missing file -> {}."""
    path = data_dir / ENDPOINTS
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def save_endpoints(data_dir: Path, data):
    """Validate and store the endpoints picked in the editor."""
    if not isinstance(data, dict):
        raise ValueError("endpoints must be an object")
    out = {}
    for key in ("start", "end"):
        val = data.get(key)
        if val is None:
            continue
        try:
            lat, lon = float(val[0]), float(val[1])
        except (TypeError, ValueError, IndexError):
            raise ValueError(f"{key}: expected [lat, lon]")
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError(f"{key}: {lat},{lon} is not a coordinate")
        out[key] = [lat, lon]
    out["return_to_start"] = bool(data.get("return_to_start"))
    (data_dir / ENDPOINTS).write_text(
        json.dumps(out, indent=1), encoding="utf-8")
    return out


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>service editor</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html, body { height: 100%; margin: 0; }
  #map { height: 100%; }
  #panel {
    position: absolute; top: 10px; left: 50px; z-index: 1000;
    background: rgba(255,255,255,.96); border: 1px solid #bbb;
    border-radius: 6px; padding: 10px 12px; width: 320px;
    font: 13px/1.5 system-ui, sans-serif; box-shadow: 0 1px 4px rgba(0,0,0,.2);
  }
  #panel b { font-size: 14px; }
  .row { margin-top: 6px; }
  .swatch { display: inline-block; width: 18px; height: 4px;
            vertical-align: middle; margin-right: 4px; border-radius: 2px; }
  #search { width: 130px; }
  button { cursor: pointer; }
  button:disabled { cursor: default; opacity: .45; }
  #hint { color: #666; margin-top: 6px; }
  #export { font-weight: 600; }
</style>
</head>
<body>
<div id="map"></div>
<div id="panel">
  <b>service editor</b>
  <div class="row" id="stats"></div>
  <div class="row">
    <span class="swatch" style="background:#2b6cb0"></span>2 both sides&nbsp;
    <span class="swatch" style="background:#dd6b20"></span>1 one side&nbsp;
    <span class="swatch" style="background:#a0aec0"></span>0 not mine&nbsp;
    <span class="swatch" style="background:#b91c1c"></span>x banned
  </div>
  <div class="row">
    click sets
    <select id="paint">
      <option value="cycle">cycle 2&rarr;1&rarr;0</option>
      <option value="2">2 &mdash; both sides</option>
      <option value="1">1 &mdash; one side</option>
      <option value="0">0 &mdash; not mine</option>
      <option value="x">x &mdash; never use</option>
    </select>
  </div>
  <div class="row">
    search <input id="search" placeholder="name / road type">
    <span id="nmatch"></span><br>
    set matched to
    <button id="bulk2">2</button>
    <button id="bulk1">1</button>
    <button id="bulk0">0</button>
    <button id="bulkx">x</button>
  </div>
  <div class="row">
    <button id="undo">undo</button>
    <button id="export">Export edges.csv</button>
    <span id="dirty"></span> <span id="savemsg"></span>
  </div>
  <div class="row">
    <button id="islands">show islands</button>
    <div id="islelist" style="max-height:150px;overflow:auto"></div>
  </div>
  <div class="row" id="epbox">
    <b>route endpoints</b> <span style="color:#666">(right-click the
    map to set)</span><br>
    <span id="epstart">start: not set</span>
    <button data-clear="start">clear</button><br>
    <span id="epend">end: not set</span>
    <button data-clear="end">clear</button><br>
    <label><input type="checkbox" id="eprts"> return to start after
      the end</label><br>
    <input id="note" placeholder="label this version (optional)"
           style="width:150px">
    <button id="solve">Solve route</button>
    <span id="solvemsg"></span>
    <div id="history" style="margin-top:4px"></div>
    <pre id="solveout" style="display:none;max-height:170px;overflow:auto;
      background:#f4f4f4;padding:6px;white-space:pre-wrap;
      font-size:11px"></pre>
  </div>
  <div id="hint">hover: the thickened edge is what a click will
    change &middot; click: cycle 2&rarr;1&rarr;0 &middot;
    ctrl-click: set 0 &middot; shift-click: toggle x
    (banned &mdash; never routed through, not even as a shortcut)
    &middot; right-click: set the route's start/end<br>
    search matches street name and road type
    (e.g. "trunk", "steps")<br>
    <span id="posthint">after export, replace data/edges.csv with the
    download, then re-run make_editor.py + solve_route.py</span></div>
</div>
<script id="edge-data" type="application/json">__PAYLOAD__</script>
<script>
"use strict";
const payload = JSON.parse(document.getElementById("edge-data").textContent);
const SERVE = location.protocol === "http:" || location.protocol === "https:";
if (SERVE) {
  document.getElementById("export").textContent = "Save";
  document.getElementById("posthint").textContent =
    "Save writes straight to data/edges.csv " +
    "(previous version kept as edges.csv.bak)";
}
const COLORS = {2: "#2b6cb0", 1: "#dd6b20", 0: "#a0aec0", x: "#b91c1c"};
const WEIGHTS = {2: 5, 1: 5, 0: 3, x: 2};
const DASH = {2: null, 1: null, 0: "4 6", x: "2 5"};
const MATCH_COLOR = "#c026d3";

const renderer = L.canvas();
// boxZoom off: it hijacks shift+click (a 1 px wobble zooms to a box),
// which is exactly the gesture that toggles x.
const map = L.map("map",
  {renderer: renderer, maxZoom: 22, boxZoom: false});
L.tileLayer("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
  {maxZoom: 22, maxNativeZoom: 20,
   attribution: "&copy; OpenStreetMap contributors &copy; CARTO"}).addTo(map);

const edges = payload.edges;
const undoStack = [];
let matched = new Set();
let hoverI = -1;

// island view: connected components of the service>0 subgraph
let islandMode = false;
let comp = {};      // edge index -> island number
let islands = [];   // [{idxs, km, bounds}] biggest first
const ISLAND_COLORS = ["#d73027", "#1a9850", "#7570b3", "#e6ab02",
                       "#66a61e", "#e7298a", "#a6761d", "#525252"];

function computeIslands() {
  const parent = {};
  function find(x) {
    while (parent[x] !== x) { parent[x] = parent[parent[x]]; x = parent[x]; }
    return x;
  }
  edges.forEach(function (e) {
    if (e.service > 0) {
      [e.row.u, e.row.v].forEach(function (n) {
        if (!(n in parent)) parent[n] = n;
      });
      parent[find(e.row.u)] = find(e.row.v);
    }
  });
  const groups = {};
  edges.forEach(function (e, i) {
    if (e.service > 0) {
      const r = find(e.row.u);
      (groups[r] = groups[r] || []).push(i);
    }
  });
  islands = Object.values(groups).map(function (idxs) {
    let km = 0;
    const pts = [];
    idxs.forEach(function (i) {
      km += (parseFloat(edges[i].row.length_m) || 0) * edges[i].service;
      edges[i].coords.forEach(function (c) { pts.push(c); });
    });
    return {idxs: idxs, km: km / 1000, bounds: L.latLngBounds(pts)};
  }).sort(function (a, b) { return b.idxs.length - a.idxs.length; });
  comp = {};
  islands.forEach(function (isl, ci) {
    isl.idxs.forEach(function (i) { comp[i] = ci; });
  });
}

function renderIslandList() {
  const el = document.getElementById("islelist");
  if (!islandMode) { el.innerHTML = ""; return; }
  el.innerHTML = islands.map(function (isl, ci) {
    const c = ISLAND_COLORS[ci % ISLAND_COLORS.length];
    return '<div><span class="swatch" style="background:' + c + '"></span>' +
      "#" + (ci + 1) + ": " + isl.idxs.length + " edges, " +
      isl.km.toFixed(2) + ' km <button data-isl="' + ci + '">go</button>' +
      "</div>";
  }).join("") + (islands.length > 1
    ? '<div style="color:#666">1 island = exact optimum; extra islands ' +
      "get auto-bridged (fine if intended, fix if stray)</div>"
    : '<div style="color:#666">single island: solver result is exact</div>');
}
document.getElementById("islelist").addEventListener("click", function (ev) {
  const b = ev.target.closest("button[data-isl]");
  if (b) map.fitBounds(islands[+b.dataset.isl].bounds.pad(0.35));
});
document.getElementById("islands").onclick = function () {
  islandMode = !islandMode;
  this.textContent = islandMode ? "hide islands" : "show islands";
  refresh();
  edges.forEach(function (e, i) { e.layer.setStyle(styleFor(e, i)); });
};

function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
                  .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
function styleFor(e, i) {
  const hit = matched.has(i);
  const hov = i === hoverI;
  if (islandMode && !hit) {
    if (e.service > 0) {
      return {color: ISLAND_COLORS[comp[i] % ISLAND_COLORS.length],
              weight: 5 + (hov ? 3 : 0), dashArray: null, opacity: 1};
    }
    return {color: "#b0b7c0", weight: 2 + (hov ? 3 : 0),
            dashArray: "3 6", opacity: 0.45};
  }
  return {color: hit ? MATCH_COLOR : COLORS[e.service],
          weight: WEIGHTS[e.service] + (hit ? 2 : 0) + (hov ? 3 : 0),
          dashArray: DASH[e.service],
          opacity: hov ? 1
            : (e.service === 0 ? 0.8 : e.service === "x" ? 0.75 : 0.95)};
}
function tip(e) {
  const r = e.row;
  return "<b>" + esc(r.name || "(unnamed)") + "</b> &mdash; service " +
         e.service + "<br>" + esc(r.highway || "") + " &middot; " +
         esc(r.length_m || "?") + " m &middot; id " + esc(r.edge_id);
}

function clickEdge(i, domEvent) {
  const e = edges[i];
  const paint = document.getElementById("paint").value;
  let next;
  if (paint !== "cycle") {
    next = paint === "x" ? "x" : +paint;      // painting a fixed value
  } else if (domEvent.shiftKey) {
    next = e.service === "x" ? 0 : "x";       // toggle "never use"
  } else if (domEvent.ctrlKey || domEvent.metaKey) {
    next = 0;
  } else if (e.service === "x") {
    next = 0;                                 // leave x via plain click
  } else {
    next = (e.service + 2) % 3;
  }
  if (next === e.service) return;
  undoStack.push([[i, e.service, next]]);
  apply(i, next);
  refresh();
}

edges.forEach(function (e, i) {
  e.baseline = e.service;
  e.layer = L.polyline(e.coords,
    Object.assign({interactive: false}, styleFor(e, i))).addTo(map);
});
map.fitBounds(L.featureGroup(edges.map(e => e.layer)).getBounds());

// All picking is "nearest edge to the cursor" within SNAP_PX screen
// pixels, computed against a per-zoom cache of projected points.
// Draw order and line thickness never decide what a click hits, so
// metre-long slivers are selectable; the hovered edge is thickened
// and captioned, and a click changes exactly that edge.
const SNAP_PX = 20;
let screenPts = null;
map.on("zoomend", function () { screenPts = null; });
function ensurePts() {
  if (screenPts) return;
  screenPts = edges.map(function (e) {
    return e.coords.map(function (c) {
      return map.latLngToLayerPoint(c);
    });
  });
}
function distToSegment(p, a, b) {
  const dx = b.x - a.x, dy = b.y - a.y;
  const l2 = dx * dx + dy * dy;
  let t = l2 ? ((p.x - a.x) * dx + (p.y - a.y) * dy) / l2 : 0;
  t = Math.max(0, Math.min(1, t));
  return Math.hypot(p.x - (a.x + t * dx), p.y - (a.y + t * dy));
}
function nearestEdge(pt, maxPx) {
  ensurePts();
  let best = -1, bestD = maxPx;
  for (let i = 0; i < edges.length; i++) {
    const pts = screenPts[i];
    for (let k = 0; k + 1 < pts.length; k++) {
      const d = distToSegment(pt, pts[k], pts[k + 1]);
      if (d < bestD) { bestD = d; best = i; }
    }
  }
  return best;
}

const hoverTip = L.tooltip({direction: "top", offset: [0, -8]});
function setHover(i, latlng) {
  if (i >= 0 && latlng) hoverTip.setLatLng(latlng);
  if (i === hoverI) return;
  const prev = hoverI;
  hoverI = i;
  if (prev >= 0) edges[prev].layer.setStyle(styleFor(edges[prev], prev));
  if (i >= 0) {
    edges[i].layer.setStyle(styleFor(edges[i], i));
    hoverTip.setContent(tip(edges[i]));
    if (!map.hasLayer(hoverTip)) hoverTip.addTo(map);
  } else if (map.hasLayer(hoverTip)) {
    map.removeLayer(hoverTip);
  }
}
map.on("mousemove", function (ev) {
  setHover(nearestEdge(
    map.containerPointToLayerPoint(ev.containerPoint), SNAP_PX),
    ev.latlng);
});
map.on("mouseout", function () { setHover(-1, null); });
map.on("click", function (ev) {
  const i = nearestEdge(
    map.containerPointToLayerPoint(ev.containerPoint), SNAP_PX);
  if (i >= 0) clickEdge(i, ev.originalEvent);
});

// right-click anywhere: set the round's start/end, or copy the point
map.on("contextmenu", function (ev) {
  ev.originalEvent.preventDefault();   // else the browser menu covers us
  const ll = [ev.latlng.lat, ev.latlng.lng];
  const txt = ll[0].toFixed(7) + "," + ll[1].toFixed(7);
  L.popup().setLatLng(ev.latlng).setContent(
    "<b>" + txt + "</b><br>" +
    '<button data-ep="start">set as START</button> ' +
    '<button data-ep="end">set as END</button><br>' +
    '<button data-copy="' + txt + '">copy lat,lon</button> ' +
    '<button data-copy="--start=' + txt + '">--start=</button> ' +
    '<button data-copy="--end=' + txt + '">--end=</button>'
  ).openOn(map);
  pending = ll;
});

let pending = null;
let endpoints = payload.endpoints || {};
const epMarkers = {};

function drawEndpoints() {
  ["start", "end"].forEach(function (k) {
    if (epMarkers[k]) { map.removeLayer(epMarkers[k]); delete epMarkers[k]; }
    const ll = endpoints[k];
    document.getElementById("ep" + k).textContent = k + ": " +
      (ll ? ll[0].toFixed(5) + ", " + ll[1].toFixed(5) : "not set");
    if (!ll) return;
    epMarkers[k] = L.circleMarker(ll, {
      radius: 9, weight: 3, color: k === "start" ? "#2f855a" : "#9b2c2c",
      fillColor: k === "start" ? "#48bb78" : "#f56565", fillOpacity: 1
    }).addTo(map).bindTooltip(k.toUpperCase() + " of the round");
  });
  document.getElementById("eprts").checked = !!endpoints.return_to_start;
}

async function saveEndpoints() {
  if (!SERVE) {
    alert("Setting endpoints needs the server: run\n" +
          "  make_editor.py --data data --serve");
    return;
  }
  const resp = await fetch("/endpoints", {method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(endpoints)});
  if (!resp.ok) { alert("could not save endpoints: " + await resp.text()); return; }
  endpoints = await resp.json();
  drawEndpoints();
}

document.addEventListener("click", function (ev) {
  const copy = ev.target.closest("button[data-copy]");
  if (copy) {
    if (navigator.clipboard) navigator.clipboard.writeText(copy.dataset.copy);
    copy.textContent = "copied!";
    return;
  }
  const ep = ev.target.closest("button[data-ep]");
  if (ep && pending) {
    endpoints[ep.dataset.ep] = pending;
    map.closePopup();
    saveEndpoints();
    return;
  }
  const clr = ev.target.closest("button[data-clear]");
  if (clr) {
    delete endpoints[clr.dataset.clear];
    saveEndpoints();
  }
});
document.getElementById("eprts").addEventListener("change", function () {
  endpoints.return_to_start = this.checked;
  saveEndpoints();
});

async function loadHistory() {
  if (!SERVE) return;
  let rows;
  try {
    rows = await (await fetch("/history")).json();
  } catch (err) { return; }
  const el = document.getElementById("history");
  if (!rows.length) { el.innerHTML = ""; return; }
  let prev = null;
  el.innerHTML = "<b>versions</b> (newest last)<br>" +
    rows.map(function (r) {
      const t = parseFloat(r.total_km);
      const d = prev === null ? "" :
        ' <span style="color:' + (t <= prev ? "#2f855a" : "#9b2c2c") +
        '">' + (t - prev >= 0 ? "+" : "") + (t - prev).toFixed(2) + "</span>";
      prev = t;
      return '<span style="font-family:monospace">' + r.id.slice(4) +
        "</span> " + parseFloat(r.mandatory_km).toFixed(2) + "+" +
        parseFloat(r.deadhead_km).toFixed(2) + " = <b>" + t.toFixed(2) +
        " km</b>" + d + (r.note ? " &middot; " + esc(r.note) : "");
    }).join("<br>");
}

document.getElementById("solve").onclick = async function () {
  if (!SERVE) {
    alert("Solving from the page needs the server: run\n" +
          "  make_editor.py --data data --serve");
    return;
  }
  if (edges.some(e => e.service !== e.baseline)) {
    if (!confirm("You have unsaved edits. Solve the SAVED version?")) return;
  }
  const msg = document.getElementById("solvemsg");
  const out = document.getElementById("solveout");
  this.disabled = true;
  msg.textContent = " solving ...";
  try {
    const resp = await fetch("/solve", {method: "POST",
      body: document.getElementById("note").value});
    const text = await resp.text();
    out.style.display = "block";
    out.textContent = text;
    msg.innerHTML = resp.ok
      ? ' <a href="/route_map.html" target="_blank">open route map</a>'
      : " failed";
    if (resp.ok) loadHistory();
  } catch (err) {
    msg.textContent = " failed: " + err;
  } finally {
    this.disabled = false;
  }
};

function apply(i, svc) {
  const e = edges[i];
  e.service = svc;
  e.layer.setStyle(styleFor(e, i));
  if (i === hoverI) hoverTip.setContent(tip(e));
}
function refresh() {
  const n = {0: 0, 1: 0, 2: 0, x: 0};
  let km = 0, dirty = 0;
  edges.forEach(function (e) {
    n[e.service] += 1;
    km += (parseFloat(e.row.length_m) || 0) *
          (e.service === "x" ? 0 : e.service);
    if (e.service !== e.baseline) dirty += 1;
  });
  document.getElementById("stats").innerHTML =
    edges.length + " edges &middot; mandatory <b>" +
    (km / 1000).toFixed(2) + " km</b><br>" +
    "2: " + n[2] + " &nbsp; 1: " + n[1] + " &nbsp; 0: " + n[0] +
    " &nbsp; x: " + n.x;
  document.getElementById("dirty").textContent =
    dirty ? dirty + " unsaved" : "";
  if (dirty) document.getElementById("savemsg").textContent = "";
  if (islandMode) {
    computeIslands();
    edges.forEach(function (e, i) { e.layer.setStyle(styleFor(e, i)); });
    renderIslandList();
  }
  document.getElementById("undo").disabled = !undoStack.length;
  ["bulk2", "bulk1", "bulk0", "bulkx"].forEach(function (id) {
    document.getElementById(id).disabled = !matched.size;
  });
}

document.getElementById("search").addEventListener("input", function () {
  const q = this.value.trim().toLowerCase();
  matched = new Set();
  if (q) {
    edges.forEach(function (e, i) {
      if ((e.row.name || "").toLowerCase().includes(q) ||
          (e.row.highway || "").toLowerCase().includes(q)) matched.add(i);
    });
  }
  edges.forEach(function (e, i) { e.layer.setStyle(styleFor(e, i)); });
  document.getElementById("nmatch").textContent =
    q ? matched.size + " matched" : "";
  refresh();
});

function bulk(svc) {
  const acts = [];
  matched.forEach(function (i) {
    if (edges[i].service !== svc) acts.push([i, edges[i].service, svc]);
  });
  if (!acts.length) return;
  undoStack.push(acts);
  acts.forEach(function (a) { apply(a[0], a[2]); });
  refresh();
}
document.getElementById("bulk2").onclick = function () { bulk(2); };
document.getElementById("bulk1").onclick = function () { bulk(1); };
document.getElementById("bulk0").onclick = function () { bulk(0); };
document.getElementById("bulkx").onclick = function () { bulk("x"); };

document.getElementById("undo").onclick = function () {
  const acts = undoStack.pop();
  if (!acts) return;
  acts.slice().reverse().forEach(function (a) { apply(a[0], a[1]); });
  refresh();
};

function quote(v) {
  v = String(v == null ? "" : v);
  return /[",\r\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
}
function buildCsv() {
  const lines = [payload.header.map(quote).join(",")];
  edges.forEach(function (e) {
    lines.push(payload.header.map(function (h) {
      return quote(h === "service" ? e.service : e.row[h]);
    }).join(","));
  });
  return "\ufeff" + lines.join("\r\n") + "\r\n";
}
async function exportCsv() {
  const text = buildCsv();
  if (SERVE) {
    try {
      const resp = await fetch("/save", {method: "POST",
        headers: {"Content-Type": "text/csv"}, body: text});
      if (!resp.ok) {
        alert("save failed: " + await resp.text());
        return;
      }
    } catch (err) {
      alert("save failed: " + err);
      return;
    }
  } else if (window.showSaveFilePicker) {
    try {
      const handle = await showSaveFilePicker({
        suggestedName: "edges.csv",
        types: [{description: "CSV", accept: {"text/csv": [".csv"]}}],
      });
      const w = await handle.createWritable();
      await w.write(text);
      await w.close();
    } catch (err) {
      if (err && err.name === "AbortError") return;  // user cancelled
      download(text);
    }
  } else {
    download(text);
  }
  edges.forEach(function (e) { e.baseline = e.service; });
  refresh();
  document.getElementById("savemsg").textContent =
    (SERVE ? "saved " : "exported ") + new Date().toLocaleTimeString();
}
function download(text) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], {type: "text/csv;charset=utf-8"}));
  a.download = "edges.csv";
  a.click();
  URL.revokeObjectURL(a.href);
}
document.getElementById("export").onclick = exportCsv;

window.addEventListener("beforeunload", function (ev) {
  if (edges.some(e => e.service !== e.baseline)) ev.preventDefault();
});

drawEndpoints();
loadHistory();
refresh();
</script>
</body>
</html>
"""


def build_html(data_dir: Path):
    """Fresh HTML from the CSVs as they are on disk right now."""
    header, edges = load(data_dir)
    payload = json.dumps({"header": header, "edges": edges,
                          "endpoints": load_endpoints(data_dir)},
                         ensure_ascii=False).replace("</", "<\\/")
    return TEMPLATE.replace("__PAYLOAD__", payload), edges


def summarise(edges):
    n = {0: 0, 1: 0, 2: 0, "x": 0}
    for e in edges:
        n[e["service"]] += 1
    return (f"{len(edges)} edges | service 2: {n[2]}, 1: {n[1]}, "
            f"0: {n[0]}, x: {n['x']}")


def save_csv(data_dir: Path, text: str) -> str:
    """Validate an edited edges.csv posted by the browser and write it
    back atomically.  Only `service` may change: header, edge_ids and
    row order must match the file on disk.  Raises ValueError."""
    reader = csv.DictReader(io.StringIO(text))
    new_header = list(reader.fieldnames or [])
    rows = list(reader)

    with open(data_dir / "edges.csv", newline="",
              encoding="utf-8-sig") as f:
        cur = csv.DictReader(f)
        cur_header = list(cur.fieldnames)
        cur_ids = [r["edge_id"] for r in cur]

    if new_header != cur_header:
        raise ValueError("header mismatch -- refusing to save")
    if [r["edge_id"] for r in rows] != cur_ids:
        raise ValueError("edge_id list mismatch -- refusing to save "
                         "(edges.csv changed on disk? reload the page)")
    for r in rows:
        if (r.get("service") or "").strip().lower() not in \
                ("0", "1", "2", "x"):
            raise ValueError(f"bad service value on edge {r['edge_id']}")

    target = data_dir / "edges.csv"
    shutil.copy2(target, data_dir / "edges.csv.bak")
    tmp = data_dir / "edges.csv.tmp"
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cur_header)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, target)

    n = {"0": 0, "1": 0, "2": 0, "x": 0}
    for r in rows:
        n[r["service"].strip().lower()] += 1
    return (f"saved: {len(rows)} edges | "
            f"2: {n['2']}, 1: {n['1']}, 0: {n['0']}, x: {n['x']}")


def run_solver(data_dir: Path, out_dir: Path, history_dir: Path,
               note: str = ""):
    """Run solve_route.py on the saved endpoints. Returns (ok, output)."""
    ep = load_endpoints(data_dir)
    cmd = [sys.executable, str(BASE / "solve_route.py"),
           "--data", str(data_dir), "--out", str(out_dir),
           "--history", str(history_dir)]
    if note:
        cmd += ["--note", note]
    if ep.get("start"):
        cmd.append("--start={},{}".format(*ep["start"]))
    if ep.get("end"):
        cmd.append("--end={},{}".format(*ep["end"]))
        if ep.get("return_to_start"):
            cmd.append("--return-to-start")
    res = subprocess.run(cmd, capture_output=True, text=True)
    return res.returncode == 0, (res.stdout + res.stderr).strip()


def serve(data_dir: Path, port: int, out_dir: Path, history_dir: Path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code, body, ctype="text/plain; charset=utf-8"):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/editor.html", "/index.html"):
                html, _ = build_html(data_dir)
                self._reply(200, html, "text/html; charset=utf-8")
            elif path == "/history":
                import round_history
                rows = round_history.read_index(history_dir)
                self._reply(200, json.dumps(rows[-8:]),
                            "application/json; charset=utf-8")
            elif path == "/route_map.html":
                target = out_dir / "route_map.html"
                if not target.exists():
                    self._reply(404, "No route yet -- press Solve first.")
                    return
                self._reply(200, target.read_bytes(),
                            "text/html; charset=utf-8")
            else:
                self.send_error(404)

        def do_POST(self):
            if self.path == "/save":
                length = int(self.headers.get("Content-Length") or 0)
                text = self.rfile.read(length).decode("utf-8-sig")
                try:
                    msg = save_csv(data_dir, text)
                except ValueError as e:
                    self._reply(400, str(e))
                else:
                    print(f"  {msg}", flush=True)
                    self._reply(200, msg)
            elif self.path == "/endpoints":
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8")
                try:
                    saved = save_endpoints(data_dir, json.loads(raw))
                except ValueError as e:
                    self._reply(400, str(e))
                else:
                    print(f"  endpoints saved: "
                          f"start={saved.get('start')} "
                          f"end={saved.get('end')} "
                          f"return={saved['return_to_start']}", flush=True)
                    self._reply(200, json.dumps(saved),
                                "application/json; charset=utf-8")
            elif self.path == "/solve":
                length = int(self.headers.get("Content-Length") or 0)
                note = self.rfile.read(length).decode("utf-8").strip()
                print("  solving ...", flush=True)
                ok, output = run_solver(data_dir, out_dir, history_dir,
                                        note)
                print(f"  solve {'ok' if ok else 'FAILED'}", flush=True)
                self._reply(200 if ok else 400, output)
            else:
                self.send_error(404)

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    _, actual_port = httpd.server_address[:2]
    _, edges = build_html(data_dir)  # fail fast on bad data
    print(f"Editing {data_dir / 'edges.csv'}   ({summarise(edges)})",
          flush=True)
    print(f"Serving editor at http://127.0.0.1:{actual_port}/  "
          f"(Ctrl-C to stop)", flush=True)
    print("Every page reload shows the file as it is on disk; "
          "Save writes straight back (backup: edges.csv.bak).",
          flush=True)
    print("Right-click the map to set the route's start/end, then "
          f"press Solve (results in {out_dir}/).", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", default="data",
                    help="directory with edges.csv + nodes.csv")
    ap.add_argument("--serve", action="store_true",
                    help="serve the editor on localhost with direct "
                         "save-back instead of writing a static file")
    ap.add_argument("--port", type=int, default=8765,
                    help="port for --serve (0 = pick a free one)")
    ap.add_argument("--out", default="result",
                    help="where --serve writes solver results")
    ap.add_argument("--history", default=None, metavar="DIR",
                    help="where --serve files solved versions "
                         f"(default {DEFAULT_HISTORY})")
    args = ap.parse_args()
    data_dir = Path(args.data)

    if args.serve:
        serve(data_dir, args.port, Path(args.out),
              Path(args.history or DEFAULT_HISTORY))
        return

    html, edges = build_html(data_dir)
    out = data_dir / "editor.html"
    out.write_text(html, encoding="utf-8")
    print(f"""
Editor written: {out}   ({summarise(edges)})

Open it in a browser:
  * click an edge to cycle 2 -> 1 -> 0   (ctrl-click = straight to 0)
  * search a street name or road type to bulk-set all matches at once
  * Export downloads the updated edges.csv -> replace {data_dir}/edges.csv
    with it, then re-run make_editor.py (the page is a snapshot) and
    solve_route.py

Tip: `--serve` avoids the export/replace dance entirely -- the page is
rebuilt from disk on every reload and Save writes straight back.""")


if __name__ == "__main__":
    main()
