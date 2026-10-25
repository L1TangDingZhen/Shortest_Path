#!/usr/bin/env python3
"""Version history for solved rounds.

Every solve can be filed away with the annotation that produced it, so
you can see how the round evolved: which streets you added or dropped,
and what that did to the distance.

    python solve_route.py --data data --out result --history
        ... solve as usual and record a version (default location:
        round.local/history)

    python round_history.py list
    python round_history.py timeline
    python round_history.py show <id>
    python round_history.py diff <old> <new>
        <id> may be a version number ("1.2.0"), a timestamp id or its
        prefix, or a negative index: -1 is the newest version, -2 the
        one before it.  `diff` with no arguments compares the two
        latest timeline entries, including reuse of an older version.
        Explicit references remain archive-based; with only <old>,
        <new> defaults to the newest archive.

Versions are numbered like an app release, so the number tells you how
big the change was: major = the network was re-extracted or the round
changed substantially, minor = streets added/dropped/changed, patch =
same round solved with different settings.

Layout (inside the gitignored round directory):

    history/index.csv          one row per version, for quick listing
    history/events.csv         observed transitions between solved versions
    history/<id>/summary.json  totals, endpoints, mode, counts
    history/<id>/service.csv   the round definition at that moment
                               (every edge with service 1/2/x)
    history/<id>/route.csv     the emitted route
    history/<id>/route_map.html   the map, openable months later

Automatic deduplication requires matching input, effective configuration,
ordered route and result metrics. Old/incomplete records cannot prove a
match. A nonempty note or explicit --bump always creates a version.
"""

import argparse
import csv
import hashlib
import io
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

from delivery import delivery_mode

DEFAULT_HISTORY = "round.local/history"
INDEX = "index.csv"
EVENTS = "events.csv"
EVENT_FIELDS = ["event_seq", "event_timestamp", "version_id", "event_action"]
FINGERPRINT_VERSION = 1


def next_version(previous, summary, digest, bump=None):
    """Version the round like an app release, so the size of the number
    tells you the size of the change:

      major  the network itself changed (a re-extraction), or more than
             5% of the annotated edges did -- and always at least two,
             so touching a single street stays a minor edit
      minor  the round definition changed at all -- streets added,
             dropped or switched between one and two sides
      patch  same round, different solver settings (profile, penalties,
             endpoints): the answer changed, the job did not
    """
    if not previous:
        return "1.0.0"
    major, minor, patch = (int(p) for p in
                           (previous.get("version") or "1.0.0").split("."))
    if bump is None:
        was = int(previous.get("service_edges") or 0)
        now = int(summary.get("service_edges") or 0)
        if previous.get("annotation") == digest:
            bump = "patch"
        elif (int(previous.get("edges_total") or 0)
              != int(summary.get("edges_total") or 0)
              or abs(now - was) > max(1, 0.05 * max(was, 1))):
            bump = "major"
        else:
            bump = "minor"
    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


FIELDS = ["id", "version", "timestamp", "note", "mode", "service_edges",
          "mandatory_km", "total_km", "deadhead_km", "islands",
          "bridge_km", "profile", "wrong_way_km", "time_min",
          "turns_cross", "turns_u", "paired_pct", "street_runs",
          "runs_per_street", "edges_total", "annotation",
          "service_counterflow_km", "service_counterflow_segments",
          "fingerprint_version", "input_fingerprint", "config_fingerprint",
          "route_fingerprint", "run_fingerprint",
          "selection", "variant", "variant_label", "algorithm"]


# ----------------------------------------------------------------------
# reading / writing
# ----------------------------------------------------------------------

def hash_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def hash_json(value):
    return hash_bytes(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                ensure_ascii=False, allow_nan=False).encode("utf-8"))


def capture_inputs(data_dir: Path):
    """Snapshot the source bytes once, for loading and later history recording."""
    return {name: (data_dir / name).read_bytes() for name in ("nodes.csv", "edges.csv")}


