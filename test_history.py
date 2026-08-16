#!/usr/bin/env python3
"""Offline tests for round_history.py (version history).

Solves the synthetic round three times -- unchanged, then with a street
dropped, then with one added back differently -- and asserts:

  * --history files a version: index row, service.csv, summary.json,
    and copies of route.csv / route_map.html
  * an unchanged re-solve is NOT filed again (no duplicate versions)
  * changing the annotation files a new version with the new totals
  * list shows every version; diff names the edges that changed and
    the distance deltas; negative indices resolve (-1 newest)
  * a bad version reference exits non-zero

Run:  python test_history.py
"""

import csv
import json
import shutil
import subprocess
import sys
from pathlib import Path

from test_solve import write_case
from test_split import set_service

BASE = Path(__file__).parent
TMP = BASE / "_test_tmp" / "history"


def solve(data_dir, out_dir, history, *extra):
    res = subprocess.run(
        [sys.executable, str(BASE / "solve_route.py"),
         "--data", str(data_dir), "--out", str(out_dir),
         "--history", str(history), *extra],
        capture_output=True, text=True)
    assert res.returncode == 0, res.stderr or res.stdout
    return res.stdout


def history(history_dir, *args):
    return subprocess.run(
        [sys.executable, str(BASE / "round_history.py"),
         "--history", str(history_dir), *args],
        capture_output=True, text=True)


def index_rows(history_dir):
    with open(history_dir / "index.csv", newline="",
              encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def main():
    if TMP.exists():
        shutil.rmtree(TMP)
    data, out, hist = TMP / "data", TMP / "out", TMP / "hist"
    write_case(data)

    print("== first solve files a version ==")
    stdout = solve(data, out, hist, "--note", "baseline")
    assert "recorded version" in stdout, stdout
    rows = index_rows(hist)
    assert len(rows) == 1, rows
    v1 = rows[0]["id"]
    vdir = hist / v1
    for name in ("service.csv", "summary.json", "route.csv",
                 "route_map.html"):
        assert (vdir / name).exists(), f"{name} not archived"
    summary = json.loads((vdir / "summary.json").read_text(
        encoding="utf-8"))
    assert summary["note"] == "baseline"
    assert summary["service_edges"] > 0
    assert float(rows[0]["total_km"]) >= float(rows[0]["mandatory_km"])
    with open(vdir / "service.csv", newline="",
              encoding="utf-8-sig") as f:
        stored = {r["edge_id"]: r["service"] for r in csv.DictReader(f)}
    assert stored["n01-court-0"] == "2"
    assert all(v in ("1", "2", "x") for v in stored.values())

    print("== identical re-solve is not filed twice ==")
    stdout = solve(data, out, hist)
    assert "unchanged since version" in stdout, stdout
    assert len(index_rows(hist)) == 1

    print("== changed annotation files a new version ==")
    set_service(data, "n01-court-0", 0)          # drop the court
    stdout = solve(data, out, hist, "--note", "court dropped")
    assert "recorded version" in stdout, stdout
    rows = index_rows(hist)
    assert len(rows) == 2, rows
    v2 = rows[1]["id"]
    assert float(rows[1]["mandatory_km"]) < float(rows[0]["mandatory_km"])
    assert int(rows[1]["service_edges"]) == int(rows[0]["service_edges"]) - 1

    print("== versions are numbered like a release ==")
    assert rows[0]["version"] == "1.0.0", rows[0]["version"]
    # a single street dropped is a small edit, whatever the percentage
    assert rows[1]["version"] == "1.1.0", rows[1]["version"]

    print("== list ==")
    res = history(hist, "list")
    assert res.returncode == 0, res.stderr
    assert "1.0.0" in res.stdout and "1.1.0" in res.stdout
    assert "court dropped" in res.stdout

    print("== a version number resolves like an id ==")
    res = history(hist, "show", "1.1.0")
    assert res.returncode == 0 and v2 in res.stdout, res.stdout

    print("== diff (defaults to the last two) ==")
    res = history(hist, "diff")
    assert res.returncode == 0, res.stderr
    assert "Quiet Court" in res.stdout, res.stdout
    assert "removed from the round" in res.stdout
    assert "mandatory" in res.stdout and "-" in res.stdout

    print("== diff by explicit ids and negative index ==")
    res = history(hist, "diff", v1, v2)
    assert res.returncode == 0 and "Quiet Court" in res.stdout
    res = history(hist, "show", "-1")
    assert res.returncode == 0 and v2 in res.stdout, res.stdout

    print("== value change is reported as a change, not add/remove ==")
    set_service(data, "n01-court-0", 1)          # back, but one side
    solve(data, out, hist, "--note", "court one side")
    res = history(hist, "diff", "-2", "-1")
    assert "added to the round" in res.stdout, res.stdout

    print("== a bulk edit bumps the major number ==")
    for eid in ("n00-n10-0", "n10-n20-0", "n03-n13-0", "n13-n23-0"):
        set_service(data, eid, 0)
    solve(data, out, hist, "--note", "avenues dropped")
    rows = index_rows(hist)
    assert rows[-1]["version"].startswith("2."), \
        f"a four-street change should be major: {rows[-1]['version']}"

    print("== --bump forces the number ==")
    set_service(data, "n00-n10-0", 1)
    solve(data, out, hist, "--bump", "patch", "--note", "forced patch")
    rows = index_rows(hist)
    assert rows[-1]["version"] == "2.0.1", rows[-1]["version"]

    print("== unknown version reference fails ==")
    res = history(hist, "show", "nope")
    assert res.returncode != 0

    print("== an exact id beats prefix ambiguity ==")
    sys.path.insert(0, str(BASE))
    import round_history
    fake = [{"id": "20260812-120000"}, {"id": "20260812-120000-2"}]
    assert round_history.resolve(fake, "20260812-120000") == \
        "20260812-120000", "exact id must not be ambiguous"
    assert round_history.resolve(fake, "-1") == "20260812-120000-2"

    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
