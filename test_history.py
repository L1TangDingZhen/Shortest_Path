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

import round_history
from test_solve import write_case, write_simple
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


def test_legacy_diff():
    hist = TMP / "legacy"
    fields = ["edge_id", "service", "name", "length_m"]
    old = [dict(edge_id="both", service="2", name="Demo Street", length_m="100"),
           dict(edge_id="single", service="1", name="Side Street", length_m="80"),
           dict(edge_id="banned", service="x", name="Steps Lane", length_m="20")]
    versions = []
    for name, modes in (("old", None), ("blank", ["", "", ""]),
                        ("equivalent", ["both", "unspecified", "none"]),
                        ("confirmed", ["both", "left", "none"]),
                        ("switched", ["both", "right", "none"])):
        folder = hist / name
        folder.mkdir(parents=True)
        with open(folder / "service.csv", "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fields + (["delivery"] if modes else []))
            writer.writeheader()
            writer.writerows([dict(r, delivery=mode) for r, mode in zip(old, modes)]
                             if modes else old)
        row = dict(id=name, mode="test", total_km="0.28", mandatory_km="0.28",
                   deadhead_km="0", bridge_km="0", islands="1", note="")
        if modes:
            row.update(service_counterflow_km="0", service_counterflow_segments="0")
        versions.append(row)
    round_history.write_index(hist, versions)
    before = {p: p.read_bytes() for p in hist.rglob("*.csv")}

    for equivalent in ("blank", "equivalent"):
        res = history(hist, "diff", "old", equivalent)
        assert res.returncode == 0 and "the round definition is identical" in res.stdout, res.stdout
    cf_line = next(line for line in res.stdout.splitlines() if "svc-cf" in line)
    assert cf_line.split()[1:] == ["-", "0.00km", "-"], cf_line
    res = history(hist, "diff", "old", "confirmed")
    assert res.returncode == 0 and "changed value: 1 edge(s)" in res.stdout, res.stdout
    assert "unspecified -> left" in res.stdout
    assert "Demo Street" not in res.stdout and "Steps Lane" not in res.stdout
    res = history(hist, "diff", "confirmed", "switched")
    assert "changed value: 1 edge(s)" in res.stdout and "left -> right" in res.stdout
    assert before == {p: p.read_bytes() for p in hist.rglob("*.csv")}, "diff rewrote history"

    print("== older index schemas retain unknown rather than inventing zero ==")
    old_fields = [k for k in round_history.FIELDS if not k.startswith("service_counterflow_")]
    with open(hist / "index.csv", "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=old_fields)
        writer.writeheader()
        writer.writerow(versions[0])
    res = history(hist, "list")
    assert res.returncode == 0 and "svc-cf" in res.stdout, res.stdout
    values = res.stdout.splitlines()[1].split()
    assert values[7] == "-", values


def test_counterflow_history():
    data, hist = TMP / "direction_data", TMP / "direction_history"
    write_case(data)
    out = TMP / "direction_out"
    out.mkdir()
    (out / "route.csv").write_text("seq,edge_id\n1,demo\n")
    base = dict(mode="test", total_km=1.0, mandatory_km=0.5,
                deadhead_km=0.5, service_edges=1)
    assert round_history.record(hist, data, base)[1]
    rows = round_history.read_index(hist)
    old_archive = hist / rows[0]["id"]
    archived = {p: p.read_bytes() for p in old_archive.iterdir()}
    old_fields = [k for k in round_history.FIELDS if not k.startswith("service_counterflow_")]
    with open(hist / "index.csv", "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=old_fields)
        writer.writeheader()
        writer.writerow({key: rows[0][key] for key in old_fields})

    # First measured zero is different from an old, unmeasured value.
    for km, count in ((0.0, 0), (0.125, 1), (0.125, 2)):
        summary = dict(base, service_counterflow_km=km,
                       service_counterflow_segments=count)
        assert round_history.record(hist, data, summary, out,
                                    configuration={"case": "counterflow"})[1]
        assert not round_history.record(hist, data, summary, out,
                                        configuration={"case": "counterflow"})[1]
    rows = round_history.read_index(hist)
    assert len(rows) == 4
    assert rows[0]["service_counterflow_km"] == ""
    assert rows[0]["service_counterflow_segments"] == ""
    assert float(rows[1]["service_counterflow_km"]) == 0
    assert archived == {p: p.read_bytes() for p in old_archive.iterdir()}


def edit_field(path, index, key, value):
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fields, rows = reader.fieldnames, list(reader)
    if key not in fields:
        fields.append(key)
    rows[index][key] = value
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def test_fingerprints():
    print("== same totals do not merge distinct inputs, configs or route orders ==")
    data, out, hist = TMP / "fingerprint_data", TMP / "fingerprint_out", TMP / "fingerprints"
    write_case(data)
    out.mkdir()
    route = "seq,edge_id,delivery_side,travel_direction\n1,a,left,forward\n2,b,right,reverse\n"
    (out / "route.csv").write_text(route)
    summary = dict(mode="test", total_km=1.0, mandatory_km=0.5, deadhead_km=0.5,
                   service_edges=14, edges_total=18, time_min=10)
    config = {"algorithm": "node", "profile": {"speeds": {"footway": 5}}, "penalty": 1}

    def record(**kwargs):
        return round_history.record(hist, data, summary, out, configuration=config, **kwargs)

    assert record()[1]
    assert not record()[1]
    original = round_history.read_index(hist)[-1]
    assert all(len(original[k]) == 64 for k in
               ("input_fingerprint", "config_fingerprint", "route_fingerprint", "run_fingerprint"))
    # JSON key order, output path and CSV newline/BOM are not experiment settings.
    config = dict(penalty=1, profile={"speeds": {"footway": 5}}, algorithm="node")
    (out / "route.csv").write_bytes(("\ufeff" + route.replace("\n", "\r\n")).encode())
    assert not record()[1]
    config["penalty"] = 3
    assert record()[1] and not record()[1]
    config["profile"]["speeds"]["footway"] = 6
    assert record()[1]
    assert round_history.read_index(hist)[-1]["input_fingerprint"] == original["input_fingerprint"]
    for key, value in (("maxspeed_kmh", "25"), ("geometry_wkt", "LINESTRING (144.95 -37.845, 144.951 -37.844)")):
        edit_field(data / "edges.csv", 0, key, value)
        assert record()[1], key
    with (data / "edges.csv").open(newline="", encoding="utf-8-sig") as f:
        connector = next(i for i, row in enumerate(csv.DictReader(f)) if row["service"] == "0")
    edit_field(data / "edges.csv", connector, "length_m", "999")
    assert record()[1], "connector changes are inputs, not just annotated roads"
    edit_field(data / "nodes.csv", 0, "control", "signals")
    assert record()[1]
    previous = round_history.read_index(hist)[-1]
    (out / "route.csv").write_text(
        "seq,edge_id,delivery_side,travel_direction\n1,b,right,reverse\n2,a,left,forward\n")
    assert record()[1] and not record()[1]
    current = round_history.read_index(hist)[-1]
    assert current["input_fingerprint"] == previous["input_fingerprint"]
    assert current["config_fingerprint"] == previous["config_fingerprint"]
    assert current["route_fingerprint"] != previous["route_fingerprint"]
    report = history(hist, "diff")
    assert "configuration: unchanged" in report.stdout
    assert "route order/data: changed" in report.stdout
    summary["time_min"] = 11
    assert record()[1], "changed metrics at identical km must be retained"
    assert record(note="repeat measurement")[1]
    assert not record()[1]
    assert record(bump="major")[1]
    assert round_history.read_index(hist)[-1]["version"].endswith(".0.0")
    assert not record()[1]

    print("== legacy index without fingerprints creates one new baseline ==")
    archives = {p: p.read_bytes() for p in hist.glob("*/*") if p.is_file()}
    rows = round_history.read_index(hist)
    old_fields = [k for k in round_history.FIELDS if "fingerprint" not in k]
    with (hist / "index.csv").open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=old_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    assert record()[1] and not record()[1]
    assert len(round_history.read_index(hist)) == len(rows) + 1
    assert all(p.read_bytes() == raw for p, raw in archives.items())
    assert "unknown (missing or incompatible fingerprint)" in history(hist, "diff").stdout

    print("== missing route or effective config never proves an unchanged run ==")
    (out / "route.csv").unlink()
    assert record()[1] and record()[1]
    (out / "route.csv").write_text(route)
    assert round_history.record(hist, data, summary, out)[1]
    assert round_history.record(hist, data, summary, out)[1]

    print("== history uses the input snapshot, not edits made during solving ==")
    inputs = round_history.capture_inputs(data)
    before_rows, _ = round_history.read_annotation(data)
    set_service(data, "n01-court-0", 0)
    assert record(inputs=inputs)[1]
    latest = hist / round_history.read_index(hist)[-1]["id"]
    archived_rows = round_history.read_service(latest)
    assert archived_rows["n01-court-0"]["service"] == next(
        r["service"] for r in before_rows if r["edge_id"] == "n01-court-0")
    import solve_route
    _, edges, _ = solve_route.load_data(data, inputs)
    assert next(e["service"] for e in edges if e["edge_id"] == "n01-court-0") == 2
    stored = json.loads((latest / "summary.json").read_text())
    assert stored["input_files"] == {name: round_history.hash_bytes(raw) for name, raw in inputs.items()}


def test_solver_experiments():
    print("== actual solver settings are retained even when the distance cannot change ==")
    data, out, hist = TMP / "experiment_data", TMP / "experiment_out", TMP / "experiments"
    write_simple(data, {"A": (-37.845, 144.950), "B": (-37.845, 144.951)},
                 [("only", "Demo Street", "A", "B", 2)])
    flags = [(), ("--wrong-way-penalty", "3"), ("--vehicle", "carriageway"),
             ("--profile", "limits"), ("--algorithm", "turn"),
             ("--traffic-side", "right"), ("--pair-passes",), ("--street-first",),
             ("--no-turn-optimisation",), ("--no-tour-search",),
             ("--search-iterations", "1"), ("--start=-37.845,144.950",),
             ("--start=-37.845,144.951",), ("--end=-37.845,144.951",),
             ("--start=-37.845,144.950", "--end=-37.845,144.951", "--return-to-start")]
    for args in flags:
        count = len(round_history.read_index(hist))
        assert "recorded version" in solve(data, out, hist, *args), args
        assert len(round_history.read_index(hist)) == count + 1
        assert "matches existing version" in solve(data, out, hist, *args), args
    records = [json.loads((hist / r["id"] / "summary.json").read_text())
               for r in round_history.read_index(hist)]
    # The first eleven experiments must all traverse the same two-sided edge.
    assert len({r["total_km"] for r in records[:11]}) == 1
    assert records[2]["configuration"]["variants"][0]["wrong_way_penalty"] == 12
    assert records[4]["configuration"]["variants"][0]["algorithm"] == "turn"
    assert records[5]["configuration"]["traffic_side"] == "right"
    assert records[9]["configuration"]["search_iterations"] == 0

    print("== custom profile contents and all comparison candidates are recorded ==")
    custom = data / "custom.json"
    for speed in (10, 20):
        custom.write_text(json.dumps({"speeds": {"residential": speed}}))
        assert "recorded version" in solve(data, out, hist, "--profile", str(custom))
        last = json.loads((hist / round_history.read_index(hist)[-1]["id"] / "summary.json").read_text())
        assert last["configuration"]["variants"][0]["profile"]["speeds"]["residential"] == speed
        assert "matches existing version" in solve(data, out, hist, "--profile", str(custom))
    for variants in ("fastest,shortest", "fastest,shortest,recommended"):
        assert "recorded version" in solve(data, out, hist, "--variants", variants)
        last = json.loads((hist / round_history.read_index(hist)[-1]["id"] / "summary.json").read_text())
        assert [v["name"] for v in last["configuration"]["variants"]] == variants.split(",")
        assert last["configuration"]["comparison_scoring_profile"]
        assert "matches existing version" in solve(data, out, hist, "--variants", variants)
    for extra in (("--note", "explicit repeat"), ("--bump", "patch")):
        assert "recorded version" in solve(data, out, hist, "--variants", variants, *extra)
        assert "matches existing version" in solve(data, out, hist, "--variants", variants)


def test_alternating_history():
    print("== Solve/Solve/Compare/Compare/Solve reuses two historical versions ==")
    data, hist = TMP / "alternating_data", TMP / "alternating_history"
    write_simple(data, {"A": (-37.845, 144.950), "B": (-37.845, 144.951)},
                 [("only", "Demo Street", "A", "B", 2)])
    known = {}
    for compare in (False, False, True, True, False, True, False):
        out = TMP / ("alternating_compare" if compare else "alternating_solve")
        extra = ("--variants", "all") if compare else ()
        stdout = solve(data, out, hist, *extra)
        rows = round_history.read_index(hist)
        if compare in known:
            assert f"matches existing version {known[compare]['version']} ({known[compare]['id']})" in stdout
        else:
            assert "recorded version" in stdout
            known[compare] = rows[-1]
        assert len(rows) == len(known)
    rows = round_history.read_index(hist)
    assert [r["selection"] for r in rows] == ["single", "minimum_comparison_time"]
    assert rows[0]["variant"] == "node" and rows[0]["algorithm"] == "node"
    assert rows[0]["variant_label"] == "edv"
    assert rows[1]["variant"] and rows[1]["variant_label"] and rows[1]["algorithm"]
    events = round_history.read_events(hist)
    assert [e["version_id"] for e in events] == [known[c]["id"] for c in (False, True, False, True, False)]
    assert [e["event_action"] for e in events] == ["created", "created", "reused", "reused", "reused"]
    listed = history(hist, "list")
    assert "Solve: edv [node] / edv / single" in listed.stdout, listed.stdout
    assert "Compare:" in listed.stdout and "/ min time" in listed.stdout
    before = {p: p.read_bytes() for p in hist.rglob("*") if p.is_file()}
    solve(data, TMP / "another_output_directory", hist)
    assert before == {p: p.read_bytes() for p in hist.rglob("*") if p.is_file()}
    for flags in (("--note", "explicit repeat"), ("--bump", "patch")):
        assert "recorded version" in solve(data, TMP / "alternating_solve", hist, *flags)
    assert len(round_history.read_index(hist)) == 4


def test_annotation_reversion():
    print("== A/B/A annotation changes retain a timeline without copying A ==")
    data, out, hist = TMP / "revert_data", TMP / "revert_out", TMP / "revert_history"
    write_simple(data, {"A": (0.0, 0.0), "B": (0.0, 0.001), "C": (0.0, 0.002)},
                 [("first", "Demo Street", "A", "B", 1),
                  ("second", "Side Street", "B", "C", 1)])
    original = (data / "edges.csv").read_bytes()
    solve(data, out, hist)
    first = round_history.read_index(hist)[0]
    report = history(hist, "diff")
    assert report.returncode != 0 and "two timeline entries" in report.stderr
    edit_field(data / "edges.csv", 1, "service", "0")
    edit_field(data / "edges.csv", 1, "delivery", "none")
    solve(data, out, hist)
    second = round_history.read_index(hist)[-1]
    archives = {p: p.read_bytes() for p in hist.glob("*/*") if p.is_file()}
    index = (hist / "index.csv").read_bytes()
    (data / "edges.csv").write_bytes(original)
    assert "matches existing version" in solve(data, out, hist)
    assert (hist / "index.csv").read_bytes() == index
    assert all(p.read_bytes() == raw for p, raw in archives.items())
    timeline = round_history.read_timeline(hist)
    assert [e["id"] for e in timeline] == [first["id"], second["id"], first["id"]]
    assert [e["event_action"] for e in timeline] == ["created", "created", "reused"]
    assert [e["event_seq"] for e in timeline] == ["1", "2", "3"]
    assert timeline[-1]["event_timestamp"] >= timeline[1]["event_timestamp"]
    before = (hist / "events.csv").read_bytes()
    solve(data, out, hist)
    assert (hist / "events.csv").read_bytes() == before
    report = history(hist, "timeline")
    assert report.returncode == 0 and "reused" in report.stdout
    assert report.stdout.count(f"({first['id']})") == 2

    print("== default diff follows B -> A, while explicit references still use archives ==")
    untouched = {p: p.read_bytes() for p in hist.rglob("*") if p.is_file()}
    report = history(hist, "diff")
    assert report.returncode == 0, report.stderr
    assert report.stdout.startswith(f"{second['id']}  ->  {first['id']}\n")
    assert "added to the round: 1 edge(s), 111 m" in report.stdout
    assert "removed from the round" not in report.stdout
    for refs in ((first["id"], second["id"]), (first["version"], second["version"]),
                 ("-2", "-1"), (first["id"],)):
        explicit = history(hist, "diff", *refs)
        assert explicit.returncode == 0, explicit.stderr
        assert explicit.stdout.startswith(f"{first['id']}  ->  {second['id']}\n")
        assert "removed from the round: 1 edge(s), 111 m" in explicit.stdout
    assert untouched == {p: p.read_bytes() for p in hist.rglob("*") if p.is_file()}

    print("== old archives gain only observed reuse, not invented earlier events ==")
    (hist / "events.csv").unlink()
    assert [e["event_action"] for e in round_history.read_timeline(hist)] == ["archived", "archived"]
    report = history(hist, "diff")
    assert report.returncode == 0 and report.stdout.startswith(f"{first['id']}  ->  {second['id']}\n")
    assert "removed from the round: 1 edge(s), 111 m" in report.stdout
    assert not (hist / "events.csv").exists(), "reading a legacy timeline must not write it"
    solve(data, out, hist)
    timeline = round_history.read_timeline(hist)
    assert [e["id"] for e in timeline] == [first["id"], second["id"], first["id"]]
    assert [e["event_action"] for e in timeline] == ["archived", "archived", "reused"]
    report = history(hist, "diff")
    assert report.returncode == 0 and report.stdout.startswith(f"{second['id']}  ->  {first['id']}\n")
    assert "added to the round: 1 edge(s), 111 m" in report.stdout
    assert (hist / "index.csv").read_bytes() == index
    assert all(p.read_bytes() == raw for p, raw in archives.items())


def test_run_details_compatibility():
    print("== older history labels use archived evidence, never guess from a variant ==")
    hist = TMP / "legacy_labels"
    versions = []
    for name, summary in (
            ("single", {"variant": "node", "configuration": {"selection": "single",
                "variants": [{"name": "node", "label": "edv", "algorithm": "node"}]}}),
            ("compare", {"variant": "recommended", "configuration": {
                "selection": "minimum_comparison_time", "variants": [
                    {"name": "recommended", "label": "recommended", "algorithm": "turn"}]}}),
            ("unknown", {"variant": "recommended"})):
        folder = hist / name
        folder.mkdir(parents=True)
        (folder / "summary.json").write_text(json.dumps(summary))
        versions.append(dict(id=name, mode="test", total_km="1", mandatory_km="0.5",
                             deadhead_km="0.5", islands="1", note="", profile="edv"))
    fields = [k for k in round_history.FIELDS if k not in
              ("selection", "variant", "variant_label", "algorithm")]
    with (hist / "index.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(versions)
    before = {p: p.read_bytes() for p in hist.rglob("*") if p.is_file()}
    enriched = round_history.with_run_details(hist, round_history.read_index(hist))
    assert enriched[0]["run_description"].startswith("Solve:")
    assert enriched[1]["run_description"].startswith("Compare:")
    assert enriched[1]["algorithm"] == "turn"
    assert enriched[2]["run_description"].startswith("Unknown:")
    assert enriched[2]["selection"] == ""
    listed = history(hist, "list")
    assert all(label in listed.stdout for label in ("Solve:", "Compare:", "Unknown:"))
    assert before == {p: p.read_bytes() for p in hist.rglob("*") if p.is_file()}


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
    with open(out / "route.csv", newline="", encoding="utf-8-sig") as f:
        cf = [r for r in csv.DictReader(f) if r["type"] == "service" and r["counterflow"] == "yes"]
    assert summary["service_counterflow_segments"] == len(cf)
    assert summary["service_counterflow_km"] == round(sum(float(r["length_m"]) for r in cf) / 1000, 3)
    assert float(rows[0]["service_counterflow_km"]) == summary["service_counterflow_km"]
    assert float(rows[0]["total_km"]) >= float(rows[0]["mandatory_km"])
    with open(vdir / "service.csv", newline="",
              encoding="utf-8-sig") as f:
        stored = {r["edge_id"]: r["service"] for r in csv.DictReader(f)}
    assert stored["n01-court-0"] == "2"
    assert all(v in ("1", "2", "x") for v in stored.values())

    print("== identical re-solve is not filed twice ==")
    stdout = solve(data, out, hist)
    assert "matches existing version" in stdout, stdout
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

    print("== legacy side defaults do not create false history changes ==")
    test_legacy_diff()
    print("== new or changed counterflow is recorded even at the same distance ==")
    test_counterflow_history()
    test_fingerprints()
    test_solver_experiments()
    test_alternating_history()
    test_annotation_reversion()
    test_run_details_compatibility()

    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
