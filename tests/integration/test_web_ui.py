"""Drives the real web UI in a headless browser (needs `pip install playwright` + a Chromium).

Skipped automatically when Playwright or a browser isn't available. Set SPLAT_SCREENSHOTS
to a folder to save screenshots (used for the README).
"""
import os
import time
import unittest
from pathlib import Path

from tests.helpers import make_video, needs_ffmpeg, write_ply
from tests.integration.test_server_api import ServerTestCase

try:
    from playwright.sync_api import sync_playwright
except ImportError:              # pragma: no cover
    sync_playwright = None

SHOTS = os.environ.get("SPLAT_SCREENSHOTS")
BROWSER_ARGS = ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]


@unittest.skipIf(sync_playwright is None, "playwright not installed")
@needs_ffmpeg
class WebUiTest(ServerTestCase):
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

    def shot(self, name, full_page=False, page=None):
        if SHOTS:
            Path(SHOTS).mkdir(parents=True, exist_ok=True)
            (page or self.page).screenshot(path=str(Path(SHOTS) / f"{name}.png"), full_page=full_page)

    def test_first_visit_shows_recording_guide(self):
        self.page.goto(self.url)
        guide = self.page.get_by_test_id("guide")
        guide.wait_for(state="visible")
        self.assertIn("3 loops", self.page.get_by_test_id("tips-strip").inner_text())
        self.assertIn("Lock focus and exposure", guide.inner_text())
        self.shot("guide")
        self.page.locator("#guide button:has-text('Close')").click()
        self.page.reload()
        self.page.wait_for_timeout(800)
        self.assertFalse(guide.is_visible())                     # only opens automatically once
        self.page.get_by_test_id("guide-button").click()
        guide.wait_for(state="visible")
        self.assertEqual(self.errors, [])

    def test_upload_run_view_and_export(self):
        self.page.goto(self.url)
        self.page.evaluate("localStorage.setItem('guide:seen', 'true')")
        self.page.reload()
        self.page.get_by_test_id("setup-pill").wait_for()
        video = make_video(self.tmp / "IMG_4821.MOV", seconds=8, size="1280x720")
        self.page.set_input_files("#fileInput", str(video))
        advice = self.page.get_by_test_id("video-advice")
        advice.wait_for(state="visible")
        self.assertIn("too few viewpoints", advice.inner_text())   # 8 s is flagged before processing
        self.page.click('.seg[data-name="quality"] button[data-v="preview"]')
        self.page.click("#startBtn")
        # the stand-in tools are fast: the run may already be done at the next 2-second refresh
        self.page.wait_for_selector(".pill.running, .pill.done", timeout=30000)
        self.shot("run-progress")
        self.page.wait_for_selector("text=View splat", timeout=180000)
        advice = self.page.get_by_test_id("run-advice")          # shown once the run is marked done
        advice.wait_for(timeout=30000)
        self.assertIn("Excellent capture", advice.inner_text())
        self.page.click(".run button.ghost")                      # details + log
        self.page.wait_for_function("(document.querySelector('pre.log') || {}).textContent?.includes('3/3 Training')",
                                    timeout=30000)
        self.shot("studio", full_page=True)

        self.page.click("text=View splat")
        self.page.wait_for_function("document.getElementById('overlay').style.display === 'none' "
                                    "|| document.querySelector('.err')", timeout=60000)
        self.assertRegex(self.page.inner_text("#stats"), r"[\d,]+ splats")
        self.assertEqual(self.page.inner_text("#title"), "IMG_4821 · preview")
        self.page.wait_for_timeout(1500)
        self.shot("viewer")
        with self.page.expect_download() as dl:
            self.page.click("#export")
        self.assertTrue(dl.value.suggested_filename.endswith("_viewer.zip"))
        self.assertEqual(self.errors, [])

    def test_imported_splat_renders_upright(self):
        ply = write_ply(self.tmp / "vase.ply", n=30000)
        self.page.goto(self.url)
        self.page.evaluate("localStorage.setItem('guide:seen', 'true')")
        self.page.reload()
        self.page.set_input_files("#importInput", str(ply))
        self.page.wait_for_selector(".tile >> text=vase.ply")
        self.page.click(".tile >> text=View")
        self.page.wait_for_function("document.getElementById('overlay').style.display === 'none'", timeout=60000)
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
