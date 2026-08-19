# DEVELOPMENT — requirements, edge cases, specs, decisions

This is the working document for the project. It records what we are
building, the edge cases discovered so far, implementation-ready specs
for the next tasks, and why past decisions were made. Update it as the
project evolves — especially the decision log.

---

## 0. Status snapshot

| Milestone | State |
|---|---|
| V1 core: extract → edit service → exact solve → route.csv + map | **done, tested** (`test_solve.py`) |
| Graphical service editor (`make_editor.py`, lean cut of B5) | **done, tested** (`test_editor.py`) |
| Excluded edges (`service=x`) across solver + editor | **done, tested** |
| SPEC-1 edge-split utility (`split_edge.py`, mid-block boundaries) | **done, tested** (`test_split.py`) — **V1 complete** |
| V2 / SPEC-2: fixed endpoints (`--start` + `--end`, depot → round → handover office) | **done, tested** (in `test_solve.py`) |
| B1: reproducible rounds (`prepare_round.py` export/apply) | **done, tested** (`test_prepare.py`) — **V2 complete** |
| V2.2 graphical endpoints + in-browser solving | **done, tested** |
| B9: solved-version history (`round_history.py`) | **done, tested** (`test_history.py`) |
| E15: extraction switched to `--network-type all` (roads actually connected) | **done** |
| B2 cost profiles (`--profile edv`) + B10.1/B10.2 wrong-way flag and penalty | **done, tested** (in `test_solve.py`) |
| E16: one-way direction preserved through extraction | **done, tested** (`test_extract.py`) |
| B11: turn-aware Euler tour + rough riding time / turn counts | **done, tested** (in `test_solve.py`) |
| B12: posted speed limits in the data + `limits` profile + release-numbered history | **done, tested** |
| B3 pass pairing (`--pair-passes`) + operational metrics in history | **done, tested** — pairing off by default, see B3 |
| B13: turn-aware routing (line graph) + `--variants` route preferences | **done, tested** (in `test_solve.py`) |
| V3+: real time costs, pass pairing, GPX compare, per-letterbox sequencing, exact ILP | backlog |

---

## 1. Problem statement

A delivery round must traverse a set of streets: some on both sides
(twice), some on one side (once), while other streets are merely
available as shortcuts. Find the shortest closed or open route that
performs all required traversals.

This is the **Rural Postman Problem** with edge multiplicities. Key
theory facts that shape the design:

- It generalises the Chinese Postman Problem (all edges required).
- RPP is NP-hard **in general**, but when the required edges form one
  connected component it is solved exactly in polynomial time by:
  odd-node parity repair via minimum-weight perfect matching, with
  pair distances taken as shortest paths over the *full* network.
- If every street were multiplicity 2, the required graph would already
  be Eulerian and any sweep would be optimal. The handful of
  multiplicity-1 streets is precisely what creates odd-degree nodes and
  makes optimisation non-trivial.
- The mandatory distance Σ(length × service) is a hard lower bound; the
  matching cost is the only optimisable quantity. Report both.

Not a TSP: we cover edges, not visit points.

## 2. Requirements

Functional:

- **R1** Cover every service street with its exact multiplicity while
  minimising total distance; exact optimum whenever required edges are
  connected.
- **R2** Territory is defined by an editable per-edge `service` value
  in {0,1,2,x}, fully independent of administrative boundaries
  (`x` = excluded: the edge is dropped from the graph at load and can
  never be routed through, not even as deadhead).
- **R3** Non-service streets remain usable as deadhead shortcuts.
- **R4** Modes: closed circuit (default); free-endpoint open path
  (`--open`); start pinning (`--start=lat,lon`, snapped).
- **R5** Outputs: ordered `route.csv` usable as the basis of a mail
  sort sequence (street, pass x/y, direction, cross streets, cumulative
  distance); `route_map.html` coloured by order with deadhead dashed;
  console stats separating mandatory vs extra distance.
- **R6** Boundaries that cut a street mid-block (a house-number cutoff
  between intersections) are supported via edge splitting → SPEC-1. A
  cutoff that coincides with an intersection needs no split — whole-
  segment service values already express it.
- **R7** (V2) Fixed start **and** end: the route may finish at a
  location distinct from the start (e.g. a final call at an office),
  jointly optimised → SPEC-2.
- **R8** The solver and all tests run offline on synthetic data; only
  extraction touches the network.

Non-functional:

- **NF1 Privacy separation.** Public repo = generic tool + demo area
  only. The maintainer's real round (names, polygons, edited CSVs, GPX)
  lives exclusively in gitignored local files. See CLAUDE.md.
- **NF2 Honest optimality claims.** Exactness is stated only under its
  true conditions; degraded modes must warn.
- **NF3 Cost abstraction.** *Realised 2026-08.* Optimisation reads
  `cost` (on the directed graph `D`), reporting reads `length` (real
  metres). `--profile distance` makes them equal, so the historic
  behaviour is the default and every printed kilometre stays a real
  kilometre whatever the profile.
- **NF4** String node ids; utf-8-sig CSVs; no meaning parsed from
  `edge_id`.

## 3. Data contracts

Schemas (all CSV utf-8-sig, Excel-friendly; **node ids are strings
everywhere**):

