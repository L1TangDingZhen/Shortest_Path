#!/usr/bin/env python3
"""Version history for solved rounds.

Every solve can be filed away with the annotation that produced it, so
you can see how the round evolved: which streets you added or dropped,
and what that did to the distance.

    python solve_route.py --data data --out result --history
        ... solve as usual and record a version (default location:
        round.local/history)

    python round_history.py list
    python round_history.py show <id>
    python round_history.py diff <old> <new>
        <id> may be a version number ("1.2.0"), a timestamp id or its
        prefix, or a negative index: -1 is the newest version, -2 the
        one before it.  `diff` with no arguments compares the two
        newest versions.

Versions are numbered like an app release, so the number tells you how
big the change was: major = the network was re-extracted or the round
changed substantially, minor = streets added/dropped/changed, patch =
same round solved with different settings.

Layout (inside the gitignored round directory):

    history/index.csv          one row per version, for quick listing
    history/<id>/summary.json  totals, endpoints, mode, counts
    history/<id>/service.csv   the round definition at that moment
                               (every edge with service 1/2/x)
    history/<id>/route.csv     the emitted route
    history/<id>/route_map.html   the map, openable months later

A version is only filed when something actually differs from the newest
one -- pressing Solve twice on an unchanged round does not pile up
duplicates.
"""

import argparse
import csv
import hashlib
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_HISTORY = "round.local/history"
INDEX = "index.csv"


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
          "runs_per_street", "edges_total", "annotation"]


# ----------------------------------------------------------------------
# reading / writing
# ----------------------------------------------------------------------

def read_annotation(data_dir: Path):
    """The round definition: every edge that is not a plain connector.
    Returns (rows, short hash of the whole annotation)."""
    rows = []
    with open(data_dir / "edges.csv", newline="",
              encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            service = (r.get("service") or "").strip().lower()
            if service in ("1", "2", "x"):
                rows.append({"edge_id": r["edge_id"], "service": service,
                             "name": r.get("name") or "",
                             "length_m": r.get("length_m") or "0"})
    rows.sort(key=lambda r: r["edge_id"])
    blob = "\n".join(f'{r["edge_id"]}={r["service"]}' for r in rows)
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


def read_service(version_dir: Path):
    with open(version_dir / "service.csv", newline="",
              encoding="utf-8-sig") as f:
        return {r["edge_id"]: r for r in csv.DictReader(f)}


# ----------------------------------------------------------------------
# recording
# ----------------------------------------------------------------------

def record(history_dir: Path, data_dir: Path, summary: dict,
           out_dir: Path = None, note: str = "", bump: str = None):
    """File one solved version. Returns (version_id, is_new)."""
    rows, digest = read_annotation(data_dir)
    index = read_index(history_dir)

    if index:
        last = index[-1]
        if (last["annotation"] == digest
                and last["mode"] == summary.get("mode", "")
                and abs(float(last["total_km"]) -
                        summary.get("total_km", 0)) < 5e-4):
            return f"{last.get('version') or '?'} ({last['id']})", False

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
        w = csv.DictWriter(f, fieldnames=["edge_id", "service", "name",
                                          "length_m"])
        w.writeheader()
        w.writerows(rows)

    full = dict(summary)
    full.update(id=version_id, note=note, annotation=digest,
                version=next_version(index[-1] if index else None,
                                     summary, digest, bump),
                timestamp=datetime.now().isoformat(timespec="seconds"))
    (version_dir / "summary.json").write_text(
        json.dumps(full, indent=1, ensure_ascii=False), encoding="utf-8")

    if out_dir:
        for name in ("route.csv", "route_map.html"):
            src = out_dir / name
            if src.exists():
                shutil.copy2(src, version_dir / name)

    index.append({k: full.get(k, "") for k in FIELDS})
    write_index(history_dir, index)
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
          f"{'time':>7}{'wrong':>7}{'X':>4}{'U':>4}{'pair':>6}  note")
    prev = None
    for r in index:
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
        print(f"{(r.get('version') or '-'):<9}{r['id'][:8]:<12}"
              f"{float(r['mandatory_km']):>7.2f}k"
              f"{total:>7.2f}k{float(r['deadhead_km']):>6.2f}k"
              f"{time:>7}{float(r.get('wrong_way_km') or 0):>6.2f}k"
              f"{(r.get('turns_cross') or '-'):>4}{(r.get('turns_u') or '-'):>4}"
              f"{(r.get('paired_pct') or '-'):>5}%"
              f"  {note[:30]}{delta}")
    print(f"\n{len(index)} version(s) in {history_dir}")


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


def cmd_diff(history_dir: Path, args):
    index = read_index(history_dir)
    if args.old is None and args.new is None and len(index) < 2:
        sys.exit("need at least two versions to diff")
    old_id = resolve(index, args.old if args.old else "-2")
    new_id = resolve(index, args.new if args.new else "-1")
    old, new = read_service(history_dir / old_id), \
        read_service(history_dir / new_id)
    by_id = {r["id"]: r for r in index}

    added = [e for k, e in new.items() if k not in old]
    removed = [e for k, e in old.items() if k not in new]
    changed = [(old[k], e) for k, e in new.items()
               if k in old and old[k]["service"] != e["service"]]

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


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--history", default=DEFAULT_HISTORY,
                    help=f"history directory (default {DEFAULT_HISTORY})")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("list", help="all recorded versions, newest last")
    show = sub.add_parser("show", help="one version in full")
    show.add_argument("version", nargs="?")
    diff = sub.add_parser("diff", help="what changed between two versions")
    diff.add_argument("old", nargs="?")
    diff.add_argument("new", nargs="?")
    args = ap.parse_args()

    history_dir = Path(args.history)
    if not history_dir.exists():
        sys.exit(f"{history_dir} does not exist -- solve with --history "
                 f"first")
    {"list": cmd_list, "show": cmd_show,
     "diff": cmd_diff}.get(args.cmd, cmd_list)(history_dir, args)


if __name__ == "__main__":
    main()
