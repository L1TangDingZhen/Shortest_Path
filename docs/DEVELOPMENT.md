# DEVELOPMENT — requirements, edge cases, specs, decisions

This is the working document for the project. It records what we are
building, the edge cases discovered so far, implementation-ready specs
for the next tasks, and why past decisions were made. Update it as the
project evolves — especially the decision log.

---

## 0. Status snapshot

| Milestone | State |
|---|---|
| V1 core: extract → edit service → coverage-validated solve → route.csv + map | **done, tested** (`test_solve.py`) |
| Graphical service editor (`make_editor.py`, lean cut of B5) | **done, tested** (`test_editor.py`) |
| Excluded edges (`service=x`) across solver + editor | **done, tested** |
| SPEC-1 edge-split utility (`split_edge.py`, mid-block boundaries) | **done, tested** (`test_split.py`) — **V1 complete** |
| V2 / SPEC-2: fixed endpoints (`--start` + `--end`, depot → round → handover office) | **done, tested** (in `test_solve.py`) |
| B1: reproducible rounds (`prepare_round.py` export/apply) | **done, tested** (`test_prepare.py`) — **V2 complete** |
| V2.2 graphical endpoints + in-browser solving | **done, tested** |
| B9: solved-version history (`round_history.py`) | **done, tested** (`test_history.py`) |
| E15: extraction switched to `--network-type all` (roads actually connected) | **done** |
| B2 cost profiles (`--profile edv`) + B10.1/B10.2 wrong-way flag and penalty | **done, tested** (in `test_solve.py`) |
| B10 vehicle presets, explicit CLI precedence and actual penalty reporting | **done, tested** (in `test_solve.py`) |
| E16: one-way direction preserved through extraction | **done, tested** (`test_extract.py`) |
| B11: turn-aware Euler tour + rough riding time / turn counts | **done, tested** (in `test_solve.py`) |
| B12: posted speed limits in the data + `limits` profile + release-numbered history | **done, tested** |
| B3 pass pairing (`--pair-passes`) + operational metrics in history | **done, tested** — pairing off by default, see B3 |
| B13: turn-aware routing (line graph) + `--variants` route preferences | **done, tested** (in `test_solve.py`) |
| B14: intersection delay in the data and the routing | **done, tested** |
| B15: search over Euler tours (annealing) | **done** — measured at 0.5 min, the lever is exhausted |
| B6: sort plan (`sort_plan.py`) + printable/phone sheet | **done, tested** (`test_sortplan.py`) — blocked on address data, not code |
| Street-first construction (`--street-first`) | **done** — off by default, measured slightly worse |
| E17: explicit physical side tasks, independent of travel direction | **done, tested** (`test_delivery.py`, `test_solve.py`) |
| E18: pinned start / pinned end honoured exactly (T-join parity) | **done, tested** (in `test_solve.py`) |
| E19: controlled nodes survive OSM simplification | **done, tested** (`test_extract.py`) |
| E20: reverse arc search keyed its paths by the wrong node | **done, tested** (in `test_solve.py`) |
| V3+: real time costs, pass pairing, GPX compare, per-letterbox sequencing, exact ILP | backlog |

---

## 1. Problem statement

A delivery round must traverse a set of streets: some on both sides
(twice), some on one side (once), while other streets are merely
available as shortcuts. Find the shortest closed or open route that
performs all required traversals.

The base formulation is the **Rural Postman Problem** with edge
multiplicities. The following exactness results require connected required
work in an undirected graph with symmetric additive costs, not the current full
directional/turn-aware objective. Physical side tasks enforce coverage;
they do not turn heuristic optimisation into an exact solver.
Key theory facts that shape the design:

- It generalises the Chinese Postman Problem (all edges required).
- RPP is NP-hard **in general**, but when the required edges form one
  connected component it is solved exactly in polynomial time by:
  odd-node parity repair via minimum-weight perfect matching, with
  pair distances taken as shortest paths over the *full* network.
- If every street were multiplicity 2, the required graph would already
  be Eulerian and any sweep would be optimal in this connected,
  symmetric additive-cost model (not with turn costs). The handful of
  multiplicity-1 streets is precisely what creates odd-degree nodes and
  makes optimisation non-trivial.
- The mandatory distance Σ(length × service) is a hard lower bound; the
  matching cost is the only optimisable quantity in that restricted model.
  Directional/turn costs, bridging and return legs need separate treatment.

Not a TSP: we cover edges, not visit points.

## 2. Requirements

Functional:

- **R1** Cover every required physical side exactly once as service while
  optimising the configured cost. Directional costs, turn preferences and
  component bridging are heuristic; coverage is independently validated.