- `edges.csv`:
  `edge_id,name,highway,oneway,maxspeed_kmh,length_m,service,note,u,v,geometry_wkt`
  (`service` ∈ {0,1,2,x}; `oneway` steers nothing by default — the
  model is undirected — but feeds the `against_oneway` flag and the
  optional `--wrong-way-penalty`, see E13 / B10)
- `nodes.csv`: `node_id,lat,lon`
- `route.csv`:
  `seq,type,street,pass,direction,from_cross,to_cross,length_m,cum_km,against_oneway,edge_id`

`edge_id` is opaque to the solver — never parse meaning out of it.
Contract changes require: updating this section, a migration note
below, and test updates in the same commit. (The untracked local
CLAUDE.md mirrors these contracts for AI sessions — keep it in sync.)

Migration notes:

- 2026-08: `service` gained the value `x` (excluded — the edge is
  dropped from the graph at load). Files without any `x` behave
  exactly as before; no migration needed.
- 2026-08: `edges.csv` gained `maxspeed_kmh` (posted limit in km/h,
  empty when OSM says nothing usable; "40 mph" and "walk" normalised,
  "AU:urban"/"signals" dropped). Files without the column still load --
  the solver treats a missing limit as unknown and falls back to the
  profile's per-type speed. Coverage on the real round: 30% of edges,
  but 0% of the 5 890 footway/path edges, which is why footpath speed
  stays a property of the profile, never of the map.
- 2026-08: `route.csv` gained `against_oneway` (second-to-last column,
  before `edge_id`): `yes` when that traversal runs against a one-way,
  else empty. Purely informational — the model stays undirected.
  Anything reading `route.csv` by column *index* past `cum_km` must be
  updated; reading by header name is unaffected.

## 4. Edge-case catalogue

- **E1 Divided arterials.** OSM maps a divided main road as two
  parallel one-way chains. Serving "one side" of such a road means:
  the near chain gets `service=1`, the far chain `0`. The mapping also
  encodes reality for free: the chains interconnect only at
  intersections/crossings, so deadheading across the median is only
  possible where crossing is actually possible. Verify on the preview
  map (two thin parallel lines a few metres apart).
- **E2 Mid-block boundary.** Responsibility ends at a house number
  between two intersections → whole-segment granularity is wrong by up
  to a block. Resolved by SPEC-1 splitting. When the split point lands
  within the snap threshold of an intersection, no split is performed
  (the intersection-cutoff case).
- **E3 Disconnected service islands.** If service streets form several
  components (e.g. a detached pocket), the solver bridges them and
  drops the exactness claim, warning with the island count and the
  bridging kilometres — the only part of the answer that is not
  provably minimal. The bridging grows one component by always
  attaching the nearest other one, i.e. **Prim's algorithm**, so it is
  an optimal *minimum spanning tree* over component-to-component
  shortest paths (verified on real data: identical to an independently
  computed MST). It is still not the RPP optimum: a Steiner-style
  branch off an existing bridge can be cheaper, and the parity
  matching sometimes supplies connectivity for free. Closing that last
  gap needs a different algorithm class (ILP with connectivity
  constraints, branch-and-cut) — deliberately out of scope; see B8.
  **In practice most islands are annotation gaps, not geography**: on
  the maintainer's round the "islands" sat 12 m, 26 m and 114 m apart,
  i.e. one missing footway sliver each. Check with the editor's island
  view before blaming the algorithm. Note that pinned `--start`/`--end`
  outside the service area always form one extra component, so the
  warning is expected in that mode; the message names it separately.
- **E4 Dead ends and courts.** A cul-de-sac edge with `service=2`
  naturally means ride in along one side, out along the other. Loop
  bulbs may appear as self-loop edges; the Euler machinery handles
  them.
- **E5 Non-adjacent passes.** The Euler traversal may do pass 1/2 of a
  street, loop the block, and return for 2/2 later. Often this *is*
  the optimum, and because the sort sequence follows the route it is
  operationally fine. A "prefer paired passes" soft constraint is a
  V3 idea, not a bug.
- **E6 OSM noise.** `walk` networks include footways, alleys, and the
  occasional flight of steps. The preview map colour-codes road types;
  the user zeroes what they don't ride. Steps must always be `x`
  (excluded — before `x` existed the advice was 0, which still let the
  solver deadhead through them). Ground truth (the person who rides
  the round) beats the map.
- **E7 Boundary clipping.** Roads on the territory edge are kept via
  polygon buffering (`--buffer`, default ~60 m) and
  `truncate_by_edge=True`. If a boundary road is still missing,
  enlarge the buffer or switch to a hand-drawn `--polygon`.
- **E8 Re-extraction wipes manual edits.** `service` edits and splits
  live in `data/edges.csv`; re-running extraction regenerates the file.
  V1 accepts this (extract once, edit once). The V1.1 fix is a
  declarative local config — see backlog item B1.
- **E9 Geometry orientation.** An OSMnx edge's LineString direction
  does not necessarily match its (u, v) order. Any code walking or
  cutting geometry must orient first (compare endpoints against node
  coordinates; see `segment_coords()` in `solve_route.py`).
