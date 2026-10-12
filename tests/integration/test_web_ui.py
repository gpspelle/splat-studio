"""Drives the real web UI in a headless browser (needs `pip install playwright` + a Chromium).

Skipped automatically when Playwright or a browser isn't available. Set SPLAT_SCREENSHOTS
to a folder to save screenshots (used for the README).
"""
import os
import time
import unittest
from pathlib import Path

from splat_studio.server.jobs import Job
from tests.helpers import make_video, needs_ffmpeg, write_ply
from plyfile import write_foggy_scene  # tests/fakes
from tests.integration.test_server_api import ServerTestCase

import struct
import zlib

try:
    from playwright.sync_api import sync_playwright
except ImportError:              # pragma: no cover
    sync_playwright = None

SHOTS = os.environ.get("SPLAT_SCREENSHOTS")


def decode_png(data: bytes):
    """Minimal PNG decoder (8-bit RGB/RGBA, non-interlaced - what Chromium screenshots are).
    Returns (width, height, RGBA bytes)."""
    pos, chunks, w = 8, [], None
    while pos < len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        if kind == b"IHDR":
            w, h, depth, ctype = struct.unpack(">IIBB", body[:10])
            assert depth == 8 and ctype in (2, 6), (depth, ctype)
            bpp = 4 if ctype == 6 else 3
        elif kind == b"IDAT":
            chunks.append(body)
        pos += 12 + n
    raw, stride = zlib.decompress(b"".join(chunks)), w * bpp
    out, prev = bytearray(), bytearray(stride)
    for y in range(h):
        f, line = raw[y * (stride + 1)], bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for x in range(stride):
            a = line[x - bpp] if x >= bpp else 0
            b, c = prev[x], prev[x - bpp] if x >= bpp else 0
            if f == 1:
                line[x] = (line[x] + a) & 255
            elif f == 2:
                line[x] = (line[x] + b) & 255
            elif f == 3:
                line[x] = (line[x] + (a + b) // 2) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        prev = line
        if bpp == 4:
            out += line
        else:
            for x in range(0, stride, 3):
                out += line[x:x + 3] + b"\xff"
    return w, h, bytes(out)
BROWSER_ARGS = ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]


class BrowserTestCase(ServerTestCase):
    """A running server plus a headless Chromium page (self.page)."""

    def setUp(self):
        super().setUp()
        self.pw = sync_playwright().start()
        try:
            self.browser = self.pw.chromium.launch(args=BROWSER_ARGS)
        except Exception as e:              # no browser installed
            self.pw.stop()
            self.skipTest(f"no Chromium available: {e}")
        self.page = self.browser.new_page(viewport={"width": 1320, "height": 900})
        self.errors = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.on("console", lambda m: m.type == "error" and self.errors.append(m.text))
        self.page.on("dialog", lambda d: d.accept())
        self.url = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.browser.close()
        self.pw.stop()
        super().tearDown()

    def open_new(self):
        self.page.goto(self.url + "/new")
        self.page.wait_for_function("document.getElementById('prevVideo').options.length > 0")

    def shot(self, name, full_page=False, page=None):
        if SHOTS:
            Path(SHOTS).mkdir(parents=True, exist_ok=True)
            (page or self.page).screenshot(path=str(Path(SHOTS) / f"{name}.png"), full_page=full_page)

    def coverage(self, background=(13, 15, 18)):
        """Share of the screenshot (excluding the toolbars) that isn't background colour."""
        vp = self.page.viewport_size
        clip = {"x": 0, "y": 70, "width": vp["width"], "height": vp["height"] - 150}
        w, h, px = decode_png(self.page.screenshot(clip=clip, timeout=180000))
        hit = total = 0
        for i in range(0, len(px), 4 * 7):                     # every 7th pixel is plenty
            total += 1
            if abs(px[i] - background[0]) + abs(px[i + 1] - background[1]) + abs(px[i + 2] - background[2]) > 30:
                hit += 1
        return hit / total