- **R2** Territory is defined by per-edge `service` in {0,1,2,x} and
  `delivery` in {none,left,right,both,unspecified}, independent of boundaries
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
  true conditions. Current full-route results are labelled heuristic in all
  modes, not just when islands require bridging. Coverage checks are separate.
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
  `edge_id,name,highway,oneway,maxspeed_kmh,length_m,service,note,u,v,geometry_wkt,delivery`
  (`service` ∈ {0,1,2,x}; `oneway` steers nothing by default — the
  model is undirected — but feeds the `against_oneway` flag and the
  optional `--wrong-way-penalty`, see E13 / B10).
  `delivery=left/right` means one physical side relative to the polyline
  oriented `u -> v`; use its local tangent, not a north/south split.
  Closed ways use stored geometry order. `both` requires left and right;
  `none` is paired with service 0/x; `unspecified` is service 1 awaiting
  user confirmation. Mismatched service/delivery is an error.
- `nodes.csv`: `node_id,lat,lon`
- `route.csv`:
  `seq,type,street,pass,side,direction,from_cross,to_cross,length_m,cum_km,against_oneway,edge_id,delivery_side,travel_direction,travel_side,counterflow`
  `delivery_side` is the fixed reference left/right task, never inferred
  from travel. `travel_direction` is forward/reverse relative to that
  reference; `travel_side` is the delivered side relative to the rider.
  These task fields are empty on deadhead. `counterflow=yes` means against
  the mapped one-way or, for a two-way service task, against the normal
  traffic flow on that side. It does not imply a legal prohibition.
  `side` is only a coarse compass display label (`inner`/`outer` on rings).
- `endpoints.json` may contain `wrong_way_penalty`. For a single solve,
  explicit `--wrong-way-penalty` wins over explicit `--vehicle`, then the
  saved factor, then 1.0. `--variants` uses its named presets instead and
  prints an override notice. All runs report the actual factor and source.
- Comparison metadata and history summaries include `wrong_way_penalty`
  and `wrong_way_source`; history also identifies the selected `variant`.
  These are the settings used for the recorded route, including when a
  multi-variant solve chooses a top-level result.
- Direction reports use `wrong_way_km` for all riding against mapped one-ways
  and `service_counterflow_km` / `service_counterflow_segments` for service
  tasks against reference traffic flow. They overlap; do not sum them.
  Counterflow remains diagnostic at x1 (including `footpath`), not an access
  ruling. The console, maps, comparison metadata and new history records share
  the same metric calculation. Old history without the new fields is unknown.
- History uses schema-versioned SHA-256 `input_fingerprint`, `config_fingerprint`,
  `route_fingerprint` and aggregate `run_fingerprint` fields in index/summary.
  `summary.json` also retains the per-file hashes and resolved `configuration`.
  The aggregate includes the result summary; rounded total distance alone is
  never evidence of an identical run. A nonempty note or explicit bump forces
  a version even when the aggregate matches.
  Deduplication searches the entire index, newest matching archive first, and
  returns that version without rewriting/reordering version archives. Index/summary
  fields `selection`, `variant`, `variant_label` and `algorithm` identify the
  selected result and its selection rule. Lists distinguish single-result
  Solve from minimum-time Compare; the route's closed/open `mode` is separate.
  `events.csv` stores `event_seq,event_timestamp,version_id,event_action` for
  observed transitions. Actions are `created` / `reused`; consecutive identical
  version references do not append events. The CLI `timeline` and editor display
  this activity. No-argument `diff` compares its latest two entries, including
  reuse (A -> B -> A compares B -> A). `list/show`, explicit `diff` references
  and negative indices remain archive-based; `diff OLD` defaults NEW to the
  newest archive. Fewer than two timeline entries makes default diff an error.
  Old archives without creation events appear as `archived` in memory; no
  unobserved reuse is reconstructed. Event timestamps include the local UTC offset.

`edge_id` is opaque to the solver — never parse meaning out of it.
Contract changes require: updating this section, a migration note
below, and test updates in the same commit. (The untracked local
CLAUDE.md mirrors these contracts for AI sessions — keep it in sync.)

Migration notes:

- 2026-10: Optimality wording now separates verified coverage from heuristic
  route quality in CLI help/results, island view and route/comparison maps.
  Single-island and pinned-endpoint routes have no general global-optimum
  guarantee. Bridge kilometres are not an optimality-gap bound. Preset ids
  remain stable; display labels describe priorities, and comparison winners
  are explicitly limited to estimated time among the generated candidates.
  Historical decisions below describe earlier simplified models, not proofs
  for the current directional/turn-aware solver.
- 2026-10: Multi-source routing orders source nodes for both node and turn
  algorithms; bridging orders components and compares equal-cost candidates by
  their arc sequences. Matching pairs are canonically oriented and sorted before
  connector insertion and cost summation. Identical inputs/runtime/settings now
  have regression coverage across five `PYTHONHASHSEED` values for both routers.
  This removes unordered-container variability, not the heuristic nature of the
  solution; input row order/runtime changes are still distinct experiments.
  History reuses old archives but now records observed transitions separately.
  Pre-event archive data remains read-only; past transitions are unknown.
