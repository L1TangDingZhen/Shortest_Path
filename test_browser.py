#!/usr/bin/env python3
"""Offline browser regression: editor -> save -> solve -> compare -> viewer.

Setup (downloads only public test dependencies): see README.md, Tests.
Run after the other suites: python test_browser.py
All data, server logs and screenshots stay in _test_tmp/browser/.
Only localhost requests are sent; cached Leaflet and blank tiles are supplied
by Playwright. This tests route rendering and controls, not the external basemap.
"""

import base64
import csv
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

from test_solve import write_simple
from tools.browser_assets import read_assets

BASE = Path(__file__).resolve().parent
TMP = BASE / "_test_tmp" / "browser"
VIEWPORTS = [(1440, 1000), (390, 844), (320, 740)]
BLANK_TILE = base64.b64decode("R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=")


def write_demo(data):
    write_simple(data, {"A": (-37.845, 144.950), "B": (-37.845, 144.952),
                        "C": (-37.843, 144.952)},
                 [("east", "Demo East", "A", "B", 1),
                  ("north", "Demo North", "B", "C", 1),
                  ("diagonal", "Demo Diagonal", "C", "A", 2)])
    with open(data / "edges.csv", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fields = [k for k in reader.fieldnames if k != "delivery"]
        rows = [{k: row[k] for k in fields} for row in reader]
    with open(data / "edges.csv", "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


@contextmanager
def serve(folder):
    data, out, hist = folder / "data", folder / "out", folder / "history"
    write_demo(data)
    log_path = folder / "server.log"
    with log_path.open("w") as log:
        proc = subprocess.Popen(
            [sys.executable, "-B", str(BASE / "make_editor.py"), "--serve",
             "--port", "0", "--data", str(data), "--out", str(out),
             "--history", str(hist)], stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                output = log_path.read_text()
                match = re.search(r"http://127\.0\.0\.1:\d+/", output)
                if match:
                    yield match[0].rstrip("/"), data, out
                    return
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
            raise AssertionError("Editor failed to start:\n" + log_path.read_text())
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def offline_requests(context, base, assets):
    unexpected = []

    def handle(route):
        url = route.request.url
        if url.startswith(base + "/"):
            route.continue_()
        elif url in assets:
            route.fulfill(body=assets[url], content_type=(
                "text/javascript" if url.endswith(".js") else "text/css"))
        elif urlparse(url).hostname in {f"{s}.basemaps.cartocdn.com" for s in "abcd"}:
            route.fulfill(body=BLANK_TILE, content_type="image/gif")
        else:
            unexpected.append(url)
            route.abort()

    context.route("**/*", handle)
    return unexpected


def check_surface(page, screenshot):
    page.locator("#panel").evaluate("el => el.scrollTop = 0")
    page.wait_for_function("""() => [...document.querySelectorAll('#map canvas')].some(c => {
      if (!c.width || !c.height) return false;
      const pixels = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
      let painted = 0;
      for (let i = 3; i < pixels.length; i += 4) {
        if (pixels[i] && ++painted > 100) return true;
      }
      return false;
    })""")
    problems = page.evaluate("""() => {
      const panel = document.getElementById('panel'), r = panel.getBoundingClientRect();
      const problems = [];
      if (document.documentElement.scrollWidth > innerWidth + 1) problems.push('page overflow');
      if (r.left < 0 || r.right > innerWidth + 1 || r.bottom > innerHeight + 1)
        problems.push('panel outside viewport');
      if (panel.scrollWidth > panel.clientWidth + 1) problems.push('panel overflow');
      for (const el of panel.querySelectorAll('button, input, select')) {
        const b = el.getBoundingClientRect();
        if (b.width && (b.left < r.left || b.right > r.right))
          problems.push('control overflow: ' + el.id);
      }
      return problems;
    }""")
    page.screenshot(path=str(screenshot))
    assert not problems, problems


def editor_state(page):
    return page.evaluate("Object.fromEntries(edges.map(e => [e.row.edge_id, e.delivery]))")


def exercise_save_conflicts(context, page, base, data, folder):
    from playwright.sync_api import expect

    stale = context.new_page()
    stale.goto(base)
    expect(stale.locator("#npending")).to_have_text("0")
    original_revision = stale.evaluate("edgesRevision")
    stale.locator("#search").fill("Demo North")
    stale.locator("#bulkright").click()
    local_state = editor_state(stale)
    undo_count = stale.evaluate("undoStack.length")

    page.locator("#search").fill("Demo East")
    page.locator("#bulkleft").click()
    with page.expect_response("**/save") as saved:
        page.locator("#export").click()
    assert saved.value.status == 200
    expect(page.locator("#dirty")).to_have_text("")
    disk = (data / "edges.csv").read_bytes()
    backup = (data / "edges.csv.bak").read_bytes()
    messages = []

    def dismiss(dialog):
        messages.append(dialog.message)
        dialog.dismiss()

    stale.on("dialog", dismiss)
    # A rejected save must not adopt the new disk revision on retry.
    for _ in range(2):
        with stale.expect_response("**/save") as rejected:
            stale.locator("#export").click()
        assert rejected.value.status == 409
        expect(stale.locator("#savemsg")).to_have_text("Save conflict. Local edits kept.")
        expect(stale.locator("#export")).to_be_enabled()
        expect(stale.locator("#dirty")).to_have_text("1 unsaved")
        assert editor_state(stale) == local_state
        assert stale.evaluate("edgesRevision") == original_revision
        assert stale.evaluate("undoStack.length") == undo_count
        assert (data / "edges.csv").read_bytes() == disk
        assert (data / "edges.csv.bak").read_bytes() == backup
    assert len(messages) == 2 and all("Download local CSV" in msg for msg in messages)
    expect(stale.locator("#savecopy")).to_be_visible()
    with stale.expect_download() as downloaded:
        stale.locator("#savecopy").click()
    export = folder / "conflicting-local.csv"
    downloaded.value.save_as(export)
    assert export.read_bytes().decode("utf-8") == stale.evaluate("buildCsv()")
    expect(stale.locator("#dirty")).to_have_text("1 unsaved")
    check_surface(stale, folder / "save-conflict.png")
    stale.locator("#undo").click()
    assert editor_state(stale)["north"] == "left"
    stale.close()

    # Hold the response after the server saves, then make another local edit.
    # Only the submitted state may become the new saved baseline.
    page.locator("#bulkright").click()
    requests = []

    def delayed_response(route):
        requests.append(route.request)
        response = route.fetch()
        assert response.status == 200
        assert page.locator("#export").is_disabled()
        page.evaluate("bulk('left'); exportCsv()")
        route.fulfill(response=response)

    page.route("**/save", delayed_response)
    with page.expect_response("**/save") as saved:
        page.locator("#export").click()
    assert saved.value.status == 200
    expect(page.locator("#export")).to_be_enabled()
    expect(page.locator("#dirty")).to_have_text("1 unsaved")
    assert len(requests) == 1, "double save was not suppressed"
    assert editor_state(page)["east"] == "left"
    with open(data / "edges.csv", newline="", encoding="utf-8-sig") as f:
        assert next(r for r in csv.DictReader(f) if r["edge_id"] == "east")["delivery"] == "right"
    page.unroute("**/save", delayed_response)
    with page.expect_response("**/save") as saved:
        page.locator("#export").click()
    assert saved.value.status == 200
    expect(page.locator("#dirty")).to_have_text("")
    page.reload()
    assert editor_state(page)["east"] == "left"


def exercise(browser, assets, width, height):
    from playwright.sync_api import expect

    folder = TMP / str(width)
    with serve(folder) as (base, data, out):
        context = browser.new_context(viewport={"width": width, "height": height},
                                      service_workers="block")
        unexpected = offline_requests(context, base, assets)
        errors = []
        context.on("page", lambda page: page.on("pageerror", lambda error: errors.append(str(error))))
        try:
            page = context.new_page()
            page.goto(base)
            expect(page.locator("#npending")).to_have_text("2")
            page.locator("#solve").click()
            expect(page.locator("#solvemsg")).to_have_text("failed")
            expect(page.locator("#solveout")).to_contain_text("unspecified delivery")
            assert not (out / "route.csv").exists()
            assert not (folder / "history" / "index.csv").exists()

            page.locator("#pending").click()
            expect(page.locator("#nmatch")).to_have_text("2 pending")
            page.locator("#bulkright").click()
            expect(page.locator("#npending")).to_have_text("0")
            assert editor_state(page) == dict(east="right", north="right", diagonal="both")
            page.locator("#undo").click()
            expect(page.locator("#npending")).to_have_text("2")

            page.locator("#paint").select_option("right")
            page.wait_for_function("!map._animatingZoom && !(map._panAnim && map._panAnim._inProgress)")
            point = page.evaluate("""() => {
              const c = edges.find(e => e.row.edge_id === 'east').coords;
              const mid = [(c[0][0]+c[1][0])/2, (c[0][1]+c[1][1])/2];
              map.setView(mid, 18, {animate: false});
              map.panBy([0, -innerHeight * 0.28], {animate: false});
              const p = map.latLngToContainerPoint(mid), r = map.getContainer().getBoundingClientRect();
              return {x: r.left + p.x, y: r.top + p.y};
            }""")
            page.mouse.move(point["x"], point["y"])
            expect(page.locator(".ref-label")).to_have_text(["Left", "Right"])
            page.mouse.click(point["x"], point["y"])
            expect(page.locator("#npending")).to_have_text("1")
            assert editor_state(page)["east"] == "right"
            check_surface(page, folder / "reference-arrow.png")
            page.locator("#pending").click()
            page.locator("#bulkleft").click()
            expect(page.locator("#npending")).to_have_text("0")
            expected = dict(east="right", north="left", diagonal="both")
            assert editor_state(page) == expected

            page.locator("#search").fill("Demo Diagonal")
            page.locator("#bulkleft").click()
            assert editor_state(page)["diagonal"] == "left"
            page.locator("#undo").click()
            assert editor_state(page) == expected
            with page.expect_response("**/save") as response:
                page.locator("#export").click()
            assert response.value.status == 200
            expect(page.locator("#dirty")).to_have_text("")
            page.reload()
            expect(page.locator("#npending")).to_have_text("0")
            assert editor_state(page) == expected
            with open(data / "edges.csv", newline="", encoding="utf-8-sig") as f:
                assert {r["edge_id"]: r["delivery"] for r in csv.DictReader(f)} == expected
            exercise_save_conflicts(context, page, base, data, folder)

            page.locator("#solve").click()
            expect(page.locator("#solvemsg a")).to_be_visible(timeout=30000)
            expect(page.locator("#routes")).to_contain_text("service counterflow")
            expect(page.locator("#history")).to_contain_text("service counterflow")
            check_surface(page, folder / "editor.png")
            with context.expect_page() as opened:
                page.locator("#solvemsg a").click()
            comparison = opened.value
            comparison.wait_for_load_state()
            expect(comparison.locator(".opt")).to_have_count(5)
            comparison.locator(".opt").first.click()
            expect(comparison.locator(".opt.on")).to_have_attribute("data-i", "0")
            comparison.locator(".opt").last.click()
            expect(comparison.locator(".opt.on")).to_have_attribute("data-i", "4")
            expect(comparison.locator("#opts")).to_contain_text("service counterflow")
            check_surface(comparison, folder / "comparison.png")
            if width <= 600:
                assert comparison.locator("#panel").bounding_box()["height"] <= height * 0.55 + 1

            with context.expect_page() as opened:
                page.locator("#routes a").first.click()
            viewer = opened.value
            viewer.wait_for_load_state()
            expect(viewer.locator("#totals")).to_contain_text("service counterflow")
            expect(viewer.locator("#slider")).to_have_value("1")
            viewer.locator("#next").click()
            expect(viewer.locator("#slider")).to_have_value("2")
            viewer.locator("#prev").click()
            expect(viewer.locator("#slider")).to_have_value("1")
            viewer.locator("#play").click()
            expect(viewer.locator("#slider")).not_to_have_value("1")
            viewer.locator("#play").click()
            check_surface(viewer, folder / "viewer.png")
            assert not errors, errors
            assert not unexpected, f"Unexpected network requests (blocked): {unexpected}"
            print(f"PASS browser {width}x{height}: pending, paint, bulk, undo, save conflicts, in-flight edits, solve, compare, viewer")
        except BaseException:
            for i, page in enumerate(context.pages):
                page.screenshot(path=str(folder / f"failure-{i}.png"))
            raise
        finally:
            context.close()


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("Install browser test dependencies: python -m pip install -r requirements-test.txt")
    assets = read_assets()
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(BASE / "cache" / "playwright"))
    if TMP.exists():
        shutil.rmtree(TMP)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=[
            "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1"])
        try:
            for width, height in VIEWPORTS:
                exercise(browser, assets, width, height)
        finally:
            browser.close()
    print(f"ALL BROWSER TESTS PASSED; screenshots: {TMP}")


if __name__ == "__main__":
    main()
