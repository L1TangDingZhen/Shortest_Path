#!/usr/bin/env python3
"""Offline tests for make_editor.py (no internet, synthetic data).

Reuses the synthetic grid from test_solve, appends rows with hostile
names / curved geometry / blank service, generates the editor and
asserts on the JSON payload embedded in the HTML:

  * header order and edge count preserved, edges.csv untouched
  * service values round-trip; blank service -> 0
  * curved WKT parsed to [lat, lon] chains; missing WKT falls back to
    the u/v node coordinates
  * names containing commas, quotes and "</script>" survive embedding
    (the payload may not terminate the JSON island early)
  * bad service values abort with a non-zero exit
  * --serve mode: GET rebuilds the page from disk, POST /save validates
    and writes back atomically (backup kept), bad payloads are rejected
    and leave the file untouched
  * stale/missing revisions cannot overwrite newer annotations; concurrent
    saves (threads or processes) have one winner, and conflicts keep backups
  * --serve endpoints + solve: POST /endpoints stores the picked
    start/end (rejecting nonsense), the payload carries them back, and
    POST /solve honours the stored preferences and serves /route_map.html;
    POST /compare uses isolated preset outputs without changing those settings

Run:  python test_editor.py
"""

import csv
import hashlib
import http.server
import io
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue
from threading import Barrier, Event, Thread
from unittest.mock import patch

import make_editor
from test_solve import write_case

BASE = Path(__file__).parent
TMP = BASE / "_test_tmp" / "editor"

TRICKY_NAME = 'Sneaky </script> St, "North"'
TRICKY_WKT = ("LINESTRING (144.951 -37.845, 144.9515 -37.8445, "
              "144.952 -37.844)")