- 2026-10: History reads legacy `service.csv` side defaults through the same
  `delivery_mode()` used by the solver/editor. Missing or empty `delivery`
  means `both` for 2, `none` for 0/x, and `unspecified` for 1; adding equivalent
  explicit values is not a changed annotation in diff. Confirming a legacy 1
  and changing left/right remain real edits. Archives are never rewritten.
  Service counterflow metrics are appended to history indexes and included
  in new summaries; missing older metrics display as unknown, never zero.
  A newly measured or changed counterflow value/count creates a version even
  at unchanged distance. Full experiment fingerprints now supersede the
  initial counterflow-only extension: complete node/edge CSV bytes (including
  connectors and exclusions), resolved configuration, ordered route CSV and
  summary metrics must all match for automatic deduplication.
  Configurations retain actual profile contents, endpoints, algorithm, traffic
  side, search/tour options, penalty/source and all comparison candidates with
  their common scoring profile. Solver module hashes and Python/NetworkX
  versions identify implementation changes; no Git identity or workspace path
  is needed. Input paths/mtimes are excluded; input formatting changes are
  conservatively distinct. Route CSV is parsed, preserving order while ignoring
  BOM/newline differences. Loading and archiving share one input snapshot.
  Old archives are not rewritten or assigned guessed fingerprints. Index
  columns are extended on the next write; missing/incompatible fingerprints
  mean unknown and create a new baseline. Missing effective config or route
  data also disables deduplication. Notes/forced bumps always create versions.
  Alternating Solve/Compare now reuses matching non-adjacent versions. Existing
  duplicates are retained. Older index rows are enriched for display from
  archived summary/configuration metadata in memory, never by guessing origin
  from a variant name. Missing evidence remains unknown; archives are untouched.
  Full network backups and concurrent independent CLI history writers remain
  outside this change; do not run multiple writers to the same history/output.
- 2026-09: Explicit physical side tasks replace the E17 direction repair.
  Missing `delivery` is inferred as `both` for service 2 and `none` for
  0/x. Legacy service 1 becomes `unspecified`, **never** zigzag/both.
  The editor can save pending annotations, but solving requires every
  one-sided road to be confirmed left or right. Its hover arrow shows
  the reference, Pending selects unconfirmed roads, and undo/save/export
  preserve both columns. CSV topology/geometry cannot be changed by Save.
  Save now requires an `X-Edges-Revision` SHA-256 of the exact CSV bytes
  used to build the page. A missing revision returns HTTP 428; a stale
  revision returns 409 without modifying the CSV or its backup. Successful
  saves return JSON with `message` and the new `revision`. Validation,
  backup and atomic replacement share a stable `edges.csv.lock` sidecar
  lock across threads/processes; the lock file must not be deleted while
  editors are running. Conflicts retain local edits/undo and offer a local
  download, never automatic reload or force overwrite. In-flight edits
  remain dirty against the submitted snapshot, not the later UI state.
  This protects editor annotation saves only: endpoint/preferences saving
  is unchanged, and CLI CSV writers/manual replacements do not acquire
  this advisory lock and must not run concurrently with editor saves.
  Self-loop service without usable geometry is now an input error.
  `route.csv` appends four fields; regenerate older routes before sorting.
  Sorting uses local polyline side and along-chain position, then reverses
  address order when travel reverses. Deadhead and changes of side separate
  runs. Points exactly on the reference cannot be assigned a side.
  Sort-plan CSV retains the legacy `houses` range summary and appends
  `house_sequence`, a JSON array of strings in actual run order. No export
  sorting or deduplication is applied to that array; `count` is its length.
  Range-summary parentheses use that same count, including repeats.
  `sort_addresses.csv` additionally exports one row per address with
  `delivery_seq,run_seq,street,housenumber,delivery_side,side,direction`.
  Global sequence numbers start at one; empty runs contribute no rows, and
  duplicate numbers retain separate positions. Spreadsheet users should import
  `housenumber` as text rather than relying on automatic numeric/date conversion.
  HTML renders the same complete sequence, with a secondary labelled range.
  Empty runs use `[]` / a dash. These are available address-point positions,
  not verified entrances or an inferred within-building unit order.
  Export tests cover reverse travel, nonmonotonic numbering, suffixes/units,
  duplicates, empty runs and merged runs separated by deadhead; browser
  tests check long sequences at desktop/mobile widths and in print media.
  Export snapshots store oriented geometry and delivery; replay remaps
  left/right on reference reversal, falling back to unspecified when the
  reference is ambiguous. Splitting preserves the parent's physical side.
  History hashes and diffs include delivery, so side-only edits are distinct.
  Existing real annotations are not automatically rewritten by this change.