@unittest.skipIf(sync_playwright is None, "playwright not installed")
@needs_ffmpeg
class WebUiTest(BrowserTestCase):
    def test_choose_what_to_keep(self):
        os.environ["SPLAT_STUDIO_MASK_BACKEND"] = "disc"         # stand-in for SAM 2
        self.addCleanup(os.environ.pop, "SPLAT_STUDIO_MASK_BACKEND", None)
        self.open_new()
        self.assertFalse(self.page.get_by_test_id("keep-link").is_visible())     # "Everything" by default
        self.page.click('.seg[data-name="detail"] button[data-v="low"]')
        self.page.click('.seg[data-name="keep"] button[data-v="one"]')
        self.assertIn("Choose a video first", self.page.get_by_test_id("keep-status").text_content())
        self.page.set_input_files("#fileInput", str(make_video(self.tmp / "vase.mov", seconds=6, size="960x540")))
        status = self.page.get_by_test_id("keep-status")
        self.page.wait_for_function("document.querySelector('[data-testid=keep-status]').textContent.includes('No selection')")
        self.assertTrue(self.page.is_disabled("#startBtn"))                       # a selection is needed first
        self.assertIn("Choose what to keep first", self.page.text_content("#startHelp"))
        self.page.get_by_test_id("keep-link").click()
        self.page.wait_for_url("**/keep?video=*")
        self.assertEqual(self.page.get_by_test_id("keep-run").count(), 0)          # runs start only on New run
        self.assertEqual(self.page.get_attribute("[data-testid=keep-use]", "aria-disabled"), "true")
        self.assertTrue(self.page.get_by_test_id("keep-frames").is_hidden())       # frames start on their own
        canvas = self.page.get_by_test_id("keep-canvas")
        canvas.wait_for(state="visible", timeout=60000)
        self.page.wait_for_function("document.getElementById('frameImg').naturalWidth > 0")
        box = canvas.bounding_box()
        self.page.mouse.click(box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.5)
        self.page.get_by_test_id("tool-drop").click()
        self.page.mouse.click(box["x"] + box["width"] * 0.9, box["y"] + box["height"] * 0.15)
        self.page.wait_for_function("window.keepPage.maskShown()", timeout=30000)
        pts = self.page.evaluate("Object.values(window.keepPage.prompts())[0]")
        self.assertEqual([p[2] for p in pts], [1, 0])
        self.assertAlmostEqual(pts[0][0], 0.5, delta=0.02)
        self.page.get_by_test_id("keep-apply").click()
        self.page.wait_for_function("window.keepPage.masksDone()", timeout=60000)
        self.page.wait_for_timeout(500)
        self.shot("keep")
        self.page.reload()                                            # the clicks and masks are kept
        self.page.wait_for_function("window.keepPage.masksDone() && window.keepPage.maskShown()", timeout=30000)
        self.assertEqual(len(self.page.evaluate("Object.values(window.keepPage.prompts())[0]")), 2)
        # a slipped frame: click it again, then update only the frames around it (#11)
        self.assertTrue(self.page.get_by_test_id("keep-update").is_hidden())
        self.page.evaluate("document.getElementById('scrub').value = 30; document.getElementById('scrub').dispatchEvent(new Event('input'))")
        self.page.wait_for_function("window.keepPage.index() === 30 && window.keepPage.maskShown()", timeout=30000)
        self.page.get_by_test_id("tool-keep").click()
        self.page.mouse.click(box["x"] + box["width"] * 0.45, box["y"] + box["height"] * 0.5)
        upd = self.page.get_by_test_id("keep-update")
        upd.wait_for(state="visible")
        self.assertEqual(upd.text_content(), "Update 1 changed frame(s)")
        self.assertEqual(self.page.text_content("[data-testid=keep-apply]"), "Apply to all frames again")
        self.assertEqual(self.page.get_attribute("[data-testid=keep-use]", "aria-disabled"), "true")
        self.page.reload()                                            # the change survives a reload
        upd.wait_for(state="visible")
        upd.click()
        self.page.wait_for_function("window.keepPage.masksDone() && !window.keepPage.changed().length", timeout=60000)
        self.assertIn("around 1 changed frame", self.page.text_content("#applyStatus"))
        self.assertTrue(upd.is_hidden())
        self.page.get_by_test_id("keep-use").click()
        self.page.wait_for_url(self.url + "/new", timeout=10000)
        # back on New run: same video, same settings, selection ready
        self.page.wait_for_function("window.newRun.selectionReady()", timeout=10000)
        self.assertEqual(self.page.get_by_test_id("chosen-video").text_content(), "vase.mov")
        self.assertEqual(self.page.get_attribute('.seg[data-name="detail"] button.on', "data-v"), "low")
        self.assertEqual(self.page.get_attribute('.seg[data-name="keep"] button.on', "data-v"), "one")
        self.assertIn("Selection ready", status.text_content())
        self.assertEqual(self.page.get_by_test_id("keep-link").text_content(), "Edit selection")
        self.page.click('.seg[data-name="quality"] button[data-v="preview"]')
        self.page.click("#startBtn")
        self.page.wait_for_url(self.url + "/", timeout=10000)
        self.page.wait_for_selector("[data-testid=run-done] >> text=View splat", timeout=180000)
        _, jobs = self.json("GET", "/api/jobs")
        run = next(j for j in jobs if j["type"] == "video")
        self.assertTrue(run["options"]["masks"])
        self.assertEqual((run["options"]["quality"], run["options"]["detail"]), ("preview", "low"))
        self.assertTrue((self.data.root / run["run_dir"] / "dense" / "masks").is_dir())
        self.assertIn("your selection", self.page.evaluate("document.querySelector('[data-testid=run-done]').textContent"))
        self.open_new()                                               # the draft was used up
        self.assertIsNone(self.page.evaluate("window.newRun.video()"))
        self.assertEqual(self.page.get_attribute('.seg[data-name="keep"] button.on', "data-v"), "all")
        self.assertEqual(self.errors, [])

    def test_several_objects(self):
        os.environ["SPLAT_STUDIO_MASK_BACKEND"] = "disc"         # stand-in for SAM 2
        self.addCleanup(os.environ.pop, "SPLAT_STUDIO_MASK_BACKEND", None)
        self.open_new()
        self.page.click('.seg[data-name="keep"] button[data-v="several"]')
        self.page.set_input_files("#fileInput", str(make_video(self.tmp / "table.mov", seconds=6, size="960x540")))
        self.page.wait_for_function("document.querySelector('[data-testid=keep-status]').textContent.includes('No selection')")
        self.page.get_by_test_id("keep-link").click()
        self.page.wait_for_url("**/keep?*objects=several*")
        self.assertTrue(self.page.get_by_test_id("keep-frames").is_hidden())       # frames start on their own
        canvas = self.page.get_by_test_id("keep-canvas")
        canvas.wait_for(state="visible", timeout=60000)
        self.page.wait_for_function("document.getElementById('frameImg').naturalWidth > 0")
        self.assertEqual(self.page.get_by_test_id("keep-object").count(), 1)
        box = canvas.bounding_box()
        click = lambda x, y: self.page.mouse.click(box["x"] + box["width"] * x, box["y"] + box["height"] * y)
        self.page.locator("[data-testid=keep-object] input").first.fill("Mug")
        click(0.25, 0.5)
        self.page.get_by_test_id("keep-add-object").click()
        self.page.keyboard.type("Vase")
        click(0.75, 0.5)
        self.page.get_by_test_id("tool-drop").click()
        click(0.5, 0.95)
        self.page.wait_for_function("window.keepPage.masksShown() === 2", timeout=30000)
        pts = self.page.evaluate("Object.values(window.keepPage.prompts())[0]")
        self.assertEqual([p[2] for p in pts], [1, 2, 0])
        # suggested objects (#15): one click on a suggestion keeps it as a new object
        self.page.get_by_test_id("keep-suggest").click()
        self.page.wait_for_function("window.keepPage.suggestions().length === 3", timeout=30000)
        self.assertIn("3 suggestion(s)", self.page.get_by_test_id("suggest-status").text_content())
        self.shot("keep-suggest")
        self.page.get_by_test_id("tool-keep").click()
        sx, sy = self.page.evaluate("window.keepPage.suggestions()[2]")
        click(sx, sy)
        self.page.wait_for_function("window.keepPage.objects().length === 3")
        last = self.page.evaluate("Object.values(window.keepPage.prompts())[0].slice(-1)[0]")
        self.assertEqual(last, [sx, sy, 3])                                 # object 3, at the suggestion's point
        self.assertEqual(len(self.page.evaluate("window.keepPage.suggestions()")), 2)
        # Shift-click joins a suggestion to the selected object (an object suggested in parts)
        sx2, sy2 = self.page.evaluate("window.keepPage.suggestions()[1]")
        self.page.keyboard.down("Shift"); click(sx2, sy2); self.page.keyboard.up("Shift")
        self.assertEqual(self.page.evaluate("window.keepPage.objects().length"), 3)
        self.assertEqual(self.page.evaluate("Object.values(window.keepPage.prompts())[0].slice(-1)[0]"), [sx2, sy2, 3])
        asked = []
        self.page.on("request", lambda r: "/api/keep/suggest" in r.url and asked.append(r.url))
        self.page.get_by_test_id("keep-suggest").click()                     # second press hides them
        self.assertEqual(self.page.evaluate("window.keepPage.suggestions()"), [])
        self.assertEqual(self.page.text_content("[data-testid=keep-suggest]"), "Show suggestions")
        self.page.get_by_test_id("keep-suggest").click()                     # and shows them again, without asking again
        self.assertEqual(len(self.page.evaluate("window.keepPage.suggestions()")), 1)
        self.page.get_by_test_id("keep-suggest").click()
        self.assertEqual(asked, [])
        self.page.locator("[data-testid=keep-object] button[data-remove]").nth(2).click()   # back to two objects
        self.page.wait_for_function("window.keepPage.objects().length === 2")
        self.assertEqual(self.page.evaluate("window.keepPage.objects().map((o) => o.name)"), ["Mug", "Vase"])
        self.assertIn("1 click(s)", self.page.get_by_test_id("keep-object").nth(1).text_content())
        self.page.get_by_test_id("keep-apply").click()
        self.page.wait_for_function("window.keepPage.masksDone() && window.keepPage.masksShown() === 2", timeout=60000)
        self.page.wait_for_timeout(400)
        self.shot("keep-objects")
        self.page.reload()                                            # objects and names are kept
        self.page.wait_for_function("window.keepPage.masksDone() && window.keepPage.masksShown() === 2", timeout=30000)
        self.assertEqual(self.page.evaluate("window.keepPage.objects().map((o) => o.name)"), ["Mug", "Vase"])
        self.page.get_by_test_id("keep-use").click()
        self.page.wait_for_url(self.url + "/new", timeout=10000)
        self.page.wait_for_function("window.newRun.selectionReady()", timeout=10000)
        self.assertEqual(self.page.get_attribute('.seg[data-name="keep"] button.on', "data-v"), "several")
        self.assertIn("2 objects", self.page.get_by_test_id("keep-status").text_content())
        self.page.click('.seg[data-name="quality"] button[data-v="preview"]')
        self.page.click("#startBtn")
        self.page.wait_for_url(self.url + "/", timeout=10000)
        self.page.wait_for_selector("[data-testid=run-done] >> text=View splat", timeout=180000)
        _, jobs = self.json("GET", "/api/jobs")
        run = next(j for j in jobs if j["type"] == "video")
        self.assertEqual((run["options"]["masks"], run["options"]["split_objects"]), (True, True))
        self.assertIn("one splat per object", self.page.evaluate("document.querySelector('[data-testid=run-done]').textContent"))
        self.assertEqual(self.errors, [])

    def test_every_click_and_object_can_be_removed(self):
        os.environ["SPLAT_STUDIO_MASK_BACKEND"] = "disc"
        self.addCleanup(os.environ.pop, "SPLAT_STUDIO_MASK_BACKEND", None)
        up = self.upload(make_video(self.tmp / "desk.mov", seconds=4))[1]
        import json as _json
        video = _json.loads(up)["video"]
        self.page.goto(f"{self.url}/keep?video={video}&objects=several")
        canvas = self.page.get_by_test_id("keep-canvas")
        canvas.wait_for(state="visible", timeout=60000)
        self.page.wait_for_function("document.getElementById('frameImg').naturalWidth > 0")
        box = canvas.bounding_box()
        click = lambda x, y: self.page.mouse.click(box["x"] + box["width"] * x, box["y"] + box["height"] * y)
        go = lambda i: self.page.evaluate(f"document.getElementById('scrub').value = {i}; document.getElementById('scrub').dispatchEvent(new Event('input'))")
        clicks = lambda: self.page.evaluate("Object.values(window.keepPage.prompts()).flat().length")
        click(0.3, 0.5)                                    # object 1 on frame 0
        self.page.get_by_test_id("keep-add-object").click()
        go(10)
        click(0.7, 0.5)                                    # object 2 on frame 10
        self.assertEqual(clicks(), 2)
        # Undo works across frames: back on frame 0 it still takes back the latest click, on frame 10
        go(0)
        self.page.click("#undoBtn")
        self.assertEqual((clicks(), self.page.evaluate("window.keepPage.index()")), (1, 10))
        click(0.7, 0.5)
        # both objects can be removed, the last one too; then there's one empty object to start again
        go(0)
        for _ in range(2):
            self.page.locator("[data-testid=keep-object] button[data-remove]").first.click()
        self.assertEqual(clicks(), 0)
        self.assertEqual(self.page.evaluate("window.keepPage.objects()"), [{"id": 1, "name": ""}])
        self.assertEqual(self.page.locator("[data-testid=keep-object] button[data-remove]").count(), 0)
        # Start over clears every frame (after a confirmation)
        click(0.3, 0.5); go(5); self.page.get_by_test_id("tool-drop").click(); click(0.5, 0.9)
        self.page.get_by_test_id("keep-reset").click()
        self.assertEqual(clicks(), 0)
        self.page.reload()                                 # and it stays cleared
        self.page.wait_for_function("window.keepPage && document.getElementById('frameImg').naturalWidth > 0")
        self.assertEqual(clicks(), 0)
        self.assertEqual(self.errors, [])

    def test_finished_run_offers_each_object(self):
        from plyfile import write_three_objects
        splat = self.data.root / "workspace" / "table" / "run_1" / "splat"
        write_three_objects(splat / "standard_30000.ply", n=300)
        for name in ("red-ball", "blue-ball"):
            write_three_objects(splat / f"standard_30000_part-{name}.ply", n=100)
        job = self.add_finished_run("table", 1)
        job.splat = "workspace/table/run_1/splat/standard_30000.ply"
        job.parts = [{"name": "Red ball", "splat": "workspace/table/run_1/splat/standard_30000_part-red-ball.ply"},
                     {"name": "Blue ball", "splat": "workspace/table/run_1/splat/standard_30000_part-blue-ball.ply"}]
        ring = {"cameras": 30, "azimuths": [i * 12.0 for i in range(30)], "elevations": [0.0] * 30,
                "gap": {"degrees": 12.0, "from": 0.0, "to": 12.0, "first": "a.jpg", "last": "b.jpg"},
                "height": {"low": 0.0, "high": 20.0, "spread": 20.0}}
        job.coverage = {**ring, "objects": [{"id": 1, "name": "Red ball", **ring},
                                            {"id": 2, "name": "Blue ball", **ring, "cameras": 18}]}
        job.save(force=True)
        self.page.goto(self.url)
        parts = self.page.get_by_test_id("view-part")
        parts.first.wait_for()
        self.assertEqual(parts.all_text_contents(), ["Red ball", "Blue ball"])
        row = self.page.locator("[data-testid=run-done]", has=self.page.get_by_test_id("view-part"))
        row.get_by_test_id("run-details").click()                                # one coverage ring per object (#26)
        rings = row.get_by_test_id("coverage-object")
        rings.first.wait_for()
        self.assertEqual(rings.count(), 2)
        self.assertIn("Blue ball18 cameras", rings.nth(1).text_content().replace("\n", "").replace(" ", "", 2))
        titles = self.page.evaluate("[...document.querySelectorAll('.tile .body b')].map((b) => b.textContent)")
        self.assertIn("table · standard · red ball", titles)
        parts.first.click()
        self.page.wait_for_function("(document.getElementById('overlay') || {style: {}}).style.display === 'none'", timeout=60000)
        self.assertEqual(self.page.inner_text("#title"), "table · standard · red ball")
        self.assertEqual(self.errors, [])

    def test_coverage_ring_and_warning(self):
        os.environ["FAKE_COLMAP_POSES"] = "gap"                       # one side never filmed
        self.addCleanup(os.environ.pop, "FAKE_COLMAP_POSES", None)
        self.open_new()
        self.page.set_input_files("#fileInput", str(make_video(self.tmp / "side.mov", seconds=6, size="960x540")))
        self.page.get_by_test_id("video-advice").wait_for(state="visible")
        self.page.click('.seg[data-name="quality"] button[data-v="preview"]')
        self.page.click("#startBtn")
        self.page.wait_for_url(self.url + "/", timeout=10000)
        row = self.page.locator("[data-testid=run-done]").first
        row.wait_for(timeout=180000)
        self.assertIn("barely filmed", row.locator("[data-testid=run-warnings]").text_content())
        row.locator("[data-testid=run-details]").click()
        ring = row.get_by_test_id("coverage")
        ring.wait_for()
        self.assertEqual(ring.locator("path").count(), 1)                 # the gap, drawn as an arc
        self.assertGreater(ring.locator("circle").count(), 20)            # one dot per camera
        self.assertRegex(ring.text_content(), r"largest gap 1[2-4]\d°")
        self.shot("coverage", page=None)
        # the same coverage in the viewer, with a marker for where you're looking from (#26)
        row.locator("text=View splat").click()
        self.page.wait_for_function("(document.getElementById('overlay') || {style: {}}).style.display === 'none'", timeout=60000)
        cmap = self.page.get_by_test_id("cov-map")
        cmap.wait_for(state="visible", timeout=10000)
        views = set()
        for _ in range(24):                                              # walk all the way round
            self.page.evaluate("window.splatViewer.orbit(15)")
            self.page.wait_for_timeout(60)
            views.add(self.page.evaluate("window.splatViewer.coverageView().view"))
        self.assertEqual(views, {"ok", "gap"})                          # some views face the side nobody filmed
        self.page.wait_for_function("window.splatViewer.coverageView().view === 'ok' || true")
        self.page.get_by_test_id("cov-toggle").click()                  # hide it, and it stays hidden
        self.assertTrue(cmap.is_hidden())
        self.page.reload()
        self.page.wait_for_function("(document.getElementById('overlay') || {style: {}}).style.display === 'none'", timeout=60000)
        self.page.get_by_test_id("cov-toggle").wait_for(state="visible")
        self.assertTrue(cmap.is_hidden())
        self.page.get_by_test_id("cov-toggle").click()
        cmap.wait_for(state="visible")
        self.shot("viewer-coverage")
        self.assertEqual(self.errors, [])

    def test_batch_of_videos_and_saved_defaults(self):
        self.open_new()
        # defaults first: they're what the next visit starts from
        self.page.click('.seg[data-name="quality"] button[data-v="preview"]')
        self.page.click('.seg[data-name="detail"] button[data-v="low"]')
        self.page.check("#removeBg")
        self.page.get_by_test_id("save-defaults").click()
        self.assertIn("Saved", self.page.text_content("#defaultsStatus"))
        self.page.evaluate("sessionStorage.clear()")
        self.open_new()
        self.assertEqual(self.page.get_attribute('.seg[data-name="quality"] button.on', "data-v"), "preview")
        self.assertEqual(self.page.get_attribute('.seg[data-name="detail"] button.on', "data-v"), "low")
        self.assertTrue(self.page.is_checked("#removeBg"))
        # three videos at once; one removed again
        self.page.click('.seg[data-name="keep"] button[data-v="one"]')
        vids = [str(make_video(self.tmp / f"{n}.mov", seconds=4)) for n in ("mug", "lamp", "cup")]
        self.page.set_input_files("#fileInput", vids)
        self.page.wait_for_function("window.newRun.videos().length === 3", timeout=60000)
        self.assertEqual(self.page.get_by_test_id("chosen-video").all_text_contents(), ["mug.mov", "lamp.mov", "cup.mov"])
        self.assertEqual(self.page.get_attribute('.seg[data-name="keep"] button.on', "data-v"), "all")   # one video only
        self.assertTrue(self.page.is_disabled('.seg[data-name="keep"] button[data-v="one"]'))
        self.assertIn("one at a time", self.page.text_content("#keepHelp"))
        self.assertEqual(self.page.text_content("#startBtn"), "Start 3 runs")
        self.page.click('[data-testid=picked] [data-remove="1"]')
        self.assertEqual(self.page.get_by_test_id("chosen-video").all_text_contents(), ["mug.mov", "cup.mov"])
        self.assertEqual(self.page.text_content("#startBtn"), "Start 2 runs")
        self.page.click("#startBtn")
        self.page.wait_for_url(self.url + "/", timeout=10000)
        self.page.wait_for_selector("text=2 runs queued", timeout=5000)
        _, jobs = self.json("GET", "/api/jobs")
        runs = [j for j in jobs if j["type"] == "video"]
        self.assertEqual(sorted(j["video"] for j in runs), ["videos/cup.mov", "videos/mug.mov"])
        for j in runs:
            self.assertEqual((j["options"]["quality"], j["options"]["detail"], j["options"].get("remove_background")),
                             ("preview", "low", True))
            self.assertNotIn("masks", j["options"])
        self.page.wait_for_function("document.querySelectorAll('[data-testid=run-done]').length === 2", timeout=240000)
        self.assertEqual(self.errors, [])

    def test_studio_and_new_run_are_separate_pages(self):
        self.page.goto(self.url)
        self.page.get_by_test_id("setup-pill").wait_for()
        self.assertEqual(self.page.locator("#fileInput").count(), 0)              # no form on the Studio page
        self.assertIn("Start a new run", self.page.get_by_test_id("runs").text_content())
        self.page.get_by_test_id("new-run").click()
        self.page.wait_for_url(self.url + "/new")
        self.assertEqual(self.page.get_attribute("[data-testid=nav-new]", "aria-current"), "page")
        self.assertTrue(self.page.is_disabled("#startBtn"))
        self.page.get_by_test_id("nav-studio").click()
        self.page.get_by_test_id("setup-pill").wait_for()
        self.assertEqual(self.errors, [])

    def test_header_footer_and_recording_guide(self):
        self.page.goto(self.url)
        self.page.get_by_test_id("setup-pill").wait_for()
        self.assertEqual(self.page.get_attribute("[data-testid=nav-studio]", "aria-current"), "page")
        self.assertIn("github.com/gpspelle/splat-studio", self.page.get_attribute("[data-testid=github-link]", "href"))
        self.page.wait_for_function("/^v\\d/.test(document.getElementById('siteVersion').textContent)")
        self.assertIn("MIT License", self.page.inner_text(".site-foot"))
        self.page.get_by_test_id("nav-new").click()
        self.assertIn("3 loops", self.page.get_by_test_id("tips-strip").inner_text())
        self.page.get_by_test_id("nav-guide").click()
        guide = self.page.get_by_test_id("guide")
        guide.wait_for(state="visible")
        self.assertEqual(self.page.evaluate("location.pathname"), "/guide")
        self.assertEqual(self.page.get_attribute("[data-testid=nav-guide]", "aria-current"), "page")
        self.assertIn("Lock focus and exposure", guide.inner_text())
        self.shot("guide", full_page=True)
        self.page.get_by_test_id("nav-library").click()
        self.page.get_by_test_id("setup-pill").wait_for()
        self.assertEqual(self.page.evaluate("location.hash"), "#library")
        self.assertEqual(self.errors, [])

    def test_upload_run_view_and_export(self):
        self.open_new()
        video = make_video(self.tmp / "IMG_4821.MOV", seconds=8, size="1280x720")
        self.page.set_input_files("#fileInput", str(video))
        advice = self.page.get_by_test_id("video-advice")
        advice.wait_for(state="visible")
        self.assertIn("too few viewpoints", advice.inner_text())   # 8 s is flagged before processing
        self.page.click('.seg[data-name="quality"] button[data-v="preview"]')
        self.page.click("#startBtn")
        self.page.wait_for_url(self.url + "/", timeout=10000)                     # follow it in the Studio
        # the stand-in tools are fast: the run may already be done at the next 2-second refresh
        self.page.wait_for_selector(".pill.running, .pill.done", timeout=30000)
        self.shot("run-progress")
        self.page.wait_for_selector("[data-testid=run-done] >> text=View splat", timeout=180000)
        self.page.click("[data-testid=run-done] [data-testid=run-details]")    # details: advice, other views, log
        advice = self.page.get_by_test_id("run-advice")
        advice.wait_for(timeout=30000)
        self.assertIn("Every frame was placed", advice.inner_text())
        self.page.wait_for_function("(document.querySelector('pre.log') || {}).textContent?.includes('3/3 Training')",
                                    timeout=30000)
        self.shot("studio", full_page=True)

        self.assertEqual(self.page.get_by_test_id("view-original").count(), 1)   # fog removal always runs
        self.page.click("text=View splat")
        self.page.wait_for_function("(document.getElementById('overlay') || {style: {}}).style.display === 'none' "
                                    "|| document.querySelector('.err')", timeout=60000)
        self.assertRegex(self.page.inner_text("#stats"), r"[\d,]+ splats")
        self.assertEqual(self.page.inner_text("#title"), "IMG_4821 · preview · fog removed")
        self.page.wait_for_timeout(1500)
        self.shot("viewer")
        with self.page.expect_download() as dl:
            self.page.click("#export")
        self.assertTrue(dl.value.suggested_filename.endswith("_viewer.zip"))
        self.assertEqual(self.errors, [])

    def test_imported_splat_renders_upright(self):
        ply = write_ply(self.tmp / "vase.ply", n=30000)
        self.page.goto(self.url)
        self.page.set_input_files("#importInput", str(ply))
        self.page.wait_for_selector(".tile >> text=vase.ply")
        self.page.click(".tile >> text=View")
        self.page.wait_for_function("(document.getElementById('overlay') || {style: {}}).style.display === 'none'", timeout=60000)
        self.assertIn("30,040 splats", self.page.inner_text("#stats"))
        self.assertNotIn("no up-axis", self.page.inner_text("#stats"))   # read from the PLY header
        # the file's "Vertical axis: 0 -1 0" must end up pointing up on screen
        up = self.page.evaluate("window.splatViewer.worldUp()")
        self.assertAlmostEqual(up[1], 1.0, places=3)
        # and the camera frames the vase, not the far-away floaters
        size = self.page.evaluate("window.splatViewer.fitSize()")
        self.assertLess(size, 5)
        self.page.click("#flip")
        self.assertAlmostEqual(self.page.evaluate("window.splatViewer.worldUp()")[1], -1.0, places=3)
        self.assertEqual(self.errors, [])

    def test_library_is_grouped_by_video(self):
        for video, run, files in (("vase", "run_1", ["preview_7000.ply", "standard_30000.ply"]),
                                  ("vase", "run_2", ["high_40000.ply"]), ("jar", "run_1", ["standard_30000.ply"])):
            for f in files:
                write_ply(self.data.workspace / video / run / "splat" / f, n=200)
        write_ply(self.data.imports / "scan.ply", n=200)
        self.page.goto(self.url)
        self.page.wait_for_selector(".tile >> text=scan.ply")
        groups = self.page.get_by_test_id("lib-group")
        self.assertEqual(sorted(groups.evaluate_all("els => els.map((e) => e.dataset.video)")), ["", "jar", "vase"])
        vase = self.page.locator("[data-testid=lib-group][data-video=vase]")
        self.assertEqual(vase.locator(".tile").count(), 3)       # both runs of the same video
        self.assertIn("3 splat(s)", vase.locator("summary").inner_text())
        self.assertIn("Imported files", self.page.locator("[data-testid=lib-group][data-video='']").inner_text())
        # a collapsed group stays collapsed when the library refreshes
        vase.locator("summary").click()
        self.page.wait_for_function("libCollapsed.has('vase')")    # the toggle event fires after the click
        self.page.evaluate("renderLibrary()")
        self.assertFalse(self.page.locator("[data-testid=lib-group][data-video=vase]").evaluate("e => e.open"))
        self.assertTrue(self.page.locator("[data-testid=lib-group][data-video=jar]").evaluate("e => e.open"))
        self.assertEqual(self.errors, [])


    def test_delete_splat_from_library(self):
        self.page.goto(self.url)
        for name in ("vase.ply", "jar.ply"):
            self.page.set_input_files("#importInput", str(write_ply(self.tmp / name, n=2000)))
            self.page.wait_for_selector(f".tile >> text={name}")
        asked = []
        self.page.on("dialog", lambda d: asked.append(d.message))     # accepted by the default handler
        self.page.click(".tile[data-path$='vase.ply'] [data-testid=delete-splat]")
        self.page.wait_for_selector(".tile[data-path$='vase.ply']", state="detached")
        self.assertEqual(len(asked), 1)
        self.assertIn("vase.ply", asked[0])                             # confirmation names the file
        self.assertFalse((self.data.imports / "vase.ply").exists())
        self.assertTrue((self.data.imports / "jar.ply").exists())
        self.assertEqual(self.errors, [])
    def add_finished_run(self, name, hours_ago, status="done", error=None):
        end = time.time() - hours_ago * 3600
        job = Job(self.data.jobs, id=f"{name}_{hours_ago}", type="video", status=status, video=f"videos/{name}.mov",
                  created=end - 600, started=end - 500, finished=end, options={"quality": "standard"}, error=error)
        job.save(force=True)
        self.runner.jobs[job.id] = job
        return job

    def test_older_runs_move_to_history(self):
        self.add_finished_run("Chair", 1)
        self.add_finished_run("Lamp", 2, "failed", "COLMAP could not reconstruct the scene")
        for i, h in enumerate((30, 50, 100, 200, 400, 900)):
            self.add_finished_run(f"Mug{i}", h, "cancelled" if i == 2 else "done")
        self.page.goto(self.url)
        # the Studio keeps the latest few finished runs as one line each, and links to the rest
        self.page.wait_for_selector("[data-testid=run-done]")
        rows = self.page.locator("#runs [data-testid=run-done]")
        self.assertEqual(rows.count(), 3)                               # 2 from today, topped up to 3
        self.assertIn("Chair", rows.first.inner_text())
        self.assertIn("could not reconstruct", rows.nth(1).inner_text())  # errors stay on the short line
        self.assertEqual(self.page.locator("#runs .steps:visible").count(), 0)  # steps only when opened
        self.assertEqual(self.page.inner_text("[data-testid=runs-history]"), "All runs (5 older)")
        self.shot("runs-recent")
        self.page.click("[data-testid=runs-history]")
        self.page.get_by_test_id("hist-day").first.wait_for()
        self.assertEqual(self.page.evaluate("location.pathname"), "/history")
        self.assertEqual(self.page.get_attribute("[data-testid=nav-history]", "aria-current"), "page")
        days = self.page.get_by_test_id("hist-day").evaluate_all("els => els.map((e) => e.querySelector('h2 span').textContent)")
        self.assertEqual(days[0], "Today")
        self.assertIn("Last 7 days", days)
        self.assertEqual(self.page.locator("[data-testid=run-done]").count(), 8)
        self.shot("history", full_page=True)
        self.page.click("[data-testid=hist-filter] button[data-v=failed]")
        self.assertEqual(self.page.locator("[data-testid=run-done]").count(), 1)
        self.page.click("[data-testid=hist-filter] button[data-v=all]")
        self.page.fill("[data-testid=hist-search]", "mug")
        self.assertEqual(self.page.locator("[data-testid=run-done]").count(), 6)
        # removing a run drops its record, never its files
        row = self.page.locator("[data-testid=run-done]").first
        row.get_by_test_id("run-details").click()
        row.get_by_test_id("run-remove").click()
        self.page.wait_for_function("document.querySelectorAll('[data-testid=run-done]').length === 5")
        self.assertNotIn("Mug0_30", self.runner.jobs)
        self.assertEqual(self.errors, [])