def read_annotation(data_dir: Path, raw_edges=None):
    """The round definition: every edge that is not a plain connector.
    Returns (rows, short hash of the whole annotation)."""
    rows = []
    if raw_edges is None:
        raw_edges = (data_dir / "edges.csv").read_bytes()
    with io.StringIO(raw_edges.decode("utf-8-sig"), newline="") as f:
        for r in csv.DictReader(f):
            service = (r.get("service") or "").strip().lower()
            if service in ("1", "2", "x"):
                rows.append({"edge_id": r["edge_id"], "service": service,
                             "delivery": delivery_mode(r),
                             "name": r.get("name") or "",
                             "length_m": r.get("length_m") or "0"})
    rows.sort(key=lambda r: r["edge_id"])
    blob = json.dumps([(r["edge_id"], r["service"], r["delivery"])
                       for r in rows], separators=(",", ":"))
    return rows, hashlib.sha1(blob.encode()).hexdigest()[:8]


def read_index(history_dir: Path):
    path = history_dir / INDEX
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def write_index(history_dir: Path, rows):
    with open(history_dir / INDEX, "w", newline="",
              encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def read_events(history_dir: Path):
    path = history_dir / EVENTS
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def record_event(history_dir: Path, version_id, created):
    events = read_events(history_dir)
    if events and events[-1]["version_id"] == version_id:
        return
    with (history_dir / EVENTS).open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=EVENT_FIELDS)
        if f.tell() == 0:
            writer.writeheader()
        writer.writerow(dict(event_seq=len(events) + 1,
                             event_timestamp=datetime.now().astimezone().isoformat(timespec="microseconds"),
                             version_id=version_id,
                             event_action="created" if created else "reused"))


def read_timeline(history_dir: Path):
    index = with_run_details(history_dir, read_index(history_dir))
    events = read_events(history_dir)
    by_id = {row["id"]: row for row in index}
    created = {e["version_id"] for e in events if e["event_action"] == "created"}
    # Pre-event archives prove creation only; never invent earlier reuse events.
    rows = [dict(row, event_seq="", event_timestamp=row.get("timestamp") or row["id"],
                 event_action="archived") for row in index if row["id"] not in created]
    rows.extend(dict(by_id[e["version_id"]], **e) for e in events)
    return rows


def run_details(summary, configuration=None):
    configuration = configuration if configuration is not None else summary.get("configuration")
    configuration = configuration if isinstance(configuration, dict) else {}
    variant = summary.get("variant") or ""
    variants = configuration.get("variants")
    variants = variants if isinstance(variants, list) else []
    selected = next((v for v in variants
                     if isinstance(v, dict) and v.get("name") == variant), {})
    return {"selection": summary.get("selection") or configuration.get("selection") or "",
            "variant": variant,
            "variant_label": summary.get("variant_label") or selected.get("label") or "",
            "algorithm": summary.get("algorithm") or selected.get("algorithm") or ""}