- **E10 Negative latitudes on the CLI.** Southern-hemisphere
  coordinates begin with `-`, which argparse eats as an option. All
  coordinate flags are documented and tested in the equals form.
- **E11 Unreachable required streets.** If some service streets cannot
  reach the rest even via service-0 edges, the extraction area is too
  small. Fail with that exact advice; never guess.
- **E12 OSM attribute quirks.** `name`/`highway` may be strings, lists
  or missing — normalise on extraction (`norm()`), never downstream.
- **E15 `walk` networks are not road networks.** osmnx's walk filter
  keeps ways where walking is allowed, which silently drops the
  carriageway pieces *inside* large junctions. Measured on the
  maintainer's area: the road-only subgraph of a `walk` extraction fell
  into **34 components**; at one arterial crossroads the two roads
  shared **no node**, so "straight through the intersection" did not
  exist as an edge and every route crossed on the pedestrian
  crossings. The same
  polygon with `--network-type all` gives 5 road components, 4 shared
  nodes at that junction, and a 1.25 km shorter route on an identical
  annotation. `all` is therefore the default. Its cost: ways you cannot
  ride (motorway, steps, corridor) are no longer filtered out for you —
  set them to `x`.
- **E16 `to_undirected()` destroys one-way direction.** An undirected
  networkx MultiGraph stores no direction, and `G.edges()` yields
  `(u, v)` in **node-insertion order**. So exporting straight from the
  undirected view records roughly half of all one-way edges pointing
  backwards, while the `oneway=True` flag still says "this is a
  one-way" — a silent corruption that only shows up in anything
  reasoning about direction. Measured on the real round: **1 229 of
  2 497 one-way edges (49%) were reversed**, leaving 533 nodes that
  one-way roads could enter but never leave (a real network has
  ~none). The fix is to capture `{(u, v)}` of the one-way arcs from
  the *directed* graph before collapsing it, and flip the u/v columns
  on export; `edge_id` deliberately stays keyed on the iteration order
  so existing annotations survive the re-extraction untouched.
  Covered by `test_extract.py`.
- **E13 One-way streets ("can I ride against traffic?").** The model
  is deliberately **undirected**: the vehicle works from the footpath,
  and footpaths have no direction — "riding against" a one-way only
  exists on the carriageway. The OSM `oneway` flag is exported to
  `edges.csv` as pure information (useful when reviewing a deadhead
  you might ride on-road), but the solver ignores it and **no
  directional constraint can currently be expressed**. If a real need
  appears (e.g. a laneway with no footpath that must be ridden on the
  carriageway), know the theory before committing: undirected CPP is
  polynomial (matching) and fully directed CPP is polynomial
  (min-cost flow), but the realistic *mixed* case is NP-hard — the
  upgrade is a different solver (ILP formulation), not a tweak.
  Revisit only with a concrete street in hand. *Note (2026-08): with
  `network_type=walk` osmnx marks **every** edge `oneway=False`, so the
  column carried no information at all; under the new `all` default it
  is real data (2 497 of 12 912 edges one-way on the maintainer's
  area), which is what a future wrong-way **flag** would need.*
- **E14 Separate footpath lines vs the road.** Big roads are often
  mapped with their footpaths as separate parallel `footway` lines.
  Marking the line you physically ride (usually that footway, as
  `service=1`) is correct — more accurate than marking the carriageway
  — but footway chains are fragmented and connect to the network only
  at crossings, so a missed sliver silently splits the service set
  into islands. Use the editor's island view to verify continuity
  after annotating. Ordinary streets without separate sidewalk mapping
  are marked on the street line itself. Mixing the two conventions on
  the *same* road double-counts it.

## 5. SPEC-1 — edge-split utility (shipped 2026-08)

**Goal.** Split a street segment at an arbitrary point so that a
mid-block boundary can be expressed with ordinary service values.

**CLI.**

```
python split_edge.py --data data --at=<lat,lon> [--edge EDGE_ID]
```

`--at` is typically obtained by right-clicking the boundary property in
an online map and copying the coordinates. `--edge` restricts the
search when two streets run close together (e.g. divided arterials).

**Algorithm.**

1. Load `nodes.csv`/`edges.csv`. Candidate edges: all, or the one
   given. Build each candidate's LineString from `geometry_wkt`
   (fallback: straight u→v line), **oriented to match (u, v)** (E9).
2. Choose the candidate with minimum point-to-line distance. If that
   distance exceeds ~30 m, abort with a "probably a mis-click" error
   (non-zero exit).
3. Project the point onto the line; let `d` = distance along the edge.
   If `d < SNAP` or `length − d < SNAP` (SNAP ≈ 8 m): print that the
   boundary coincides with intersection node u/v, change nothing, exit
   0. This is the intersection-cutoff case (E2).
4. Otherwise split: cut the geometry at the projection
   (`shapely.ops.substring`), create synthetic node `s<n>` (`n` = max
   existing synthetic index + 1) at the cut point, and replace the
   parent row with two child rows:
   - ids `"<parent>#a"` and `"<parent>#b"` (children of children get
     `#a#a` etc. — ids stay opaque);
   - endpoints `u → s<n>` and `s<n> → v`;
   - `length_m` recomputed per child by haversine along its coordinate
     chain (do not prorate the parent value);
   - `name`, `highway`, `service` inherited; `note` set to
     `"split from <parent> at <lat,lon>"`.