@unittest.skipIf(sync_playwright is None, "playwright not installed")
@needs_ffmpeg
class LanguageAndFogUiTest(BrowserTestCase):
    """Portuguese/English switching, and the viewer's handling of background fog."""

    def open_studio(self, lang=None):
        self.page.goto(self.url)
        if lang:
            self.page.evaluate(f"localStorage.setItem('lang', '{lang}')")
        self.page.reload()
        self.page.get_by_test_id("setup-pill").wait_for()

    def test_switch_to_portuguese_and_back(self):
        self.open_new()
        self.assertEqual(self.page.text_content("[data-i18n='new.title']"), "New run")
        self.page.get_by_test_id("lang-select").select_option("pt")
        self.assertEqual(self.page.text_content("[data-i18n='new.title']"), "Nova execução")
        self.assertIn("3 voltas", self.page.get_by_test_id("tips-strip").inner_text())
        self.assertEqual(self.page.inner_text("#startBtn"), "Iniciar")
        self.assertEqual(self.page.text_content(".seg[data-name='keep'] button[data-v='several']"), "Vários objetos")
        self.assertEqual(self.page.evaluate("document.documentElement.lang"), "pt-BR")
        self.page.reload()                                            # remembered
        self.page.wait_for_function("document.getElementById('prevVideo').options.length > 0")
        self.assertEqual(self.page.inner_text("#startBtn"), "Iniciar")
        self.page.get_by_test_id("nav-studio").click()
        self.page.get_by_test_id("setup-pill").wait_for()
        self.assertEqual(self.page.text_content("[data-testid=new-run]"), "Nova execução")
        self.page.wait_for_function("/ok|bad/.test(document.getElementById('setupPill').className)")
        self.assertIn("erramenta", self.page.text_content("#setupPill"))        # "Ferramentas prontas" / "Falta 1 ferramenta"
        self.page.get_by_test_id("nav-guide").click()
        self.assertIn("Trave foco e exposição", self.page.get_by_test_id("guide").inner_text())
        self.assertEqual(self.page.inner_text("[data-testid=nav-guide]"), "Guia de gravação")
        self.shot("guide-pt")
        self.page.get_by_test_id("nav-studio").click()
        self.page.get_by_test_id("setup-pill").wait_for()
        self.page.get_by_test_id("lang-select").select_option("en")
        self.assertEqual(self.page.text_content("[data-testid=new-run]"), "New run")
        self.assertEqual(self.errors, [])

    def test_upload_advice_is_translated(self):
        self.page.goto(self.url + "/new")
        self.page.evaluate("localStorage.setItem('lang', 'pt')")
        self.open_new()
        video = make_video(self.tmp / "curto.mov", seconds=5)
        self.page.set_input_files("#fileInput", str(video))
        advice = self.page.get_by_test_id("video-advice")
        advice.wait_for(state="visible")
        self.assertIn("poucos pontos de vista", advice.inner_text())
        self.page.get_by_test_id("lang-select").select_option("en")
        self.assertIn("too few viewpoints", advice.inner_text())
        self.assertEqual(self.errors, [])

    def test_turntable_video(self):
        import json as _json
        import subprocess
        self.page.set_viewport_size({"width": 640, "height": 480})   # software rendering in CI is slow
        self.open_studio()
        self.page.set_input_files("#importInput", str(write_ply(self.tmp / "vase.ply", n=8000)))
        self.page.wait_for_selector(".tile >> text=vase.ply")
        self.page.click(".tile >> text=View")
        self.page.wait_for_function("(document.getElementById('overlay') || {style: {}}).style.display === 'none'", timeout=60000)
        self.page.get_by_test_id("tt-open").click()
        panel = self.page.get_by_test_id("tt-panel")
        panel.wait_for(state="visible")
        self.page.click('[data-tt="format"] button[data-v="gif"]')
        self.assertEqual(self.page.get_attribute('[data-tt="format"] button.on', "data-v"), "gif")
        self.page.click("#ttCancel")
        self.assertTrue(panel.is_hidden())
        before = self.page.evaluate("window.splatViewer.fitCenter()")
        for fmt in ("mp4", "gif"):
            d = self.page.evaluate(f"window.splatViewer.turntable({{format: '{fmt}', seconds: 0.4, width: 160, height: 120}})")
            out = self.data.root / d["file"]
            self.assertEqual(out.name, f"vase_turntable.{fmt}")
            s = _json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,width",
                                             "-of", "json", str(out)], capture_output=True, text=True).stdout)["streams"][0]
            self.assertEqual(s["width"], 160)
            self.assertIn(s["codec_name"], ("h264", "mpeg4", "gif"))
        self.assertEqual(self.page.evaluate("window.splatViewer.fitCenter()"), before)
        self.assertEqual(self.page.evaluate("document.getElementById('overlay').style.display"), "none")
        _, lib = self.json("GET", "/api/splats")
        self.assertEqual([x["name"] for x in lib], ["vase.ply"])          # videos aren't splats
        self.assertEqual(self.errors, [])

    def test_viewer_frames_the_object_and_crops_the_fog(self):
        ply = write_foggy_scene(self.tmp / "room.ply", n_object=4000, n_fog=3000)
        self.page.set_viewport_size({"width": 640, "height": 480})   # software rendering in CI is slow
        self.open_studio()
        self.page.set_input_files("#importInput", str(ply))
        self.page.wait_for_selector(".tile >> text=room.ply")
        self.page.click(".tile >> text=View")
        self.page.wait_for_function("(document.getElementById('overlay') || {style: {}}).style.display === 'none'", timeout=60000)
        # framed on the object (~0.9 across), not the whole fog cloud (~8 across)
        self.assertLess(self.page.evaluate("window.splatViewer.fitSize()"), 2.0)
        self.page.wait_for_timeout(1500)
        self.shot("fog-before")
        before = self.coverage()
        self.page.get_by_test_id("crop-toggle").click()
        self.assertIsNotNone(self.page.evaluate("window.splatViewer.cropRadius()"))
        self.page.wait_for_timeout(2500)
        after = self.coverage()
        self.shot("fog-cropped")
        self.assertLess(after, before * 0.8, (before, after))         # fog no longer fills the frame
        self.assertGreater(after, 0.01)                               # but the object is still there
        self.page.get_by_test_id("crop-toggle").click()
        self.assertIsNone(self.page.evaluate("window.splatViewer.cropRadius()"))
        self.assertEqual(self.errors, [])

    def test_viewer_in_portuguese(self):
        ply = write_ply(self.tmp / "vaso.ply", n=5000)
        self.open_studio("pt")
        self.page.set_input_files("#importInput", str(ply))
        self.page.wait_for_selector(".tile >> text=vaso.ply")
        self.page.click(".tile >> text=Ver")
        self.page.wait_for_function("(document.getElementById('overlay') || {style: {}}).style.display === 'none'", timeout=60000)
        self.assertEqual(self.page.text_content("#crop"), "Recortar")       # (displayed in capitals by the stylesheet)
        self.assertEqual(self.page.text_content("#export"), "Exportar site")
        self.assertIn("splats", self.page.inner_text("#stats"))
        self.assertEqual(self.errors, [])