- 2026-09: `--vehicle footpath|tricycle|carriageway` selects a soft-cost
  factor of 1/3/12. No preset is implicit: existing saved factors and the
  no-configuration default of 1 are preserved. Explicit flags override
  saved factors. The uncommitted prototype used a default vehicle and
  accidentally let saved factors override explicit presets; corrected
  before release. These presets do not change speeds or enforce access.
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
- 2026-09: `route.csv` gained `side` (fifth column, after `pass`): the
  compass point of the kerb that pass delivers, empty for deadhead.
  The authoritative coverage check now uses `delivery_side`, not this
  display label. On a closed way the display values are `inner` / `outer`.
  Column *index* readers must be updated from `pass` onwards.

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
  reports the island count and bridging kilometres as added riding,
  not as an optimality-gap estimate. The full route is heuristic even
  without bridges. Growing the nearest component is Prim-style growth;
  with symmetric additive path costs it gives an MST on the
  component-distance graph, not a guarantee for the current complete
  directional/turn-aware route. Even in that simpler model, a Steiner-style
  branch off an existing bridge can be cheaper, and the parity
  matching sometimes supplies connectivity for free. Proving an optimum
  needs a different algorithm class (ILP with connectivity
  constraints, branch-and-cut) — deliberately out of scope; see B8.
  The editor lists the gaps themselves, not just the islands: a
  `service=0` sliver whose two ends lie in different islands is exactly
  what has to be fixed, and "join" gives it the value the rest of its
  street already carries. Measured on the real round: 8 islands, joined
  by 18 slivers of 5-17 m; fixing them all leaves 2. Nine of them lie on
  one line of junctions, each the little piece of a delivered street
  that crosses the intersection.
  **Do not chase a single island.** Measured on the real round: joining
  all 18 gaps (5-17 m each) took it from 8 islands to 2, and made the
  route *worse* -- mandatory +375 m, total +160 m, while bridging fell
  only 1.24 -> 1.18 km, because the bulk of the bridging is reaching
  the pinned depot and office, which sit ~1 km off the delivery streets
  and always will. `service` means "the route must traverse this", not
  "there are letterboxes here": the bit of road inside a junction has
  none, so 0 is the correct value and the solver rides it as deadhead
  anyway. Marking it 2 would force two traversals of a junction stub
  for nothing. Only join a gap when that piece really is delivered.
  Earlier note, kept because the shape of the observation still holds:
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
  exists on the carriageway. The OSM `oneway` flag in `edges.csv`
  feeds the route flag and optional soft costs (B10/B11), but **no hard
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

- **E17 Physical side is not travel direction.** Two traversals alone
  cannot prove both kerbs were delivered. The former opposite-direction
  repair also could not represent riding backwards on the same sidewalk.
  Required graph R now contains a separate `(edge_id, delivery_side)`
  task for each required physical side, via `delivery.py`. Travel may
  reverse without changing that task; both sides may be served in the
  same direction on separate passes. A final multiset check rejects
  missing, duplicate and unexpected tasks before outputs are written.
  `service=1` means only the user-selected side and never both.
  The editor and sorting use the same u->v reference and local polyline
  tangents. Ring tasks use the stored geometry order and explicit travel
  direction; shapeless service loops are rejected.
  `cover_both_sides()` and `SIDE_PENALTY` are removed. The existing factor
  is a soft counterflow preference for task directions (on two-way roads
  based on `--traffic-side`, on one-way roads based on mapped flow); it
  remains one-way-based for deadhead. Default factor and CLI precedence
  are unchanged. This graph still joins both physical sides at each road
  node without measured crossing geometry or separate side-switch cost.
  Explicit sidewalk/crossing states and calibrated costs are future work;
  no legal vehicle access or globally optimal practical route is claimed.
- **E18 A pinned endpoint that is not an odd node.** An open walk
  exists exactly when every node has even degree apart from its two
  ends, so the set the matching must repair is the odd nodes with the
  wanted ends flipped in or out of it (a T-join). The old code only
  ever chose ends from the nodes that were *already* odd, and snapped
  a pin to the nearest of them. A finish pinned mid-street therefore
  ended the round somewhere else entirely, and a start pinned to the
  second of two odd nodes started at the first. Both are now single
  cases of the same enumeration, and `traverse()` asserts that the
  walk really does begin and end where it was told.
- **E19 Simplification deletes the things that make you stop.** OSMnx
  straightens a way by deleting the nodes along it, and a traffic
  signal or give-way line sitting mid-block is one of those nodes.
  `control_groups()` runs afterwards and cannot get them back. On the
  maintainer's round this silently removed 58 whole intersections —
  every give-way and stop line in the area, since those are almost
  never at a mapped junction. Extraction now marks controlled nodes
  before simplifying and protects them (`node_attrs_include`), which
  costs about 3% more edges and reports the count kept against the
  count found.
- **E20 A mirrored search returns mirrored keys.** Turn-aware routing
  searches paths that *end* at a target by walking a reversed line
  graph, then flips the arcs back. The result was keyed by the wrong
  end of the flipped arc, so "the cheapest way into here from X" was
  filed under the node one step further along. Island bridging is the
  caller that reads those keys, so it compared the wrong candidates.
  Regression test: every returned path must start at the node its key
  names and end at the target.

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
virtual edge is last, delete it. What remains is an open route A → … → B.
The virtual edge flips both endpoint parities. Optimality is preserved only
if the underlying closed problem is solved optimally under the restricted
symmetric additive-cost model with connected required work; that is not a
global-optimum claim for the current directional/turn-aware heuristic.
Appending a separately routed return leg likewise supplies no whole-tour
guarantee, including the turn cost at its join with the service route.

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
  `--end`. Co-optimised: the solver considers the office leg while
  choosing where to finish delivery, without guaranteeing a global optimum.
  Open design issue:
  extraction currently applies one `--default-service` to everything,
  so a corridor would arrive as `2` and need bulk-zeroing. Candidate
  fixes: a second extraction with `--default-service 0` plus a
  `merge_data.py` (union by `edge_id`, round rows win), or per-polygon
  defaults. Decide when building.