5. Append the new node to `nodes.csv`; rewrite `edges.csv` preserving
   the name sort. Print a summary and remind the user to set `service`
   on the outer child (typically to 0).

Repeated invocations must compose (each run re-reads the CSVs, so a
previously created child is just another splittable edge).

**Out of scope.** Regenerating `preview.html` (optional nicety);
address-string geocoding (`--address "143 Example Rd"` via
`ox.geocode`) is a possible convenience layer later — coordinates are
the primitive.

**Acceptance tests** (new `test_split.py`, offline, reusing the
synthetic grid from `test_solve.py`):

1. Mid-edge split: parent row gone, two children present, child
   lengths sum to parent within 1%, node added, service inherited.
2. Solver invariants still hold on the split data after setting one
   child to 0 (run the full solve; reuse `check_walk`).
3. Snap case: a point 3 m from an intersection changes nothing and
   exits 0 with the "coincides with intersection" message.
4. Mis-click guard: a point 100 m from any edge exits non-zero.
5. Split of a child (`#a`) works.

## 6. SPEC-2 — fixed endpoints and the final office leg (shipped 2026-08)

**Goal.** Support routes that must start at point A and end at point B
(e.g. finish the round at a retail office), jointly optimised.

**Reduction.** Add a virtual *required* edge (end → start) with length
0 to R, solve the ordinary closed circuit, rotate the circuit so the
virtual edge is last, delete it. What remains is the optimal open route
A → … → B. The virtual edge flips the parity of both endpoints and the
existing matching machinery does the rest — no new algorithm, exactness
preserved.