@unittest.skipIf(sync_playwright is None, "playwright not installed")
@needs_ffmpeg
class CompareAndBackgroundUiTest(BrowserTestCase):
    """Two splats side by side with linked cameras, and background removal from the library."""

    def open_studio(self):
        self.page.goto(self.url)
        self.page.get_by_test_id("setup-pill").wait_for()

    def import_two(self):
        self.page.set_input_files("#importInput", str(write_ply(self.tmp / "vase.ply", n=4000)))
        self.page.wait_for_selector(".tile >> text=vase.ply")
        self.page.set_input_files("#importInput", str(write_foggy_scene(self.tmp / "jar.ply", n_object=3000, n_fog=500)))
        self.page.wait_for_selector(".tile >> text=jar.ply")

    def open_compare(self):
        self.page.click(".tile[data-path$='vase.ply'] [data-testid=compare-select]")
        self.assertIn("1/2", self.page.inner_text("[data-testid=compare-open]"))
        self.page.click(".tile[data-path$='jar.ply'] [data-testid=compare-select]")
        self.assertEqual(self.page.locator(".tile.picked").count(), 2)
        self.page.click("[data-testid=compare-open]")
        self.page.wait_for_function("window.compareViewer && window.compareViewer.ready()", timeout=90000)

    def test_compare_two_splats_with_linked_cameras(self):
        self.page.set_viewport_size({"width": 900, "height": 520})       # software rendering in CI is slow
        self.open_studio()
        self.import_two()
        self.open_compare()
        cv = "window.compareViewer"
        self.assertEqual(self.page.evaluate(f"{cv}.numSplats(0)"), 4040)
        self.assertEqual(self.page.evaluate(f"{cv}.numSplats(1)"), 3500)
        titles = self.page.locator(".pane .title").all_inner_texts()
        self.assertEqual(titles, ["vase.ply", "jar.ply"])
        # side by side on a wide screen
        a, b = (self.page.get_by_test_id(t).bounding_box() for t in ("pane-a", "pane-b"))
        self.assertLess(a["x"] + a["width"], b["x"] + 5)
        self.page.wait_for_timeout(800)
        self.shot("compare")

        same = (f"() => {{ const a = {cv}.view(0), b = {cv}.view(1);"
                " return a.every((x, i) => Math.abs(x - b[i]) < 1e-3); }")

        def same_view():
            return self.page.evaluate(same)

        def follows():                     # software rendering can be slow: wait for the next frames
            self.page.wait_for_function(same, timeout=15000)
            return True

        self.page.evaluate(f"{cv}.orbit(0, 70)")                       # turn the left model...
        self.assertTrue(follows())                                       # ...the right one follows
        self.page.evaluate(f"{cv}.orbit(1, -30)")                      # and the other way round
        self.assertTrue(follows())
        self.page.get_by_test_id("link-toggle").click()                  # unlink
        self.assertFalse(self.page.evaluate(f"{cv}.linked()"))
        self.page.evaluate(f"{cv}.orbit(0, 90)")
        self.page.wait_for_timeout(400)
        self.assertFalse(same_view())
        with self.page.expect_download() as dl:
            self.page.get_by_test_id("compare-export").click()
        self.assertTrue(dl.value.suggested_filename.endswith("_compare.zip"))
        self.assertEqual(self.errors, [])

    def test_turn_each_model_on_its_own(self):
        self.page.set_viewport_size({"width": 1000, "height": 560})
        self.open_studio()
        self.import_two()
        self.open_compare()
        cv = "window.compareViewer"
        q = lambda i: self.page.evaluate(f"{cv}.modelQuat({i})")
        view = lambda i: self.page.evaluate(f"{cv}.view({i})")
        close = lambda a, b: max(abs(x - y) for x, y in zip(a, b)) < 1e-3
        a0, b0, cam0 = q(0), q(1), view(0)
        bar = self.page.get_by_test_id("model-bar-a")
        bar.locator(".right").click()
        bar.locator(".right").click()
        self.assertFalse(close(q(0), a0))                                  # A turned...
        self.assertTrue(close(q(1), b0))                                   # ...B didn't
        self.assertTrue(close(view(0), cam0))                              # and the camera stayed put
        self.assertTrue(self.page.evaluate(f"{cv}.linked()"))              # cameras still linked
        # "Turn model" is on for both at first: dragging turns the model, not the camera
        for L, i in (("a", 0), ("b", 1)):
            self.assertEqual(self.page.get_attribute(f"[data-testid=turn-{L}]", "aria-checked"), "true")
            self.assertEqual(self.page.get_attribute(f"[data-testid=turn-{L}]", "role"), "switch")
        box = self.page.get_by_test_id("pane-b").bounding_box()
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

        def drag():
            self.page.mouse.move(cx, cy)
            self.page.mouse.down()
            self.page.mouse.move(cx + 120, cy + 20, steps=6)
            self.page.mouse.up()
            self.page.wait_for_timeout(300)

        cam_b = view(1)
        drag()
        self.assertFalse(close(q(1), b0))
        self.assertTrue(close(view(1), cam_b))
        # switched off, the model is locked: a drag turns neither the model nor the camera
        self.page.get_by_test_id("turn-b").click()
        self.assertEqual(self.page.get_attribute("[data-testid=turn-b]", "aria-checked"), "false")
        locked_q, locked_cam = q(1), view(1)
        drag()
        self.assertTrue(close(q(1), locked_q))
        self.assertTrue(close(view(1), locked_cam))
        # Reset puts a model back; Save view remembers both for the viewer
        self.page.get_by_test_id("orig-b").click()
        self.assertTrue(close(q(1), b0))
        self.page.get_by_test_id("compare-save").click()
        saved = self.page.evaluate("JSON.parse(localStorage.getItem('rot:imports/vase.ply'))")
        self.assertTrue(close(saved, q(0)))
        self.page.wait_for_timeout(600)
        self.shot("compare-turn")
        self.assertEqual(self.errors, [])

    def test_compare_stacks_on_phones(self):
        self.page.set_viewport_size({"width": 390, "height": 760})
        self.open_studio()
        self.import_two()
        self.open_compare()
        a, b = (self.page.get_by_test_id(t).bounding_box() for t in ("pane-a", "pane-b"))
        self.assertLess(a["y"] + a["height"], b["y"] + 5)                # one above the other
        self.assertGreater(a["width"], 380)
        bar = self.page.get_by_test_id("model-bar-b").bounding_box()      # turn controls fit in their pane
        self.assertLessEqual(bar["x"] + bar["width"], b["x"] + b["width"])
        self.assertGreaterEqual(bar["y"], b["y"])
        self.shot("compare-phone")
        self.assertEqual(self.errors, [])

    def test_exported_comparison_site_works_standalone(self):
        import io, zipfile, http.server, threading, functools
        self.page.set_viewport_size({"width": 800, "height": 480})
        self.open_studio()
        self.import_two()
        q = "a=imports/vase.ply&b=imports/jar.ply&ta=Vase&tb=Jar"
        import urllib.request
        with urllib.request.urlopen(f"{self.url}/api/export-compare?{q}") as r:
            data = r.read()
        site = self.tmp / "site"
        zipfile.ZipFile(io.BytesIO(data)).extractall(site)
        class Quiet(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *a):
                pass
        handler = functools.partial(Quiet, directory=str(site))
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            self.page.goto(f"http://127.0.0.1:{srv.server_address[1]}/")
            self.page.wait_for_function("window.compareViewer && window.compareViewer.ready()", timeout=90000)
            self.assertEqual(self.page.locator(".pane .title").all_inner_texts(), ["Vase", "Jar"])
            self.assertFalse(self.page.locator("[data-testid=compare-export]").is_visible())   # app-only
        finally:
            srv.shutdown()
        self.assertEqual(self.errors, [])

    def test_remove_background_from_library(self):
        from plyfile import write_room_scene
        self.open_new()
        self.assertFalse(self.page.get_by_test_id("remove-bg-option").is_checked())   # opt-in
        self.open_studio()
        self.page.set_input_files("#importInput", str(write_room_scene(self.tmp / "room.ply")))
        self.page.wait_for_selector(".tile >> text=room.ply")
        self.page.click(".tile[data-path$='room.ply'] [data-testid=isolate-button]")
        self.page.wait_for_selector(".tile >> text=room_object.ply", timeout=60000)
        # the object copy offers neither fog cleaning nor another background removal
        tile = self.page.locator(".tile[data-path$='room_object.ply']")
        self.assertEqual(tile.locator("[data-testid=isolate-button]").count(), 0)
        self.assertEqual(tile.locator("[data-testid=clean-button]").count(), 0)
        self.page.wait_for_selector("text=Background removed", timeout=30000)        # advice on the run card
        self.assertEqual(self.errors, [])


