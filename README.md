# Postal Round Route Optimizer

Computes the shortest route that covers every street of a delivery round —
a real-world instance of the **Rural Postman Problem** (the
territory-restricted generalisation of the classic Chinese Postman
Problem), solved exactly, on real OpenStreetMap data.

Born on a real postal round ridden on an electric delivery vehicle; works
for any area on Earth that OSM covers — delivery rounds, leaflet drops,
street sweeps, "run every street" challenges.

## How it works

```
OpenStreetMap ──► extract_network.py ──► edges.csv  (you edit `service`)
                                         nodes.csv
                                         preview.html
edges.csv (edited) ──► solve_route.py ──► route.csv      (ordered street list
                                          route_map.html  = new sort sequence)
```

The single source of truth is the `service` column of `edges.csv`:

| value | meaning | traversals |
|------:|---------|-----------|
| `2` | I deliver **both sides** of this street (default) | 2 — one per side |
| `1` | I deliver **one side only**, *or* I zigzag both sides in a single pass | 1 |
| `0` | **Not my street**, but I may ride along it as a shortcut | 0 required (free deadhead) |
| `x` | **Never use** — steps, unrideable paths, hostile crossings | edge removed from the graph (not even deadhead) |

This one mechanism covers every customisation a real round needs:
adding/removing streets from your territory, one-side-only boundary
roads (e.g. divided arterials where you serve a single carriageway), and
per-street zigzag decisions. Boundaries that cut a street mid-block are
handled by the edge-split utility (see `docs/DEVELOPMENT.md`, SPEC-1).

## Quick start (demo area)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 1. Extract a demo network (needs internet; ~10 s)
python extract_network.py --place "Albert Park, Victoria, Australia" --out data

# 2. Open data/preview.html in a browser and data/edges.csv in a
#    spreadsheet. Edit the service column (sorted by street name, so a
#    whole street can be set in one block).

# 3. Solve
python solve_route.py --data data --out result
```

Then open `result/route_map.html` — an interactive viewer: drag the
slider (or press ←/→, or hit play) to walk the route step by step;
the current segment is highlighted with a direction arrow, service
passes are solid, deadhead dashed. `result/route.csv` is the same
route as an ordered street list — each row gives street, pass number,
compass direction, the cross streets at each end, and cumulative
distance.

## Editing the service column graphically

Editing hundreds of rows by edge_id is tedious — run the click-editor
instead:

```bash
python make_editor.py --data data --serve
```

Open the printed URL (default `http://127.0.0.1:8765/`): every edge
is drawn over a
basemap, coloured by service (blue 2 / orange 1 / grey dashed 0).
Click to cycle 2 → 1 → 0 (ctrl-click sets 0 directly — the common
"not my street" case; shift-click toggles `x` = banned, e.g. for
steps). For long stretches of the same value, switch **click sets**
from `cycle` to a fixed value and just paint. Picking always targets
the edge nearest to
the cursor — the hovered edge is thickened and captioned first, so
you can see what a click will change, and zoom reaches building
level: even metre-long slivers of split streets are selectable.
Type a street name or a road type (`trunk`, `steps`, …) to bulk-set
all matched segments at once. Live counters show edges per value, mandatory km
and unsaved changes; undo works across bulk operations.

**Right-click** anywhere on the map to set that point as the round's
**START** or **END** (or just copy the coordinates). The endpoints are
stored in `data/endpoints.json`, drawn as markers, and reused on every
reload; tick *return to start* for a closed tour. Then press
**Solve route** — the solver runs and the finished route map opens in
a new tab. The whole loop stays in the browser; `solve_route.py` picks
the same endpoints up automatically when run without `--start`/`--end`.

**show islands** colours each connected component of your service
streets separately (with per-island zoom buttons) — one island means
the solver's optimum is exact; stray extra islands are usually
annotation gaps, easiest to spot and fix right there.

**Save** writes straight back to `data/edges.csv` (all columns
preserved, only `service` changed; the previous version is kept as
`edges.csv.bak`) — then just re-run `solve_route.py`. Every page
reload shows the file as it currently is on disk, so the editor is
never stale. The server binds localhost only; your round never
leaves your machine.

Without `--serve`, `make_editor.py` writes a static
`data/editor.html` snapshot whose **Export** button downloads an
edges.csv to copy over `data/edges.csv` manually — workable, but you
must re-run `make_editor.py` after replacing the file, or the page
keeps showing the old embedded data.

## Running it on your own round (privacy)

Your edited `edges.csv` **is** your round definition — treat it as
private data. This repository is set up so that nothing personal ever
needs to be committed:

- `data/`, `result/`, `*.geojson` and `*.gpx` are gitignored. Your real
  place names, polygons, service edits and any GPS traces stay on your
  machine only.
- All tracked examples, tests and docs use the demo area or synthetic
  coordinates. Keep it that way in contributions.
- Before your first push, grep the repo for your own round's place
  names as a final check.

## Options