def with_run_details(history_dir: Path, rows):
    """Enrich old index rows from archived metadata in memory, without guessing."""
    result = []
    keys = ("selection", "variant", "variant_label", "algorithm")
    for row in rows:
        full = dict(row)
        if any(not full.get(key) for key in keys):
            try:
                summary = json.loads((history_dir / row["id"] / "summary.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                summary = {}
            if isinstance(summary, dict):
                for key, value in run_details(summary).items():
                    if not full.get(key):
                        full[key] = value
        selection = full.get("selection")
        origin, rule = {"single": ("Solve", "single"),
                        "minimum_comparison_time": ("Compare", "min time")}.get(
                            selection, ("Unknown", "unknown selection"))
        variant = full.get("variant") or ""
        label = full.get("variant_label") or variant or "-"
        if variant and label != variant:
            label += f" [{variant}]"
        full["run_description"] = f"{origin}: {label} / {full.get('profile') or '-'} / {rule}"
        result.append(full)
    return result


def read_service(version_dir: Path):
    """Normalise legacy side semantics in memory; never rewrite an archive."""
    with open(version_dir / "service.csv", newline="",
              encoding="utf-8-sig") as f:
        return {r["edge_id"]: dict(r, delivery=delivery_mode(r))
                for r in csv.DictReader(f)}


# ----------------------------------------------------------------------
# recording
# ----------------------------------------------------------------------

def record(history_dir: Path, data_dir: Path, summary: dict,
           out_dir: Path = None, note: str = "", bump: str = None,
           *, inputs=None, configuration=None):
    """File one solved version. Returns (version_id, is_new)."""
    if inputs is None:
        inputs = capture_inputs(data_dir)
    rows, digest = read_annotation(data_dir, inputs["edges.csv"])
    input_files = {name: hash_bytes(raw) for name, raw in inputs.items()}
    route_bytes = None
    if out_dir and (out_dir / "route.csv").exists():
        route_bytes = (out_dir / "route.csv").read_bytes()
    # Preserve row order, but do not treat BOM/newline changes as a new route.
    route_rows = (list(csv.reader(io.StringIO(route_bytes.decode("utf-8-sig"), newline="")))
                  if route_bytes else None)
    fingerprints = dict(fingerprint_version=FINGERPRINT_VERSION,
                        input_fingerprint=hash_json(input_files),
                        config_fingerprint=hash_json(configuration) if configuration else "",
                        route_fingerprint=hash_json(route_rows) if route_rows else "")
    complete = (all(name in inputs for name in ("nodes.csv", "edges.csv"))
                and configuration and route_rows and len(route_rows) > 1)
    fingerprints["run_fingerprint"] = hash_json(
        dict(**fingerprints, summary=summary)) if complete else ""
    index = read_index(history_dir)

    if index and not note.strip() and bump is None:
        # Prefer the latest matching archive, including explicitly forced repeats.
        for previous in reversed(index):
            if (fingerprints["run_fingerprint"]
                    and previous.get("run_fingerprint") == fingerprints["run_fingerprint"]
                    and str(previous.get("fingerprint_version")) == str(FINGERPRINT_VERSION)):
                record_event(history_dir, previous["id"], created=False)
                return f"{previous.get('version') or '?'} ({previous['id']})", False

    # second-resolution ids collide when two solves land in the same
    # second (small rounds solve in well under a second)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    version_id, n = stamp, 1
    while (history_dir / version_id).exists():
        n += 1
        version_id = f"{stamp}-{n}"
    version_dir = history_dir / version_id
    version_dir.mkdir(parents=True, exist_ok=True)

    with open(version_dir / "service.csv", "w", newline="",
              encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["edge_id", "service", "delivery", "name",
                                          "length_m"])
        w.writeheader()
        w.writerows(rows)

    full = dict(summary)
    full.update(fingerprints, input_files=input_files, configuration=configuration)
    full.update(run_details(summary, configuration))
    full.update(id=version_id, note=note, annotation=digest,
                version=next_version(index[-1] if index else None,
                                     summary, digest, bump),
                timestamp=datetime.now().isoformat(timespec="seconds"))
    (version_dir / "summary.json").write_text(
        json.dumps(full, indent=1, ensure_ascii=False), encoding="utf-8")

    if out_dir:
        if route_bytes is not None:
            (version_dir / "route.csv").write_bytes(route_bytes)
        src = out_dir / "route_map.html"
        if src.exists():
            shutil.copy2(src, version_dir / src.name)

    index.append({k: full.get(k, "") for k in FIELDS})
    write_index(history_dir, index)
    record_event(history_dir, version_id, created=True)
    return f"{full['version']} ({version_id})", True


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def resolve(index, ref):
    """Version id from a version number ("1.2.0"), an id or id prefix,
    or a negative index (-1 = newest)."""
    if not index:
        sys.exit("no versions recorded yet -- solve with --history first")
    if ref is None:
        return index[-1]["id"]
    if ref.startswith("-") and ref[1:].isdigit():
        try:
            return index[int(ref)]["id"]
        except IndexError:
            sys.exit(f"only {len(index)} version(s) recorded")
    versions = [r["id"] for r in index if r.get("version") == ref]
    if versions:
        return versions[-1]
    # an exact id always wins: collision suffixes ("...-2") make one id
    # a prefix of another, which would otherwise read as ambiguous
    if any(r["id"] == ref for r in index):
        return ref
    hits = [r["id"] for r in index if r["id"].startswith(ref)]
    if not hits:
        sys.exit(f"no version matches {ref!r}")
    if len(hits) > 1:
        sys.exit(f"{ref!r} matches {len(hits)} versions: "
                 f"{', '.join(hits[:5])}")
    return hits[0]


def cmd_list(history_dir: Path, _args):
    index = read_index(history_dir)
    if not index:
        sys.exit("no versions recorded yet -- solve with --history first")
    print(f"{'ver':<9}{'date':<12}{'service':>8}{'total':>8}{'dead':>7}"
          f"{'time':>7}{'oneway':>7}{'svc-cf':>8}{'X':>4}{'U':>4}{'pair':>6}  run / selected / profile / selection | note")
    prev = None
    for r in with_run_details(history_dir, index):
        total = float(r["total_km"])
        mins = float(r.get("time_min") or 0)
        delta = ""
        if prev is not None:
            delta = f" ({total - prev[0]:+.2f} km"
            if mins and prev[1]:
                delta += f", {mins - prev[1]:+.0f} min"
            delta += ")"
        prev = (total, mins)
        time = f"{int(mins) // 60}h{int(mins) % 60:02d}" if mins else "-"
        note = r["note"] or r["mode"]
        cf = r.get("service_counterflow_km")
        cf_text = f"{float(cf):.2f}k" if cf not in (None, "") else "-"
        print(f"{(r.get('version') or '-'):<9}{r['id'][:8]:<12}"
              f"{float(r['mandatory_km']):>7.2f}k"
              f"{total:>7.2f}k{float(r['deadhead_km']):>6.2f}k"
              f"{time:>7}{float(r.get('wrong_way_km') or 0):>6.2f}k{cf_text:>8}"
              f"{(r.get('turns_cross') or '-'):>4}{(r.get('turns_u') or '-'):>4}"
              f"{(r.get('paired_pct') or '-'):>5}%"
              f"  {r['run_description']} | {note[:30]}{delta}")
    print(f"\n{len(index)} version(s) in {history_dir}")
    print("oneway: against mapped one-ways, all riding; "
          "svc-cf: service against reference traffic flow. "
          "These overlap; '-' means not recorded.")


def cmd_show(history_dir: Path, args):
    index = read_index(history_dir)
    vid = resolve(index, args.version)
    vdir = history_dir / vid
    data = json.loads((vdir / "summary.json").read_text(encoding="utf-8"))
    for key, value in data.items():
        print(f"  {key:16s} {value}")
    for name in ("route.csv", "route_map.html"):
        if (vdir / name).exists():
            print(f"  {name:16s} {vdir / name}")


def cmd_timeline(history_dir: Path, _args):
    rows = read_timeline(history_dir)
    if not rows:
        sys.exit("no versions recorded yet -- solve with --history first")
    print("time | action | version (archive id) | run / selected / profile / selection")
    for row in rows:
        print(f"{row['event_timestamp']} | {row['event_action']} | "
              f"{row.get('version') or '?'} ({row['id']}) | {row['run_description']}")


def cmd_diff(history_dir: Path, args):
    index = read_index(history_dir)
    if args.old is None and args.new is None:
        timeline = read_timeline(history_dir)
        if len(timeline) < 2:
            sys.exit("need at least two timeline entries to diff")
        old_id, new_id = timeline[-2]["id"], timeline[-1]["id"]
    else:
        old_id = resolve(index, args.old if args.old else "-2")
        new_id = resolve(index, args.new if args.new else "-1")
    old, new = read_service(history_dir / old_id), \
        read_service(history_dir / new_id)
    by_id = {r["id"]: r for r in index}

    added = [e for k, e in new.items() if k not in old]
    removed = [e for k, e in old.items() if k not in new]
    changed = [(old[k], e) for k, e in new.items()
               if k in old and (old[k]["service"], old[k]["delivery"])
               != (e["service"], e["delivery"])]

    def metres(rows, key=lambda r: r):
        return sum(float(key(r)["length_m"]) for r in rows)

    print(f"{old_id}  ->  {new_id}\n")
    for label, rows, sign in (("added to the round", added, "+"),
                              ("removed from the round", removed, "-")):
        if rows:
            print(f"{label}: {len(rows)} edge(s), {metres(rows):.0f} m")
            for r in sorted(rows, key=lambda r: -float(r["length_m"]))[:8]:
                print(f"   {sign} {r['name'][:30]:32s} "
                      f"service={r['service']}  {float(r['length_m']):.0f} m")
            if len(rows) > 8:
                print(f"     ... and {len(rows) - 8} more")
    if changed:
        print(f"changed value: {len(changed)} edge(s)")
        for a, b in sorted(changed,
                           key=lambda p: -float(p[1]["length_m"]))[:8]:
            print(f"   ~ {b['name'][:30]:32s} "
                  f"{a['service']} -> {b['service']}  "
                  f"{a['delivery']} -> {b['delivery']}  "
                  f"{float(b['length_m']):.0f} m")
        if len(changed) > 8:
            print(f"     ... and {len(changed) - 8} more")
    if not (added or removed or changed):
        print("the round definition is identical")

    a, b = by_id.get(old_id), by_id.get(new_id)
    if a and b:
        print("\n              before     after     change")
        for label, key in (("mandatory", "mandatory_km"),
                           ("total", "total_km"),
                           ("deadhead", "deadhead_km"),
                           ("bridging", "bridge_km")):
            x, y = float(a[key] or 0), float(b[key] or 0)
            print(f"  {label:10s} {x:8.2f}km{y:9.2f}km{y - x:+9.2f}km")
        print(f"  {'islands':10s} {a['islands']:>8}  {b['islands']:>9}")
        x, y = a.get("service_counterflow_km"), b.get("service_counterflow_km")
        before = f"{float(x):.2f}km" if x not in (None, "") else "-"
        after = f"{float(y):.2f}km" if y not in (None, "") else "-"
        change = (f"{float(y) - float(x):+.2f}km"
                  if x not in (None, "") and y not in (None, "") else "-")
        print(f"  {'svc-cf':10s} {before:>10}{after:>11}{change:>11}")
        print("\nExperiment fingerprints:")
        for label, key in (("input files", "input_fingerprint"),
                           ("configuration", "config_fingerprint"),
                           ("route order/data", "route_fingerprint")):
            if (not a.get(key) or not b.get(key)
                    or a.get("fingerprint_version") != b.get("fingerprint_version")):
                status = "unknown (missing or incompatible fingerprint)"
            else:
                status = "unchanged" if a[key] == b[key] else "changed"
            print(f"  {label}: {status}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--history", default=DEFAULT_HISTORY,
                    help=f"history directory (default {DEFAULT_HISTORY})")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("list", help="all recorded versions, newest last")
    sub.add_parser("timeline", help="observed version transitions, including reuse, newest last")
    show = sub.add_parser("show", help="one version in full")
    show.add_argument("version", nargs="?")
    diff = sub.add_parser("diff", help="compare the latest timeline entries or explicit archives",
                         description="Without references, compare the latest two timeline entries. "
                         "Explicit references use archives; with only OLD, NEW is the newest archive.")
    diff.add_argument("old", nargs="?", help="archive version, id/prefix or negative index")
    diff.add_argument("new", nargs="?", help="archive reference (default with OLD: newest archive)")
    args = ap.parse_args()

    history_dir = Path(args.history)
    if not history_dir.exists():
        sys.exit(f"{history_dir} does not exist -- solve with --history "
                 f"first")
    {"list": cmd_list, "show": cmd_show, "timeline": cmd_timeline,
     "diff": cmd_diff}.get(args.cmd, cmd_list)(history_dir, args)


if __name__ == "__main__":
    main()