@unittest.skipIf(sync_playwright is None, "playwright not installed")
@needs_ffmpeg
class ThemeUiTest(BrowserTestCase):
    def bg(self):
        return self.page.evaluate("getComputedStyle(document.body).backgroundColor")

    def test_follows_the_system_and_can_be_switched(self):
        self.page.emulate_media(color_scheme="dark")
        self.page.goto(self.url)
        self.page.get_by_test_id("setup-pill").wait_for()
        self.assertEqual(self.bg(), "rgb(13, 13, 12)")                    # system dark
        self.page.get_by_test_id("theme-select").select_option("light")
        self.assertEqual(self.bg(), "rgb(239, 237, 231)")
        self.page.reload()                                                 # remembered
        self.page.get_by_test_id("setup-pill").wait_for()
        self.assertEqual(self.bg(), "rgb(239, 237, 231)")
        self.page.get_by_test_id("lang-select").select_option("pt")
        self.assertEqual(self.page.locator("[data-testid=theme-select] option").all_text_contents(),
                         ["Sistema", "Claro", "Escuro"])
        self.page.get_by_test_id("theme-select").select_option("system")
        self.assertEqual(self.bg(), "rgb(13, 13, 12)")
        # fonts load from the app itself, never from the internet
        self.page.wait_for_function("document.fonts.check('14px Inter') && document.fonts.check('12px \"JetBrains Mono\"')")
        self.assertEqual(self.errors, [])

    def test_keyboard_focus_is_visible(self):
        self.page.goto(self.url + "/new")
        self.page.wait_for_selector("#dropZone")
        self.page.focus("#dropZone")
        self.page.keyboard.press("Tab")
        self.page.keyboard.press("Shift+Tab")                              # keyboard focus, so :focus-visible applies
        style = self.page.evaluate("getComputedStyle(document.activeElement).outlineStyle")
        self.assertEqual(style, "solid")