- **Option B — separate final leg.** Solve the round as today
  (circuit, or `--end` pinned to the boundary node nearest the office
  direction), then compute an ordinary shortest path round-end →
  office on a larger throwaway graph and report it as a separate leg.
  Simpler and decoupled: the finishing street is not co-optimised,
  and the resulting loss has no general bound.

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
service component are joined by the auto-bridge with its heuristic warning.
Option B was not needed.

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
  round. Required work is still undirected (E13); direction can affect
  cost without removing the reverse arc.
  Measured on the current route: 8.59 km of 29.37 km runs on one-way
  edges, and **4.01 km of that is against the arrow** (47% of the
  one-way mileage; 1.76 km service + 2.25 km deadhead), concentrated
  on divided arterials mapped as two one-way chains. Legal on the
  footpath, wrong on the carriageway. Three separable tiers, cheapest
  first:
  1. **Flag it.** *Shipped 2026-08.* `route.csv` gained
     `against_oneway`; the viewer draws those stretches dark red and
     dashed, names them in the tooltip and the step panel, and totals
     them ("N km against a mapped one-way"). Purely
     informational; the model stays undirected.
  2. **Penalise it.** *Shipped 2026-08* as `--wrong-way-penalty`
     (default 1.0 = off). Every edge becomes two arcs in a directed
     cost graph `D`; the backward arc of a one-way costs x FACTOR.
     The path cost steers deadhead; the same preference also steers
     tour direction choices (B11). Service coverage stays mandatory.
     Measured at x3 on the real round: wrong-way
     4.18 -> 2.29 km (its deadhead part ~2.4 -> 0.48 km) for +0.27 km
     total. Documented approximation: costs become asymmetric while
     the matching needs one number per pair, so `symmetrise()` takes
     the cheaper direction -- a lower bound, since the Euler tour only
     picks the direction later. Each connector therefore stores a path
     for *both* directions and the traversal uses the matching one.
     Off by default: riding the footpath is direction-free, and on a
     divided arterial the "correct" chain is across the median.
     *2026-09:* explicit `--vehicle` presets select x1 (`footpath`),
     x3 (`tricycle`) or x12 (`carriageway`). They are only cost presets,
     not vehicle access models or speed profiles. Priority for a single
     solve is explicit factor > explicit vehicle > saved factor > x1.
     The factor and its source are printed and recorded; non-finite or
     invalid factors fail with the source named. `--variants` retains
     its preset factors and announces any overridden vehicle/factor.
     Synthetic tests exercise the CLI with and without saved values,
     a short reverse connector versus a longer detour, a necessary
     reverse traversal even at x12, and the actual settings in history.
  3. **Forbid it (exact, different solver class).** A true directional
     constraint is the Mixed CPP: NP-hard (E13), needs the ILP /
     branch-and-cut backend of B8. Only worth it with a concrete
     street that must be ridden on the carriageway.
- **B3 Prefer paired passes.** *Shipped 2026-08 as `--pair-passes`,
  off by default, because it measurably does not pay.* The bonus makes
  the tour chooser take the sibling pass of the edge it just rode, and
  has no distance cost only when connector expansions are length-symmetric.
  On the real round it moved pairing from 16.8%
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
  covers the same edge instances. Length is invariant only when each
  instance expands equally in both directions. Hierholzer stays correct whichever unused edge comes
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
  computed least-cost paths. Directional penalties, turn costs or equal-cost
  alternatives can make their physical lengths differ; there is no general
  few-metres bound. Distance equality on a symmetric test fixture is not a
  guarantee for arbitrary routes.
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
  Deduplicates matching complete experiment fingerprints (see migration notes).
  The annotation hash remains for version numbering and side diffs. Answers
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
  of `VARIANTS` is one priority (distance / riding time /
  recommended / turn avoidance / traffic flow), solved
  separately and then **all measured with the same profile**, so the
  numbers beside the options can honestly be compared. Output:
  `result/<preference>/` plus `alternatives.html`, one map with a
  switcher. The editor now separates **Solve route** (saved preferences,
  no `--variants`) from **Compare presets** (the whole preset set, under
  `<out>/comparison/`). Comparison leaves the ordinary solve's top-level
  route files and the saved endpoint/preferences file unchanged. Measured on
  the real round -- shortest 29.35 km but 1h04 and 41 crossing turns,
  fastest 29.68 km / 55 min but 5.4 km against one-ways, recommended
  30.59 km / 55 min with 20 crossing turns and 0.4 km wrong-way.
  "Shortest" being the slowest option is the whole argument for
  offering the choice.
  Named variants override `--profile`, `--algorithm`, `--vehicle`,
  `--wrong-way-penalty` and saved preferences, with an explicit notice.
  Every variant reports its actual penalty; comparison metadata and
  the selected route's history retain that value and its source.
  Editor `POST /solve` never loads old alternatives; `POST /compare` returns
  only its successful comparison results. Both actions wait for queued
  settings saves in the browser, refuse to run after a settings save failure,
  and disable settings/solve controls during a run. One server serialises
  solve requests (a concurrent request gets HTTP 409). Settings saves are
  queued in UI order so an older response cannot replace a newer edit.
  History fingerprinting records all effective comparison settings as well
  as the selected route, so adding a candidate is a distinct experiment even
  if the winner and its distance stay the same.
