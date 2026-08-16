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
  * --serve endpoints + solve: POST /endpoints stores the picked
    start/end (rejecting nonsense), the payload carries them back, and
    POST /solve runs the solver on them and serves /route_map.html

Run:  python test_editor.py
"""

import csv
import io
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

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
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=header, lineterminator="\r\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


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

        text = edit_csv_text(data / "edges.csv", "n01-court-0", 0)
        req = urllib.request.Request(
            f"{base}/save", data=text.encode("utf-8"), method="POST",
            headers={"Content-Type": "text/csv"})
        resp = urllib.request.urlopen(req, timeout=10)
        assert resp.status == 200, resp.status
        with open(data / "edges.csv", newline="",
                  encoding="utf-8-sig") as f:
            saved = {r["edge_id"]: r["service"] for r in csv.DictReader(f)}
        assert saved["n01-court-0"] == "0", "save did not stick"
        assert (data / "edges.csv.bak").exists(), "no backup written"

        # x survives a save round-trip
        text = edit_csv_text(data / "edges.csv", "n00-n01-0", "x")
        req = urllib.request.Request(
            f"{base}/save", data=text.encode("utf-8"), method="POST",
            headers={"Content-Type": "text/csv"})
        assert urllib.request.urlopen(req, timeout=10).status == 200
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
        req = urllib.request.Request(
            f"{base}/save", data=bad.encode("utf-8"), method="POST",
            headers={"Content-Type": "text/csv"})
        try:
            urllib.request.urlopen(req, timeout=10)
            raise AssertionError("bad service must be rejected")
        except urllib.error.HTTPError as e:
            assert e.code == 400, e.code
        assert (data / "edges.csv").read_bytes() == before, \
            "rejected save must not modify the file"

        print("== serve: endpoints + solve ==")
        # no route yet
        try:
            urllib.request.urlopen(f"{base}/route_map.html", timeout=10)
            raise AssertionError("route map must 404 before solving")
        except urllib.error.HTTPError as e:
            assert e.code == 404, e.code

        eps = {"start": [-37.845, 144.950], "end": [-37.8471, 144.9529],
               "return_to_start": True, "profile": "edv",
               "wrong_way_penalty": 2}
        req = urllib.request.Request(
            f"{base}/endpoints", data=json.dumps(eps).encode("utf-8"),
            method="POST", headers={"Content-Type": "application/json"})
        saved = json.loads(urllib.request.urlopen(req, timeout=10).read())
        assert saved["start"] == eps["start"] and saved["end"] == eps["end"]
        assert saved["return_to_start"] is True
        assert saved["profile"] == "edv"
        assert saved["wrong_way_penalty"] == 2
        assert json.loads((data / "endpoints.json").read_text(
            encoding="utf-8"))["start"] == eps["start"]

        # nonsense values are rejected
        for bad in ({"start": ["north", 3]},
                    {"wrong_way_penalty": 0.5},
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

        # the served page carries the stored endpoints back
        html3 = urllib.request.urlopen(f"{base}/", timeout=10).read()
        payload3 = json.loads(re.search(
            rb'<script id="edge-data" type="application/json">(.*?)'
            rb"</script>", html3, re.DOTALL).group(1))
        assert payload3["endpoints"]["end"] == eps["end"]

        # solve on the stored endpoints, then serve the map
        req = urllib.request.Request(f"{base}/solve", data=b"",
                                     method="POST")
        out = urllib.request.urlopen(req, timeout=120).read().decode()
        assert "RESULT" in out and "return to start" in out, out
        assert "speed profile: edv" in out, \
            "the solver must pick up the profile stored by the editor"
        assert (TMP / "serve_out" / "route.csv").exists()
        page = urllib.request.urlopen(f"{base}/route_map.html",
                                      timeout=10).read()
        assert b"route-data" in page, "route map not served"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


if __name__ == "__main__":
    main()