```bash
# Open route: start and end may differ; the solver picks the endpoint
# pair that minimises total distance (never longer than the circuit)
python solve_route.py --data data --out result --open

# Pin the start (snapped to the nearest service-street node).
# Note the equals sign -- southern latitudes are negative:
python solve_route.py --data data --out result --start=-37.8406,144.9541

# Fixed start AND end (e.g. depot -> round -> parcel-handover office):
# jointly optimised open route via a zero-length virtual required edge.
# Both points snap to the full network, so they may sit on service-0
# corridor streets. --end alone pins just the finish.
python solve_route.py --data data --out result \
    --start=-37.8406,144.9541 --end=-37.8380,144.9520

# Closed tour: same, plus the shortest ride from the end back to the
# start (depot -> round -> handover office -> depot). The office visit
# is last-before-home, so the return leg is a constant and joint
# optimality is preserved.
python solve_route.py --data data --out result \
    --start=-37.8406,144.9541 --end=-37.8380,144.9520 --return-to-start

# Tip: right-click in the editor (make_editor.py --serve) to copy any
# point as a ready-made --start=/--end= argument; the extraction
# preview shows coordinates on click too.

# Use an exact hand-drawn boundary instead of place names:
# draw a polygon at https://geojson.io, save it, then
python extract_network.py --polygon round.geojson --out data

# A delivery boundary that cuts a street mid-block (house-number
# cutoff): split the edge at that point, then set the outer child's
# service to 0. Snaps to the intersection if you click near one.
python split_edge.py --data data --at=-37.8450,144.9505

# Network type (default `all`: footpaths AND carriageways). `walk`
# looks right for a footpath vehicle, but osmnx drops the roadway
# pieces inside big junctions, so "straight through the intersection"
# stops existing and routes detour over pedestrian crossings -- on a
# real round that cost 1.25 km. With `all`, set ways you cannot ride
# (motorway, steps, corridor) to x.
python extract_network.py --place ... --network-type walk --out data
```

## Re-extracting without losing your annotation

Re-running the extraction regenerates `edges.csv` and would wipe every
`service` edit and split. When you need a bigger area (say, to bring a
depot or a handover office into the network), snapshot first:

```bash
# 1. freeze the current annotation into the gitignored round dir
python prepare_round.py --data data --round round.local --export

# 2. enlarge your polygon (geojson.io), then re-extract; default 0
#    makes the newly added streets arrive as connectors, not service
python extract_network.py --polygon round.local/my_round.geojson \
    --default-service 0 --out data

# 3. replay splits + service values onto the fresh extraction
python prepare_round.py --data data --round round.local
```

Edges that are new to the enlarged area keep the extraction default
and are reported — review them in the editor.

`edge_id`s come from OSM and are stable while the map is, but changing
`--network-type` changes which junctions exist, so osmnx splits ways
differently and some ids disappear (one old edge often becomes two or
three new ones). Overrides that match no id are therefore retried **by
geometry**: a new edge lying along the old one, with the same name and
road type, inherits its value. Anything that still cannot be placed is
listed for you to re-mark — never silently dropped.

## Version history — did that edit actually help?

Add `--history` to any solve and the run is filed away: the round
definition at that moment, the route it produced, and the totals.

```bash
python solve_route.py --data data --out result --history \
    --note "after fixing the sliver gaps"

python round_history.py list          # every version, newest last
python round_history.py diff          # what changed between the last two
python round_history.py diff 2026 -1  # by id prefix or negative index
python round_history.py show -1       # one version in full
```

`list` shows service / total / deadhead km and the island count per
version, with the change in total distance since the previous one.
`diff` names the streets added to or dropped from the round and the
resulting distance deltas — so "I marked 19 slivers, did the route get
shorter?" has an answer instead of a feeling.

Versions live in `round.local/history/<id>/` (gitignored), each with
`service.csv`, `summary.json`, `route.csv` and a copy of
`route_map.html` you can still open months later. Re-solving an
unchanged round does not create a duplicate. The editor's **Solve
route** button records versions automatically and lists the recent
ones under the button.

## Reading the result

The console summary separates what optimisation can and cannot change:

```
Mandatory service riding :   18.42 km   (theoretical lower bound)
Route total              :   19.87 km
Extra / deadhead         :    1.45 km   (7.3% of route)
```

The mandatory part is the job itself — every service street times its
multiplicity. The **extra** is the only optimisable quantity, and the
algorithm guarantees it is minimal **when the service streets form one
connected piece**. If they do not, the solver bridges the islands with
a minimum spanning tree, warns, and reports how many kilometres that
bridging accounts for — that figure is the only part of the answer
that is not provably minimal. Use the editor's *show islands* view to
merge stray islands and get back to an exact result; see
`docs/DEVELOPMENT.md` (E3) for why optimal island connection is a
harder problem.

`route.csv` doubles as the basis for a new **sort sequence**: mail is
bundled in row order; a `2/2` pass is the same street's other side.

## Testing

```bash
python test_solve.py
python test_editor.py
python test_split.py
python test_prepare.py
python test_history.py
```

Runs the solver on a synthetic street grid (no internet needed) and
asserts: exact service multiplicities, walk continuity, circuit closure,
open ≤ circuit, correct auto-bridging of disconnected service sets, and
exclusion of `service=x` edges. `test_editor.py` covers the editor:
payload round-trip, hostile street names, WKT parsing, blank/bad
service handling, and the `--serve` save-back loop. `test_split.py`
covers the edge splitter: mid-edge splits, intersection snapping,
mis-click guard, composition, and solver invariants on split data.

## Development

Start with `docs/DEVELOPMENT.md` — requirements, data contracts, the
edge-case catalogue, per-version specs (V1 → V3) and the decision log.