- **B14 Intersection delay.** *Shipped 2026-08.* Waiting is not
  riding, and on a footpath round it is a quarter of the day. Node tags
  were being thrown away (`nodes.csv` held only id/lat/lon) while the
  area holds 301 `traffic_signals`, 1804 `crossing`, 320 `give_way` and
  91 `stop` nodes. Extraction now keeps them **and clusters them into
  intersections** (40 m): a junction carries 4.5 tagged nodes on
  average, so charging per node would over-count by that much. The
  delay is charged once per intersection *entered*, which the line
  graph can express because its vertex is an arc and therefore knows
  where you came from. Measured: waiting **20 min -> 10 min** once the
  router could see crossings, total 1h16 -> 1h09 for +2 km. This is the
  last change that moved the route's shape.
- **B15 Searching over Euler tours.** *Shipped 2026-08, and it is
  exhausted on that experiment.* `local_search_tour`
  hill-climbs (then anneals) over the two moves that preserve an Euler
  tour: reverse a closed sub-walk, or do it at another visit of the
  same node. Objective: turn seconds + intersection delay + wrong-way
  metres + a 20 s charge per extra street visit. Result on the real
  round: **0.5 min and 4 crossing turns**, and *identical* at 4 000 and
  20 000 iterations, with strict hill climbing and with annealing
  accepting 8 985 moves. The greedy chooser is already at a strong
  local optimum. Street runs did not move at all (43, against a
  theoretical 23): loop reversal cannot restructure a tour at street
  scale -- that needs a different construction, building the tour
  street by street rather than edge by edge.
- **B6 Per-letterbox sequencing.** *Built 2026-08 as `sort_plan.py`;
  the code works, the Australian data does not.* It attaches every
  address to the nearest edge carrying its street name, splits a
  two-sided street between its explicit physical side tasks, orders numbers
  along the actual travel direction, merges only compatible consecutive
  runs, and writes `sort_plan.csv` plus a printable, phone-sized
  `sort_plan.html`. **Coverage killed it, not the algorithm**: of 1 923
  OSM addresses in the area only 125 sit on a delivered street, and
  those are its arterials (64, 32 and 14 on the three biggest) — the
  residential streets that are the actual round carry 0 to 3 each,
  because Australian addresses live in G-NAF and were never imported
  into OSM. The input is a plain `addresses.csv` of
  `lat,lon,housenumber,street`, so a G-NAF extract drops straight in
  and the plan fills out; that conversion is the remaining work.

## 8. Testing strategy

Offline synthetic tests are the backbone (`test_solve.py`): exact
multiplicities, walk continuity, circuit closure, open ≤ circuit,
auto-bridge behaviour, `--start` snapping. Every spec above lists its
acceptance tests; features merge together with their tests. Extraction
is the only networked step — keep it thin, and keep everything after
`edges.csv` testable without internet.

`test_delivery.py` covers side identity under reversed travel and traffic
handedness, soft direction costs, same-direction passes on different sides,
diagonal/curved/ring geometry, pending legacy data, immutable editor references,
split/replay orientation, exact task validation and side-only history changes.
`test_history.py` also checks legacy side defaults, genuine confirmations and
side changes, archive immutability, and old indexes without direction metrics.
It exercises same-distance configurations, same-name custom profile edits,
connector/geometry/control changes, route reordering, explicit notes/bumps,
legacy fingerprint migration, missing-data handling, snapshots during edits,
and repeated single/comparison solves without duplicate versions.
It also checks non-adjacent matches, Solve/Solve/Compare/Compare/Solve producing
only two versions, forced repeats, and read-only recovery of legacy run labels.
`test_delivery.py` checks service counterflow versus one-way totals at x1/x3,
the footpath preset, and agreement across CSV, viewer, comparison and history.
`test_editor.py` checks snapshot/revision consistency, missing/stale revisions,
unchanged CSV/backup on conflicts, consecutive saves, concurrent HTTP saves
and separate processes competing for the same sidecar lock.
It also checks custom limits/x7 and edv/x11 solves, preset factors, separate
output directories, unchanged saved settings/current route during comparison,
and suppression of stale comparison metadata on a normal solve.
Concurrent solve/compare requests are rejected, and a failed solve releases
the gate for the next request.