**CLI.** `--start=<lat,lon> --end=<lat,lon>`; both snapped to nodes of
the **full** graph F (the end need not lie on a service street, but
must be inside the extracted network — else fail with "extract a larger
area", E11). `--end` is mutually exclusive with `--open`. `--end`
without `--start` is allowed (circuit start free, end pinned — same
trick, the virtual edge just targets the chosen end).

**The office-outside-the-area question.** The maintainer's end point
lies outside the round's natural extraction area. Two options:

- **Option A — extend the extraction** to cover the office and a
  connecting corridor, with all added streets at `service=0`, then pin
  `--end`. Jointly optimal: the solver chooses which service street to
  finish on so the ride to the office is cheapest. Open design issue:
  extraction currently applies one `--default-service` to everything,
  so a corridor would arrive as `2` and need bulk-zeroing. Candidate
  fixes: a second extraction with `--default-service 0` plus a
  `merge_data.py` (union by `edge_id`, round rows win), or per-polygon
  defaults. Decide when building.
- **Option B — separate final leg.** Solve the round as today
  (circuit, or `--end` pinned to the boundary node nearest the office
  direction), then compute an ordinary shortest path round-end →
  office on a larger throwaway graph and report it as a separate leg.
  Simpler, decoupled, marginally suboptimal (the round's finishing
  street is not co-optimised).

Recommendation: B is an acceptable first cut; A is the proper solution
once merge tooling exists. The depot → round-start leg is the mirror
image and uses whichever mechanism B/A settles on.

**As shipped (2026-08).** `--start`+`--end` implements the virtual-edge
reduction: both points snap to F, the zero-length required edge joins R
before connectivity repair, and `traverse()` materialises the circuit,
rotates it around the virtual edge and orients the walk to begin at
the start pin. `--end` alone reuses the open-path machinery with the
pin on the finish side (orientation post-fix; if parity forces both
endpoints, the forced endpoint nearest `--end` is chosen and reported).
Endpoint pinning also fixed a latent bug: the open-mode pin previously
picked an arbitrary odd node when the snapped pin was not itself odd —
it now minimises true network distance over F. The office-outside-the-
area question was resolved as **Option A enabled by B1**: enlarge the
polygon, re-extract with `--default-service 0`, replay the annotation
with `prepare_round.py`, and pin `--start`/`--end`; endpoints off the
service component are joined by the auto-bridge (near-optimal warning
stands, honestly). Option B was not needed.

## 7. Backlog (V1.1 / V3+)

- **B1 Declarative round config.** *Shipped 2026-08* as
  `prepare_round.py`: `--export` snapshots the complete annotation
  (`service_overrides.csv` covers every edge; `splits.csv` is
  reconstructed from the split notes, parents before children) and the
  default mode replays splits (via `split_edge.py`) then overrides
  onto a fresh extraction, with backups and stale/new-edge reporting.
  Fixes E8; workflow: `extract --default-service 0 → prepare → solve`.
- **B2 Cost profiles.** *First cut shipped 2026-08* as `--profile`
  (preset `distance` / `edv`, or a JSON file of `{highway: km/h}`):
  cost = metres ÷ speed, so a metre of footpath costs more than a
  metre of road and deadhead prefers carriageways. Reporting stays in
  real metres (NF3). Measured effect on the real round: footway
  deadhead 0.49 km → 0.03 km for +0.05 km total. Note the *large* win
  (32% → 5% footway deadhead) came earlier and for free from
  `--network-type all` (E15): with the roads missing, the solver had
  no choice. **Still open:** real time estimates (fixed penalties for
  signalised crossings and arterials; turn-aware costs need a
  line-graph formulation — research task).
- **B10 One-way awareness (three tiers).** Since the `all` default
  (E15) `oneway` is real data — 2 497 of 12 912 edges on the real
  round. The solver is still undirected (E13) and ignores it.
  Measured on the current route: 8.59 km of 29.37 km runs on one-way
  edges, and **4.01 km of that is against the arrow** (47% of the
  one-way mileage; 1.76 km service + 2.25 km deadhead), concentrated
  on divided arterials mapped as two one-way chains. Legal on the
  footpath, wrong on the carriageway. Three separable tiers, cheapest
  first:
  1. **Flag it.** *Shipped 2026-08.* `route.csv` gained
     `against_oneway`; the viewer draws those stretches dark red and
     dashed, names them in the tooltip and the step panel, and totals
     them ("N km against a one-way -- use the footpath"). Purely
     informational; the model stays undirected.
  2. **Penalise it.** *Shipped 2026-08* as `--wrong-way-penalty`
     (default 1.0 = off). Every edge becomes two arcs in a directed
     cost graph `D`; the backward arc of a one-way costs x FACTOR.
     Only steers deadhead -- service passes are mandatory whichever
     way they are ridden. Measured at x3 on the real round: wrong-way
     4.18 -> 2.29 km (its deadhead part ~2.4 -> 0.48 km) for +0.27 km
     total. Documented approximation: costs become asymmetric while
     the matching needs one number per pair, so `symmetrise()` takes
     the cheaper direction -- a lower bound, since the Euler tour only
     picks the direction later. Each connector therefore stores a path
     for *both* directions and the traversal uses the matching one.
     Off by default: riding the footpath is direction-free, and on a
     divided arterial the "correct" chain is across the median.
  3. **Forbid it (exact, different solver class).** A true directional
     constraint is the Mixed CPP: NP-hard (E13), needs the ILP /
     branch-and-cut backend of B8. Only worth it with a concrete
     street that must be ridden on the carriageway.
- **B3 Prefer paired passes.** *Shipped 2026-08 as `--pair-passes`,
  off by default, because it measurably does not pay.* The bonus makes
  the tour chooser take the sibling pass of the edge it just rode, and
  it costs no distance. On the real round it moved pairing from 16.8%
  to 22.1% while adding 6 crossing turns and 6 U-turns — three minutes
  for five points. Raising the bonus does nothing: 30 s, 90 s and 300 s
  all give exactly 22.1%, which locates the ceiling. **Hierholzer
  splices sub-tours into the middle of the walk**, so two edges chosen
  consecutively during the forward pass need not be adjacent in the
  emitted tour, and no choice-time weight can change that. Doing this
  properly means rearranging the finished tour (moving the closed
  sub-walk that sits between the two passes to another visit of the
  same node) — a real algorithm, not a weight.
- **B11 Choosing WHICH Euler tour (turn comfort, wrong-way).**
  *Shipped 2026-08.* Every Euler tour over the same augmented graph
  covers the same edges, so they all have the same length: the choice
  is free, and Hierholzer stays correct whichever unused edge comes
  next (unlike Fleury). `euler_tour()` therefore replaces networkx's
  arbitrary tour with a greedy one scoring turn comfort (straight /
  easy / crossing / U-turn, in seconds, sided by `--traffic-side`)
  plus wrong-way metres. Two things learned the hard way:
  - The greedy is myopic. Judging only the first edge made it *slide
    into* long backwards runs on a divided arterial, because "carry
    straight on" is the cheap choice; it now looks ahead along the
    forced continuation (55% of nodes have degree 2 and offer no
    choice at all, so entering a chain commits to all of it).
  - The wrong-way weight is tied to `--wrong-way-penalty`, not a
    constant: with the penalty off, trading turns for wrong-way is not
    what the rider asked for.
  Caveat on "free": a connector's two directions are separately
  computed shortest paths, so a wrong-way penalty can make them differ
  by a few metres. Without a penalty the length is identical to the
  metre (asserted in `test_solve.py`).
- **B4 GPX comparison.** Import a ride trace of the current route
  (private data — never committed) and report current vs optimised km.
- **B5 Interactive service editor.** Browser UI: click an edge to
  cycle 0/1/2, export `service_overrides.csv`. Natural React project;
  pairs with B1. *Status 2026-08: a lean cut shipped as
  `make_editor.py` (self-contained `data/editor.html`; click-to-cycle,
  name-search bulk set, exports a full replacement `edges.csv`). The
  B5 slot stays open for the overrides-export version once B1 exists.*
- **B7 Excluded / unrideable edges.** *Shipped 2026-08.* `service=x`
  drops the edge from F at load — never required, never deadhead.
  Editor: shift-click toggles x, bulk-x button, red dashed styling;
  loaders in solver + editor accept `x`/`X`; steps guidance updated
  (E6). Deleting the row remains the nuclear option.
- **B8 Provably optimal island connection (research).** Replace the
  MST bridging with an exact RPP formulation: ILP over edge
  multiplicities with degree-parity and connectivity (subtour
  elimination) constraints, solved by branch-and-cut with a MILP
  backend (HiGHS/CBC via PuLP or OR-Tools). Only worth it if the
  bridging kilometres reported in the warning are material — merging
  stray islands in the editor is nearly always the bigger, cheaper
  win. Would add the project's first heavy dependency.
- **B9 Solved-version history.** *Shipped 2026-08.* `--history` files
  each solve under `round.local/history/<id>/` (service.csv,
  summary.json, route.csv, route_map.html) with an `index.csv` for
  listing; `round_history.py list/show/diff` compares versions.
  Deduplicates unchanged re-solves by hashing the annotation. Answers
  "did that edit help?" with numbers instead of memory.
- **B13 Turn-aware routing (line graph).** *Shipped 2026-08.* Turn
  cost used to reach only the choice of Euler tour, never the choice of
  path: Dijkstra over a road graph cannot price a manoeuvre, because a
  node has no memory of how you arrived. `--algorithm turn` routes over
  the **line graph** instead -- a vertex is "travelling along this arc",
  an edge is a turn, and its weight is the next arc's seconds plus what
  the turn costs. 86 676 manoeuvres priced on the real round; measured
  against the plain algorithm on identical work: **crossing turns
  36 -> 20**, wrong-way 0.94 -> 0.42 km, riding 57 -> 55 min, for
  +0.20 km. Routers return **arc** paths, so the arcs the search priced
  are the arcs that reach the route -- expansion no longer re-picks
  them. The reversed search builds its line graph for the other side of
  the road, because mirroring the network swaps left and right.
  `--variants` turns this into what a directions app offers: each entry
  of `VARIANTS` is one objective (shortest distance / fastest /
  recommended / fewest awkward turns / stay with the traffic), solved
  separately and then **all measured with the same profile**, so the
  numbers beside the options can honestly be compared. Output:
  `result/<preference>/` plus `alternatives.html`, one map with a
  switcher; the editor's Solve button runs the whole set. Measured on
  the real round -- shortest 29.35 km but 1h04 and 41 crossing turns,
  fastest 29.68 km / 55 min but 5.4 km against one-ways, recommended
  30.59 km / 55 min with 20 crossing turns and 0.4 km wrong-way.
  "Shortest" being the slowest option is the whole argument for
  offering the choice.
- **B6 Per-letterbox sequencing.** Join route order with the G-NAF
  open address database to emit house-number ranges per pass —
  turning route.csv into a literal sort plan.

## 8. Testing strategy

Offline synthetic tests are the backbone (`test_solve.py`): exact
multiplicities, walk continuity, circuit closure, open ≤ circuit,
auto-bridge behaviour, `--start` snapping. Every spec above lists its
acceptance tests; features merge together with their tests. Extraction
is the only networked step — keep it thin, and keep everything after
`edges.csv` testable without internet.

## 9. Decision log

| When | Decision | Why |
|---|---|---|
| 2026-08 | Model as undirected RPP with per-edge multiplicity `service∈{0,1,2}` | One editable column expresses territory, one-sided streets and zigzag choices; delivery vehicles on footpaths are effectively direction-free |
| 2026-08 | Multiplicity 2 = one traversal per side; zigzag is the user's per-street choice via `1` | Sides map to passes; no global zigzag assumption needed |
| 2026-08 | Non-service streets stay in the graph as deadhead (`0`), not deleted | Shortcuts through foreign streets are legal and often optimal |
| 2026-08 | Exactness only claimed for connected required sets; greedy bridge + warning otherwise | Honest optimality (NF2); optimal island connection out of scope |
| 2026-08 | Open path via best-pair exclusion from the matching | Small odd-node counts make the O(k²) sweep trivial |
| 2026-08 | Default network `walk` | Closest to a delivery vehicle using footpaths; captures laneway shortcuts; noise handled by the 0 value |
| 2026-08 | Mid-block boundaries via post-extract CSV mutation (SPEC-1), declarative overlay deferred to B1 | Smallest change that meets R6; reproducibility is a separate concern |
| 2026-08 | Fixed end via zero-length virtual required edge (SPEC-2) | Standard reduction; reuses the whole pipeline, keeps exactness |
| 2026-08 | End point lies outside the round's extraction area → Option A/B recorded, decision deferred to V2 | Needs merge tooling (A) or accepts slight suboptimality (B) |
| 2026-08 | Public/private split: generic repo + demo area (Albert Park, VIC); all real-round data gitignored | The edited edges.csv is effectively a personal movement map; code has zero need for it |
| 2026-08 | Weights read only via `length` | Keeps V3 time-costs a drop-in (NF3) |
| 2026-08 | Stay undirected; export `oneway` as an informational column only | Footpath work is direction-free; a mixed directed/undirected model (Mixed CPP) is NP-hard and would change the solver class (E13). Exported now because re-extraction later would wipe manual service edits (E8) |
| 2026-08 | Lean browser editor shipped ahead of plan (`make_editor.py`; exports a complete replacement `edges.csv`, not `service_overrides.csv`) | First real annotation session showed per-id lookup in Excel was the bottleneck. A full-file export needs no new apply step and keeps `edges.csv` the single source of truth; the B1 overrides workflow (survives re-extraction) stays open. SPEC-1 deferred until the first real mid-block boundary appears |
| 2026-08 | Editor picking = "nearest edge to cursor" over a projected-point cache; hover thickens + captions the target; zoom raised to 22 | Slivers of split streets were unclickable: Leaflet's default 18-zoom cap left them a few px long, and thick neighbouring lines stole the hit test. Nearest-edge picking makes thickness/draw order irrelevant |
| 2026-08 | Editor `--serve` mode: localhost server rebuilds the page from disk on every reload; Save POSTs the CSV back, server validates (header/edge_id order fixed, only `service` may change) and writes atomically with an `edges.csv.bak` backup. Static snapshot + export kept as the no-server fallback | Real use hit the snapshot trap: the export landed in Downloads, `data/edges.csv` was replaced correctly, but reopening the static page showed the old embedded data and looked like data loss. Serving closes the read-edit-write loop and removes the whole stale-snapshot class of errors |
| 2026-08 | `route_map.html` rewritten as a self-contained step-through viewer (slider/play/arrow-key stepping, current segment highlighted with a direction arrow, visited vs unvisited, START=END merged into one marker on circuits); folium now used only by the extraction preview | The static gradient map could not answer "where do I go next", hid the start marker under the end marker on circuits, and made corridors that are legitimately used twice look like drawing errors. Stepping the walk makes the solver's choices legible and separates data problems from display problems |
| 2026-08 | Island view inside the editor (client-side union-find over service>0 edges, one colour per island, size list with zoom-to buttons, live while editing) | A text report of island street names was useless for locating unnamed footway fragments; colouring them on the map with a "go" button makes strays obvious and fixable in place |
| 2026-08 | `service` gained `x`: the edge is dropped from F at load — never required, never deadhead. Editor: shift-click toggle + bulk-x; steps guidance now `x` (E6) | `0` keeps an edge routable, and a real route deadheaded through a connector the rider considers unusable. A fourth value closes the gap; absence of `x` ≡ old behaviour, so no migration (B7) |
| 2026-08 | SPEC-1 shipped as `split_edge.py`, using a local equirectangular metre frame + haversine instead of the spec's `shapely.ops.substring` | Projecting in raw lon/lat degrees skews distances by cos(lat); the local frame is exact for the ≤30 m decisions involved, keeps the splitter stdlib-only, and child lengths come from haversine chains as specified. **V1 complete** |
| 2026-08 | B1 shipped: overrides snapshot every edge (not just deviations); splits.csv is reconstructed from child `note`s and replayed through `split_edge.py` as a subprocess; recommended re-extraction default is `--default-service 0` | A full snapshot restores the round regardless of the extraction default and makes "new streets = whatever the default was" explicit; note-reconstruction means users never maintain splits.csv by hand; subprocess replay keeps the tools decoupled |
| 2026-08 | SPEC-2 shipped: `--start`+`--end` via the zero-length virtual required edge; `--end` alone via the pinned open path; endpoints snapped to F; depot/office outside the old area handled by Option A (enlarge polygon + `--default-service 0` + B1 replay), Option B dropped | The virtual-edge reduction reuses the entire pipeline and keeps exactness when endpoints touch the service component; off-component endpoints ride the existing auto-bridge with its honest warning. B1 removed Option A's blocker, so the strictly better variant won |
| 2026-08 | `--return-to-start` (requires `--end`): closed tour start → service → end → start = the SPEC-2 open route plus a shortest-path tail appended as deadhead | The real round is "depot → deliver → handover office → depot": the office is the last stop before home, so the tail is a constant independent of the covering walk — appending it preserves joint optimality with zero new machinery |
| 2026-08 | Point picking on the maps: editor right-click copies `lat,lon` / `--start=` / `--end=`; the extraction preview gets folium's LatLngPopup | Coordinates are the CLI primitive (reproducible, scriptable), but hunting them in an external map was needless friction |
| 2026-08 | Endpoints became data, not just arguments: right-click sets START/END into `data/endpoints.json`, the editor draws them, `solve_route.py` loads the file when `--start`/`--end` are absent, `prepare_round.py` carries it across re-extractions; a Solve button runs the solver server-side and serves the route map | Copy-pasting coordinates into a terminal was still the last manual step. Storing the pins makes the browser flow complete (edit → Save → Solve → view) while the CLI stays authoritative and scriptable |
| 2026-08 | Editor: Leaflet `boxZoom` disabled; contextmenu `preventDefault`; added a "click sets" mode selector | Two real bugs found in use: shift+click (the x gesture) was eaten by box-zoom, since a 1 px wobble zooms to a box, and the right-click popup was hidden behind the browser's native menu. The mode selector removes the reliance on modifier keys altogether |
| 2026-08 | Algorithms became a list rather than a replacement: `ALGORITHMS` maps a name to the routers to run, `--algorithm all` solves with each and emits a side-by-side map | The two answer different questions ("which is shortest" vs "which rides best"), and neither is wrong. Showing both, with the numbers that separate them, is more honest than picking one -- and adding a third is now a dict entry, not a rewrite |
| 2026-08 | Route quality is tracked by operational metrics, not distance alone: % of two-sided streets done back to back, number of continuous delivery runs, visits per street — reported and stored per version | Distance is 63% constant on this round, so it is a poor yardstick; what differs between routes is overhead and how pleasant the result is to *work*. A street entered three times is three places in the sort frame, and that costs nothing in metres |
| 2026-08 | `--pair-passes` ships off by default | Measured: +5.3 points of pairing for +3 min of turning, and it saturates at 22.1% however large the bonus, because Hierholzer splices sub-tours between the two passes. Shipping it on would have made routes worse for a metric that barely moved (B3) |
| 2026-08 | Exactly two speed profiles, both posted-limit based: `edv` caps at the vehicle's 50 km/h, `limits` does not. `distance` removed; cost is always seconds, so the optimiser minimises the same quantity it reports | Two questions worth asking -- "how fast can I do this?" and "how fast does the road allow?" -- and they differ only by the cap. A pure-distance mode answered a third question nobody was asking once time became the yardstick; km are still reported exactly, they are just no longer what gets minimised |
| 2026-08 | Speed model: footpath speed comes from the profile, every other surface uses the posted limit (`--profile limits`), with an optional `cap_kmh` | The rider's own model, and the data agrees with it: 0 of 5 890 footway edges carry a maxspeed, while trunk/primary/secondary carry one 96-100% of the time. A posted limit describes the carriageway, so it is the honest number exactly where you ride on the carriageway, and meaningless where you do not. The cap exists because a limit is not a vehicle capability |
| 2026-08 | History versions numbered like an app release (major/minor/patch), auto-classified from what changed, `--bump` to force | "How big was this change?" is the question the user actually asks of a history list, and a timestamp cannot answer it. Touching one street stays minor whatever the percentage |
| 2026-08 | SPEC-2's pinned mode now starts the circuit at the pinned **end** and crosses the virtual edge first, instead of rotating afterwards and reversing when the circuit happened to cross it the other way | The reversal mirrored **every** traversal in the route, so a coin flip decided whether the tour's carefully chosen directions survived: measured 2.47 km ridden against one-ways where the tour had produced 0.03 km. Reversing a walk is not direction-neutral once anything cares about direction |
| 2026-08 | Riding time and turn counts are reported (rough: profile speeds + a fixed cost per turn), and stored per version | The user asked to compare routes by time. Reported as "excludes every stop" because stop time at letterboxes dominates a real round and would make an absolute estimate fiction; as a *relative* measure between versions it is sound |
| 2026-08 | One-way direction is captured from the directed graph before `to_undirected()` and written into the u/v columns; `edge_id` stays keyed on iteration order | The undirected view keeps no direction, so 49% of one-way edges had been exported backwards (E16). Everything built on B10 — the `against_oneway` flag and the wrong-way penalty — was reasoning about a coin flip. Keeping `edge_id` stable meant the fix cost one re-extraction and zero annotation |
| 2026-08 | Optimisation weight (`cost`) split from reported distance (`length`), on a directed graph `D` where every edge becomes two arcs | A cost profile and a wrong-way penalty both need a weight that is not metres, and one of them is direction-dependent. Keeping `length` untouched means every printed kilometre stays a real kilometre, and `--profile distance` reproduces the old routes exactly (verified: synthetic totals unchanged to the metre) |
| 2026-08 | Wrong-way penalty ships off by default | On a footpath, direction does not exist; on a divided arterial the "correct" chain is on the far side of the median, so forcing it could send the rider across the road. A real option for carriageway riding, not a default (B10) |
| 2026-08 | Default extraction switched from `walk` to `all` | `walk` drops junction carriageways, shattering the road network into 34 components and forcing routes onto pedestrian crossings; `all` keeps both layers, restores "go straight", yields real `oneway` data and a 1.25 km shorter route on the same annotation (E15) |
| 2026-08 | `prepare_round` gained a geometry fallback: overrides whose `edge_id` vanished are re-found by position (same name/type, all sample points within 6 m, length-bounded), accepting any component of an osmnx-merged `"A; B"` name | Changing the network type changes which junctions exist, so osmnx splits ways differently and 618 of 10 850 ids disappeared. Id-only replay silently lost 7 annotated edges (659 m); with geometry it recovered all of them as 18 new pieces, and mandatory distance came out identical to the metre. B1's promise ("fully reproducible after re-extraction") only holds with this |
| 2026-08 | `.gitignore`: `data/` → `data*/`, `result/` → `result*/` | A side-by-side `data_all/` extraction of the real round showed up as untracked — the ignore patterns only covered the exact directory names |
| 2026-08 | Version history stores the *annotation* (edges with service 1/2/x, ~200 rows) rather than a copy of `edges.csv` (10 850 rows), keyed by a hash of it; unchanged re-solves are not filed | The annotation is the round definition and is two orders of magnitude smaller; hashing it makes "did anything actually change?" exact, so pressing Solve repeatedly cannot bury the real changes |
| 2026-08 | Island warning rewritten: names service islands vs pinned endpoints separately, reports the bridging kilometres, points at the editor's island view | The old message called the endpoint pair a "service island" (wrong) and gave no sense of scale, so "near-optimal" read as "unquantified doubt" instead of "these 1.1 km are heuristic" |