def append_row(data_dir, **kw):
    with open(data_dir / "edges.csv", newline="",
              encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        header = list(reader.fieldnames)
    with open(data_dir / "edges.csv", "a", newline="",
              encoding="utf-8-sig") as f:
        csv.DictWriter(f, fieldnames=header).writerow(
            {h: kw.get(h, "") for h in header})


def run_editor(data_dir):
    return subprocess.run(
        [sys.executable, str(BASE / "make_editor.py"),
         "--data", str(data_dir)],
        capture_output=True, text=True)


def extract_payload(html_path):
    html = html_path.read_text(encoding="utf-8")
    m = re.search(r'<script id="edge-data" type="application/json">'
                  r"(.*?)</script>", html, re.DOTALL)
    assert m, "JSON island not found in editor.html"
    return json.loads(m.group(1))


def revision(data):
    return hashlib.sha256((data / "edges.csv").read_bytes()).hexdigest()


def post_save(base, text, rev=None):
    headers = {"Content-Type": "text/csv"}
    if rev is not None:
        headers["X-Edges-Revision"] = rev
    req = urllib.request.Request(f"{base}/save", data=text.encode("utf-8"),
                                 method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def main():
    if TMP.exists():
        shutil.rmtree(TMP)
    data = TMP / "data"
    spec = write_case(data)
    append_row(data, edge_id="tricky-0", name=TRICKY_NAME,
               highway="residential", length_m="123.4", service="1",
               u="n00", v="n11", geometry_wkt=TRICKY_WKT)
    append_row(data, edge_id="blank-0", name="Blank Street",
               highway="residential", length_m="50", service="",
               u="n01", v="n10", geometry_wkt="")
    append_row(data, edge_id="banned-0", name="Banned Path",
               highway="path", length_m="30", service="x",
               u="n10", v="n21", geometry_wkt="")
    before = (data / "edges.csv").read_bytes()

    print("== generate ==")
    res = run_editor(data)
    assert res.returncode == 0, res.stderr
    assert (data / "editor.html").exists()
    assert (data / "edges.csv").read_bytes() == before, \
        "generator must not modify edges.csv"

    print("== payload round-trip ==")
    payload = extract_payload(data / "editor.html")
    with open(data / "edges.csv", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        assert payload["header"] == list(reader.fieldnames)
        rows = list(reader)
    edges = payload["edges"]
    assert len(edges) == len(rows) == len(spec) + 3
    by_id = {e["row"]["edge_id"]: e for e in edges}

    for eid, svc in spec.items():
        assert by_id[eid]["service"] == svc, eid
    for e in edges:
        assert len(e["coords"]) >= 2, e["row"]["edge_id"]

    print("== hostile name ==")
    tricky = by_id["tricky-0"]
    assert tricky["row"]["name"] == TRICKY_NAME
    assert tricky["service"] == 1

    print("== WKT parsing and node fallback ==")
    assert tricky["coords"] == [[-37.845, 144.951],
                                [-37.8445, 144.9515],
                                [-37.844, 144.952]]
    nodes = {}
    with open(data / "nodes.csv", newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            nodes[r["node_id"]] = [float(r["lat"]), float(r["lon"])]
    grid = by_id["n00-n01-0"]  # write_case leaves geometry_wkt empty
    assert grid["coords"] == [nodes["n00"], nodes["n01"]]

    print("== blank service -> 0, x round-trips ==")
    assert by_id["blank-0"]["service"] == 0
    assert "blank service" in res.stdout
    assert by_id["banned-0"]["service"] == "x"

    print("== bad service aborts ==")
    data2 = TMP / "data_bad"
    write_case(data2)
    append_row(data2, edge_id="bad-0", name="Bad Street",
               highway="residential", length_m="10", service="7",
               u="n00", v="n01", geometry_wkt="")
    res2 = run_editor(data2)
    assert res2.returncode != 0, "service=7 must abort"

    print("== serve mode ==")
    test_serve()
    test_snapshot_revision()
    test_process_lock()
    test_busy_solver()

    print("\nALL TESTS PASSED")


def edit_csv_text(path, edge_id, new_service):
    """Return the file's CSV text with one service value changed,
    serialised the way the browser would post it."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        header = list(reader.fieldnames)
        rows = list(reader)
    for r in rows:
        if r["edge_id"] == edge_id:
            r["service"] = str(new_service)
            if "delivery" in header:
                r["delivery"] = {"0": "none", "1": "left", "2": "both", "x": "none"}.get(str(new_service), "")
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=header, lineterminator="\r\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def test_snapshot_revision():
    print("== page revision and displayed rows use the same snapshot ==")
    data = TMP / "snapshot"
    write_case(data)
    old_revision = revision(data)
    replacement = edit_csv_text(data / "edges.csv", "n01-court-0", 0)
    original_load = make_editor.load

    def replace_during_load(data_dir, raw_edges):
        (data_dir / "edges.csv").write_bytes(replacement.encode("utf-8-sig"))
        return original_load(data_dir, raw_edges)

    with patch.object(make_editor, "load", side_effect=replace_during_load):
        html, edges = make_editor.build_html(data)
    payload = json.loads(re.search(
        r'<script id="edge-data" type="application/json">(.*?)</script>',
        html, re.DOTALL).group(1))
    assert payload["edges_revision"] == old_revision != revision(data)
    assert next(e for e in edges if e["row"]["edge_id"] == "n01-court-0")["service"] == 2
    try:
        make_editor.save_csv(data, replacement, old_revision)
        raise AssertionError("disk changes after page load must conflict")
    except make_editor.SaveConflict:
        pass
    assert not (data / "edges.csv.bak").exists()


def test_process_lock():
    print("== separate editor processes share the save lock ==")
    data = TMP / "process_lock"
    write_case(data)
    before = (data / "edges.csv").read_bytes()
    rev = revision(data)
    code = """
import json, sys
from pathlib import Path
from make_editor import save_csv, SaveConflict
request = json.loads(Path(sys.argv[1]).read_text())
print('ready', flush=True)
try:
    result = save_csv(Path(request['data']), request['csv'], request['revision'])
    print(json.dumps(dict(status=200, **result)))
except SaveConflict:
    print(json.dumps(dict(status=409)))
"""
    processes = []
    try:
        with make_editor.lock_edges(data):
            for i, value in enumerate((0, 1)):
                request = data / f"request-{i}.json"
                request.write_text(json.dumps(dict(
                    data=str(data), revision=rev,
                    csv=edit_csv_text(data / "edges.csv", "n01-court-0", value))))
                proc = subprocess.Popen([sys.executable, "-B", "-c", code, str(request)],
                                        cwd=BASE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True)
                processes.append(proc)
                assert proc.stdout.readline().strip() == "ready"
            time.sleep(0.1)
            assert all(proc.poll() is None for proc in processes), "save bypassed lock"
            assert (data / "edges.csv").read_bytes() == before
        replies = []
        for proc in processes:
            output, errors = proc.communicate(timeout=10)
            assert proc.returncode == 0, errors
            replies.append(json.loads(output))
        assert sorted(r["status"] for r in replies) == [200, 409], replies
        assert (data / "edges.csv.bak").read_bytes() == before
        assert revision(data) == next(r["revision"] for r in replies if r["status"] == 200)
        assert not (data / "edges.csv.tmp").exists()
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)


def test_busy_solver():
    print("== one server refuses overlapping solve/compare requests ==")
    data = TMP / "busy_solver"
    write_case(data)
    servers = Queue()
    started, release = Event(), Event()
    original_server = http.server.ThreadingHTTPServer

    def server_factory(*args, **kwargs):
        server = original_server(*args, **kwargs)
        servers.put(server)
        return server

    def blocked_solver(*args, **kwargs):
        started.set()
        assert release.wait(10), "test did not release the solver"
        return False, "Synthetic solver failure"

    def request(base, action):
        req = urllib.request.Request(base + action, data=b"", method="POST")
        try:
            response = urllib.request.urlopen(req, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.code, json.loads(response.read())

    with patch.object(http.server, "ThreadingHTTPServer", side_effect=server_factory), \
            patch.object(make_editor, "run_solver", side_effect=blocked_solver) as solver:
        thread = Thread(target=make_editor.serve,
                        args=(data, 0, TMP / "busy_out", TMP / "busy_history"), daemon=True)
        thread.start()
        server = servers.get(timeout=10)
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                first = pool.submit(request, base, "/solve")
                try:
                    assert started.wait(10)
                    status, reply = request(base, "/compare")
                    assert status == 409 and not reply["ok"] and reply["routes"] == [], reply
                    assert solver.call_count == 1
                finally:
                    release.set()
                assert first.result(timeout=10)[0] == 400
            # Failure must release the gate for another request.
            assert request(base, "/compare")[0] == 400
            assert solver.call_count == 2
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=10)
            assert not thread.is_alive()


def test_serve():
    data = TMP / "data_serve"
    write_case(data)
    proc = subprocess.Popen(
        [sys.executable, str(BASE / "make_editor.py"),
         "--data", str(data), "--serve", "--port", "0",
         "--out", str(TMP / "serve_out"),
         # never let a test write into the real round directory
         "--history", str(TMP / "serve_hist")],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        port = None
        deadline = time.time() + 10
        while time.time() < deadline:
            line = proc.stdout.readline()
            m = re.search(r"http://127\.0\.0\.1:(\d+)/", line)
            if m:
                port = int(m.group(1))
                break
        assert port, "server did not announce its port"
        base = f"http://127.0.0.1:{port}"

        html = urllib.request.urlopen(f"{base}/", timeout=10).read()
        payload = json.loads(re.search(
            rb'<script id="edge-data" type="application/json">(.*?)'
            rb"</script>", html, re.DOTALL).group(1))
        assert payload["edges"], "served page has no edges"
        assert payload["edges_revision"] == revision(data)
        stale_text = edit_csv_text(data / "edges.csv", "n00-n01-0", "x")

        text = edit_csv_text(data / "edges.csv", "n01-court-0", 0)
        status, reply = post_save(base, text, payload["edges_revision"])
        assert status == 200, reply
        assert reply["revision"] == revision(data)
        with open(data / "edges.csv", newline="",
                  encoding="utf-8-sig") as f:
            saved = {r["edge_id"]: r["service"] for r in csv.DictReader(f)}
        assert saved["n01-court-0"] == "0", "save did not stick"
        assert (data / "edges.csv.bak").exists(), "no backup written"

        print("== serve: stale and unversioned saves rejected ==")
        before = (data / "edges.csv").read_bytes()
        backup = (data / "edges.csv.bak").read_bytes()
        for rev, expected in ((payload["edges_revision"], 409),
                              (None, 428), ("invalid", 409)):
            status, msg = post_save(base, stale_text, rev)
            assert status == expected, (status, msg)
            assert (data / "edges.csv").read_bytes() == before
            assert (data / "edges.csv.bak").read_bytes() == backup
            assert not (data / "edges.csv.tmp").exists()

        # Use the returned revision without reloading: consecutive saves work.
        text = edit_csv_text(data / "edges.csv", "n00-n01-0", "x")
        status, reply = post_save(base, text, reply["revision"])
        assert status == 200, reply
        assert reply["revision"] == revision(data)
        with open(data / "edges.csv", newline="",
                  encoding="utf-8-sig") as f:
            saved = {r["edge_id"]: r["service"] for r in csv.DictReader(f)}
        assert saved["n00-n01-0"] == "x", "x did not survive save"

        # GET now reflects the save (page rebuilt from disk)
        html2 = urllib.request.urlopen(f"{base}/", timeout=10).read()
        payload2 = json.loads(re.search(
            rb'<script id="edge-data" type="application/json">(.*?)'
            rb"</script>", html2, re.DOTALL).group(1))
        by_id = {e["row"]["edge_id"]: e for e in payload2["edges"]}
        assert by_id["n01-court-0"]["service"] == 0

        # invalid payload -> 400, file untouched
        before = (data / "edges.csv").read_bytes()
        bad = edit_csv_text(data / "edges.csv", "n01-court-0", 9)
        status, msg = post_save(base, bad, payload2["edges_revision"])
        assert status == 400, msg
        assert (data / "edges.csv").read_bytes() == before, \
            "rejected save must not modify the file"

        print("== serve: concurrent saves have one winner ==")
        texts = [edit_csv_text(data / "edges.csv", "n01-court-0", value)
                 for value in (1, 2)]
        gate = Barrier(2)

        def concurrent_save(text):
            gate.wait(timeout=10)
            return post_save(base, text, payload2["edges_revision"])

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(concurrent_save, texts))
        assert sorted(status for status, _ in results) == [200, 409], results
        winner = next(i for i, result in enumerate(results) if result[0] == 200)
        assert revision(data) == results[winner][1]["revision"]
        assert (data / "edges.csv.bak").read_bytes() == before
        with open(data / "edges.csv", encoding="utf-8-sig", newline="") as f:
            row = next(r for r in csv.DictReader(f) if r["edge_id"] == "n01-court-0")
        assert row["service"] == str((1, 2)[winner])

        print("== serve: endpoints + solve ==")
        # no route yet
        try:
            urllib.request.urlopen(f"{base}/route_map.html", timeout=10)
            raise AssertionError("route map must 404 before solving")
        except urllib.error.HTTPError as e:
            assert e.code == 404, e.code

        eps = {"start": [-37.845, 144.950], "end": [-37.8471, 144.9529],
               "return_to_start": True, "profile": "limits",
               "wrong_way_penalty": 7}
        req = urllib.request.Request(
            f"{base}/endpoints", data=json.dumps(eps).encode("utf-8"),
            method="POST", headers={"Content-Type": "application/json"})
        saved = json.loads(urllib.request.urlopen(req, timeout=10).read())
        assert saved["start"] == eps["start"] and saved["end"] == eps["end"]
        assert saved["return_to_start"] is True
        assert saved["profile"] == "limits"
        assert saved["wrong_way_penalty"] == 7
        assert json.loads((data / "endpoints.json").read_text(
            encoding="utf-8"))["start"] == eps["start"]

        # Zero, sub-unit and non-finite values must fail before touching disk.
        settings_before = (data / "endpoints.json").read_bytes()
        for bad in ({"start": ["north", 3]},
                    {"wrong_way_penalty": 0.5},
                    *({"wrong_way_penalty": value} for value in
                      (0, 0.0, -0.0, "0", False,
                       float("nan"), float("inf"), float("-inf"),
                       "NaN", "Infinity", "-Infinity", "1e9999")),
                    {"profile": "; rm -rf /"}):
            req = urllib.request.Request(
                f"{base}/endpoints", data=json.dumps(bad).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=10)
                raise AssertionError(f"{bad} must be rejected")
            except urllib.error.HTTPError as e:
                assert e.code == 400, e.code
                if "wrong_way_penalty" in bad:
                    assert "must be finite and >= 1" in e.read().decode()
            assert (data / "endpoints.json").read_bytes() == settings_before

        # Missing/null/empty still default to 1; explicit valid factors survive.
        for settings, expected in (({}, 1), ({"wrong_way_penalty": None}, 1),
                                   ({"wrong_way_penalty": ""}, 1),
                                   ({"wrong_way_penalty": 1}, 1),
                                   ({"wrong_way_penalty": 1.25}, 1.25), (eps, 7)):
            req = urllib.request.Request(
                f"{base}/endpoints", data=json.dumps(settings).encode("utf-8"),
                method="POST", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as response:
                assert json.loads(response.read())["wrong_way_penalty"] == expected

        # the served page carries the stored endpoints back
        html3 = urllib.request.urlopen(f"{base}/", timeout=10).read()
        payload3 = json.loads(re.search(
            rb'<script id="edge-data" type="application/json">(.*?)'
            rb"</script>", html3, re.DOTALL).group(1))
        assert payload3["endpoints"]["end"] == eps["end"]

        # solve on the stored endpoints, then serve the map
        req = urllib.request.Request(f"{base}/solve", data=b"",
                                     method="POST")
        reply = json.loads(
            urllib.request.urlopen(req, timeout=300).read().decode())
        out = reply["output"]
        assert reply["ok"] and "RESULT" in out, out
        assert "return to start" in out, out
        assert "speed profile: limits" in out and "wrong-way penalty x7" in out, out
        assert "--variants" not in out
        assert reply["routes"] == [], reply
        result_dir = TMP / "serve_out"
        assert not (result_dir / "alternatives.json").exists()
        current_csv = (result_dir / "route.csv").read_bytes()
        current_map = (result_dir / "route_map.html").read_bytes()
        settings = (data / "endpoints.json").read_bytes()
        summaries = list((TMP / "serve_hist").glob("*/summary.json"))
        assert len(summaries) == 1
        summary = json.loads(summaries[0].read_text())
        assert summary["profile"] == "limits" and summary["wrong_way_penalty"] == 7
        assert summary["wrong_way_source"] == "endpoints.json"

        print("== compare: explicit presets leave current route/settings intact ==")
        req = urllib.request.Request(f"{base}/compare", data=b"preset comparison",
                                     method="POST")
        reply = json.loads(urllib.request.urlopen(req, timeout=300).read())
        assert reply["ok"] and "--variants" in reply["output"], reply
        assert len(reply["routes"]) >= 2, reply["routes"]
        assert {"variant", "label", "time_min", "total_km"} <= \
            set(reply["routes"][0]), reply["routes"][0]
        assert {r["wrong_way_penalty"] for r in reply["routes"]} == {1, 3, 12}
        assert (data / "endpoints.json").read_bytes() == settings
        assert (result_dir / "route.csv").read_bytes() == current_csv
        assert (result_dir / "route_map.html").read_bytes() == current_map
        with urllib.request.urlopen(f"{base}/history", timeout=10) as response:
            history_entries = json.loads(response.read())
        assert {r["selection"] for r in history_entries} == {"single", "minimum_comparison_time"}
        assert history_entries[0]["run_description"].startswith("Solve:")
        assert history_entries[-1]["run_description"].startswith("Compare:")
        assert all(r["variant"] and r["algorithm"] for r in history_entries)
        page = urllib.request.urlopen(
            f"{base}/comparison/alternatives.html", timeout=10).read()
        assert b"alt-data" in page, "comparison map not served"
        variant = reply["routes"][0]["variant"]
        page = urllib.request.urlopen(
            f"{base}/comparison/{variant}/route_map.html", timeout=10).read()
        assert b"route-data" in page

        # An old comparison must never be returned for a later normal solve.
        (result_dir / "alternatives.json").write_text(json.dumps({"routes": [{"stale": True}]}))
        eps.update(profile="edv", wrong_way_penalty=11)
        req = urllib.request.Request(f"{base}/endpoints", data=json.dumps(eps).encode(),
                                     method="POST", headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).close()
        req = urllib.request.Request(f"{base}/solve", data=b"", method="POST")
        reply = json.loads(urllib.request.urlopen(req, timeout=300).read())
        assert reply["ok"] and reply["routes"] == [], reply
        assert "speed profile: edv" in reply["output"]
        assert "wrong-way penalty x11" in reply["output"]
        page = urllib.request.urlopen(f"{base}/route_map.html",
                                      timeout=10).read()
        assert b"route-data" in page, "route map not served"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


if __name__ == "__main__":
    main()