`test_browser.py` is the reproducible Playwright/Chromium suite at 1440x1000,
390x844 and 320x740: Pending and rejected solves, reference arrows, paint,
bulk edits, undo, save/reload, two-tab conflicts with local download/undo,
in-flight edits and duplicate-save suppression, settings-save failures,
queued settings edits and save/solve ordering, current-config solves,
separate preset comparisons and viewer stepping/playback.
It checks viewport/panel bounds and nonblank route canvas pixels and saves
screenshots in `_test_tmp/browser/`. Only synthetic roads and localhost are
used. After the one-time setup in README, Leaflet is served from a pinned,
checksum-verified local cache, tiles are replaced by a blank image, and other
external page requests are blocked. This is not a test of the external basemap.
Run it after the other suites, since `test_solve.py` resets `_test_tmp/`.

## 9. Decision log

Historical decisions below record the model at the time. The final E17 entry
supersedes the early zigzag interpretation and direction-based coverage check;
the current contracts above take precedence.

| When | Decision | Why |
|---|---|---|
| 2026-08 | Model as undirected RPP with per-edge multiplicity `service∈{0,1,2}` | One editable column expresses territory, one-sided streets and zigzag choices; delivery vehicles on footpaths are effectively direction-free |
| 2026-08 | Multiplicity 2 = one traversal per side; zigzag is the user's per-street choice via `1` | Sides map to passes; no global zigzag assumption needed |
| 2026-08 | Non-service streets stay in the graph as deadhead (`0`), not deleted | Shortcuts through foreign streets are legal and often optimal |
| 2026-08 | Original exactness claim required connected work | Qualified in 2026-10: symmetric additive costs and an exact subproblem are also needed; the current full route remains heuristic even with one island |
| 2026-08 | Open path via best-pair exclusion from the matching | Small odd-node counts make the O(k²) sweep trivial |
| 2026-08 | Default network `walk` | Closest to a delivery vehicle using footpaths; captures laneway shortcuts; noise handled by the 0 value |
| 2026-08 | Mid-block boundaries via post-extract CSV mutation (SPEC-1), declarative overlay deferred to B1 | Smallest change that meets R6; reproducibility is a separate concern |
| 2026-08 | Fixed end via zero-length virtual required edge (SPEC-2) | Standard endpoint reduction; preserves optimality only when the underlying closed problem is solved optimally in the restricted symmetric model |
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
| 2026-08 | SPEC-2 shipped: pinned endpoints on F, virtual-edge reduction, enlarged extraction with B1 replay | Endpoint constraints are preserved. Co-optimising the office leg is not a global optimum proof; directional/turn costs and off-component bridging remain heuristic |
| 2026-08 | `--return-to-start` appends a separately routed deadhead tail after the pinned end | Under a purely additive model with fixed endpoints the tail can be independent. The current turn-aware full route, including the join to that tail, has no joint-optimality guarantee |
| 2026-08 | Point picking on the maps: editor right-click copies `lat,lon` / `--start=` / `--end=`; the extraction preview gets folium's LatLngPopup | Coordinates are the CLI primitive (reproducible, scriptable), but hunting them in an external map was needless friction |
| 2026-08 | Endpoints became data, not just arguments: right-click sets START/END into `data/endpoints.json`, the editor draws them, `solve_route.py` loads the file when `--start`/`--end` are absent, `prepare_round.py` carries it across re-extractions; a Solve button runs the solver server-side and serves the route map | Copy-pasting coordinates into a terminal was still the last manual step. Storing the pins makes the browser flow complete (edit → Save → Solve → view) while the CLI stays authoritative and scriptable |
| 2026-08 | Editor: Leaflet `boxZoom` disabled; contextmenu `preventDefault`; added a "click sets" mode selector | Two real bugs found in use: shift+click (the x gesture) was eaten by box-zoom, since a 1 px wobble zooms to a box, and the right-click popup was hidden behind the browser's native menu. The mode selector removes the reliance on modifier keys altogether |
| 2026-08 | `sort_plan.py` reads a plain `addresses.csv` rather than importing G-NAF itself | The join, the side-splitting and the sheet are the work; where the numbers come from is not. Keeping the input a four-column CSV meant OSM could be tried in an afternoon, and when it turned out to cover 125 of 1 923 addresses on the round, nothing was wasted — a G-NAF extract replaces the file and the rest stands |
| 2026-08 | Street-first construction measured and left off; `meta` was missing `kmh`, so graded turn costs had been inert in the tour chooser | Biasing construction toward finishing a street changed street visits 43 → 44 and crossing turns 24 → 27 on the real round: within noise, not an improvement. Finding it meant fixing a silent bug — a patch that never matched its anchor, applied without an assertion, had left the tour chooser blind to road speed |
| 2026-08 | Turn cost is graded by the road being entered: `turn_seconds()` scales a crossing or U-turn by the entered road's speed over a 50 km/h reference, clamped to 0.4-2.0 | What makes a right turn expensive is the traffic you wait for, and the road's speed is the best proxy the data has. Turning onto a laneway and onto a 60 km/h arterial were priced identically before, which is exactly the case the rider raised |
| 2026-08 | B3 rearrangement built, measured, and still left off — because it improves pairing while making street contiguity worse | Edge-level pairing and street-level sweeping pull against each other, and the sorter cares about the street. Measuring the operational metrics first is what made this visible; without them it would have shipped as an improvement (B3) |
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
| 2026-09 | Vehicle presets are opt-in soft costs; explicit numeric factor > explicit vehicle > saved factor > x1 | Keeps the historical default and lets the CLI override editor settings. Factors 1/3/12 are preferences, not measured multipliers of riding time or access permissions. Named variants keep their own settings, announce overrides, and report/store the actual values. |
| 2026-08 | Default extraction switched from `walk` to `all` | `walk` drops junction carriageways, shattering the road network into 34 components and forcing routes onto pedestrian crossings; `all` keeps both layers, restores "go straight", yields real `oneway` data and a 1.25 km shorter route on the same annotation (E15) |
| 2026-08 | `prepare_round` gained a geometry fallback: overrides whose `edge_id` vanished are re-found by position (same name/type, all sample points within 6 m, length-bounded), accepting any component of an osmnx-merged `"A; B"` name | Changing the network type changes which junctions exist, so osmnx splits ways differently and 618 of 10 850 ids disappeared. Id-only replay silently lost 7 annotated edges (659 m); with geometry it recovered all of them as 18 new pieces, and mandatory distance came out identical to the metre. B1's promise ("fully reproducible after re-extraction") only holds with this |
| 2026-08 | `.gitignore`: `data/` → `data*/`, `result/` → `result*/` | A side-by-side `data_all/` extraction of the real round showed up as untracked — the ignore patterns only covered the exact directory names |
| 2026-08 | Version history stores the *annotation* (edges with service 1/2/x, ~200 rows) rather than a copy of `edges.csv` (10 850 rows), keyed by a hash of it; unchanged re-solves are not filed | The annotation is the round definition and is two orders of magnitude smaller; hashing it makes "did anything actually change?" exact, so pressing Solve repeatedly cannot bury the real changes |
| 2026-08 | Island warning separates service islands from pinned endpoints and reports bridging kilometres | Corrected again in 2026-10: those kilometres are added riding, not a bound on the whole route's optimality gap; no near-optimum claim is made |
| 2026-09 | Historical E17 attempted side coverage through travel-direction repair | Superseded by explicit physical-side tasks below. Opposite travel directions alone do not prove side coverage, and directional connector expansions mean tour changes need not preserve distance |
| 2026-09 | A closed way's second lap is emitted riding the other way round, and its kerbs are named `inner` / `outer` from the sign of the ring's area | `u == v` carries no direction, so both laps of a loop road came out identical — the same defect as an ordinary street ridden twice the same way, but invisible to a check based on endpoints. Both laps of a ring cost the same and end in the same place, so riding one each way is free; naming the sides by compass point would have been fiction |
| 2026-09 | `route.csv` names the kerb each pass delivers (`side`), by compass point (`inner`/`outer` on a closed way) | The guarantee needs to be checkable by the person riding it, not just by an assertion. It also gives the sort plan a side to key on without re-deriving travel direction |
| 2026-09 | Open-route endpoints chosen by T-join over `odd Δ {start, end}`, with pinned ends included in the set to repair | The old rule ("ends must already be odd") is only true when both ends are free. A pin in the middle of a street has even degree, so it could never be chosen, and the round quietly finished elsewhere — the one thing a fixed finish exists to prevent |
| 2026-09 | Controlled nodes are marked before simplification and protected through it | 58 intersections' worth of waiting time was missing from every route ever solved, and nothing downstream could tell: the nodes were simply not there. Roughly 3% more edges is a cheap price for the numbers being real |
| 2026-09 | `F` and `D` are built from one shared attribute dict | They had drifted: `kmh` reached `D` only, so every lookup through `F` read `None` and the speed-graded turn cost was inert in the tour chooser. The duplication was the bug; removing it is the fix |
| 2026-09 | `prepare_round` reports the round's mandatory distance on every apply, and shouts when unmatched edges carry a non-zero default | Re-extracting without `--default-service 0` turns every street the replay cannot match into a delivery street. On the real round that took 18.85 km to 101.53 km with no error and no warning — a five-fold round that looks like a successful replay |
| 2026-09 | E17 replaced by explicit `delivery` tasks, separate from travel; `service=1` always one side, `2` both | A vehicle may reverse on the same sidewalk. Opposite travel directions do not define physical coverage. The editor requires left/right confirmation for legacy 1s; the solver validates the exact task multiset; sorting, splitting, replay and history preserve that identity. Same-direction passes may serve opposite sides. The opt-in factor now also gives two-way service tasks a soft normal-flow preference. No extra sidewalk crossing geometry, access enforcement or global optimality is claimed. |
