"""Drives the real web UI in a headless browser (needs `pip install playwright` + a Chromium).

Skipped automatically when Playwright or a browser isn't available. Set SPLAT_SCREENSHOTS
to a folder to save screenshots (used for the README).
"""
import os
import time
import unittest
from pathlib import Path

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
        self.page.wait_for_function("(document.getElementById('overlay') || {style: {}}).style.display === 'none' "
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
        self.page.evaluate("localStorage.setItem('guide:seen', 'true')")
        self.page.reload()
        self.page.wait_for_selector(".tile >> text=scan.ply")
        groups = self.page.get_by_test_id("lib-group")
        self.assertEqual(sorted(groups.evaluate_all("els => els.map((e) => e.dataset.video)")), ["", "jar", "vase"])
        vase = self.page.locator("[data-testid=lib-group][data-video=vase]")
        self.assertEqual(vase.locator(".tile").count(), 3)       # both runs of the same video
        self.assertIn("3 splat(s)", vase.locator("summary").inner_text())
        self.assertIn("Imported files", self.page.locator("[data-testid=lib-group][data-video='']").inner_text())
        # a collapsed group stays collapsed when the library refreshes
        vase.locator("summary").click()
        self.page.evaluate("renderLibrary()")
        self.assertFalse(self.page.locator("[data-testid=lib-group][data-video=vase]").evaluate("e => e.open"))
        self.assertTrue(self.page.locator("[data-testid=lib-group][data-video=jar]").evaluate("e => e.open"))
        self.assertEqual(self.errors, [])


    def test_delete_splat_from_library(self):
        self.page.goto(self.url)
        self.page.evaluate("localStorage.setItem('guide:seen', 'true')")
        self.page.reload()
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

@unittest.skipIf(sync_playwright is None, "playwright not installed")
@needs_ffmpeg
class LanguageAndFogUiTest(BrowserTestCase):
    """Portuguese/English switching, and the viewer's handling of background fog."""

    def open_studio(self, lang=None):
        self.page.goto(self.url)
        self.page.evaluate("localStorage.setItem('guide:seen', 'true')")
        if lang:
            self.page.evaluate(f"localStorage.setItem('lang', '{lang}')")
        self.page.reload()
        self.page.get_by_test_id("setup-pill").wait_for()

    def test_switch_to_portuguese_and_back(self):
        self.open_studio()
        self.assertEqual(self.page.text_content("[data-i18n='new.title']"), "New run")
        self.page.get_by_test_id("lang-select").select_option("pt")
        self.assertEqual(self.page.text_content("[data-i18n='new.title']"), "Nova execução")
        self.assertIn("3 voltas", self.page.get_by_test_id("tips-strip").inner_text())
        self.assertEqual(self.page.inner_text("#startBtn"), "Iniciar")
        self.assertEqual(self.page.evaluate("document.documentElement.lang"), "pt-BR")
        self.page.reload()                                            # remembered
        self.page.get_by_test_id("setup-pill").wait_for()
        self.assertEqual(self.page.inner_text("#startBtn"), "Iniciar")
        self.page.wait_for_function("/ok|bad/.test(document.getElementById('setupPill').className)")
        self.assertIn("erramenta", self.page.text_content("#setupPill"))        # "Ferramentas prontas" / "Falta 1 ferramenta"
        self.page.get_by_test_id("guide-button").click()
        self.assertIn("Trave foco e exposição", self.page.get_by_test_id("guide").inner_text())
        self.shot("guide-pt")
        self.page.locator("#guide button[data-i18n='dlg.close']").click()
        self.page.get_by_test_id("lang-select").select_option("en")
        self.assertEqual(self.page.inner_text("#startBtn"), "Start run")
        self.assertEqual(self.errors, [])

    def test_upload_advice_is_translated(self):
        self.open_studio("pt")
        video = make_video(self.tmp / "curto.mov", seconds=5)
        self.page.set_input_files("#fileInput", str(video))
        advice = self.page.get_by_test_id("video-advice")
        advice.wait_for(state="visible")
        self.assertIn("poucos pontos de vista", advice.inner_text())
        self.page.get_by_test_id("lang-select").select_option("en")
        self.assertIn("too few viewpoints", advice.inner_text())
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
        self.page.evaluate("localStorage.setItem('guide:seen', 'true')")
        self.page.reload()
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

    def test_compare_stacks_on_phones(self):
        self.page.set_viewport_size({"width": 390, "height": 760})
        self.open_studio()
        self.import_two()
        self.open_compare()
        a, b = (self.page.get_by_test_id(t).bounding_box() for t in ("pane-a", "pane-b"))
        self.assertLess(a["y"] + a["height"], b["y"] + 5)                # one above the other
        self.assertGreater(a["width"], 380)
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
        self.open_studio()
        self.assertFalse(self.page.get_by_test_id("remove-bg-option").is_checked())   # opt-in
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
        self.page.evaluate("localStorage.setItem('guide:seen', 'true')")
        self.page.reload()
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
        self.page.goto(self.url)
        self.page.evaluate("localStorage.setItem('guide:seen', 'true')")
        self.page.reload()
        self.page.get_by_test_id("setup-pill").wait_for()
        self.page.focus("#dropZone")
        self.page.keyboard.press("Tab")
        self.page.keyboard.press("Shift+Tab")                              # keyboard focus, so :focus-visible applies
        style = self.page.evaluate("getComputedStyle(document.activeElement).outlineStyle")
        self.assertEqual(style, "solid")
