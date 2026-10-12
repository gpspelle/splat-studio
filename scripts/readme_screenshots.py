#!/usr/bin/env python3
"""Regenerate the README screenshots in docs/images/.

Runs the app on a temporary data folder with the stand-in COLMAP/Brush (tests/fakes), so it
works anywhere. To get realistic thumbnails it first "films" a synthetic splat orbiting in
the viewer and uses that clip as the input video.

    python3 scripts/readme_screenshots.py          # needs: ffmpeg, playwright + chromium
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests" / "fakes"), str(ROOT)]
from playwright.sync_api import sync_playwright  # noqa: E402

from plyfile import write_ply  # noqa: E402
from splat_studio.server.app import create_server  # noqa: E402
from splat_studio.server.jobs import Job  # noqa: E402

OUT = ROOT / "docs" / "images"
ARGS = ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]


def film_vase(out: Path, n_frames: int = 36, size=(3840, 2160)):
    """Render the test scene (striped vase on a checkered disc) orbiting, as simple soft dots."""
    import math
    import random

    import cv2
    import numpy as np
    rnd = random.Random(2)
    pts, cols = [], []
    for _ in range(14000):
        h, a = rnd.random(), rnd.random() * 2 * math.pi
        r = 0.35 + 0.18 * math.sin(h * math.pi * 1.6) - 0.12 * h
        pts.append((r * math.cos(a), h * 1.2, r * math.sin(a)))
        cols.append((0.15, 0.35, 0.85) if int(h * 8) % 2 else (0.6, 0.85, 0.95))       # BGR
    for _ in range(9000):
        rr, a = math.sqrt(rnd.random()) * 1.4, rnd.random() * 2 * math.pi
        g = 0.35 + 0.2 * ((int(rr * 6) + int(a * 3)) % 2)
        pts.append((rr * math.cos(a), 0.0, rr * math.sin(a)))
        cols.append((g * 1.05, g, g))
    P, C = np.array(pts), (np.array(cols) * 255).astype(int)
    out.mkdir(parents=True, exist_ok=True)
    w, h = size
    for i in range(n_frames):
        t = 2 * math.pi * i / n_frames
        cam = np.array([3.0 * math.sin(t), 1.6, 3.0 * math.cos(t)])
        fwd = np.array([0, 0.45, 0]) - cam
        fwd /= np.linalg.norm(fwd)
        right = np.cross(fwd, [0, 1, 0])
        right /= np.linalg.norm(right)
        up = np.cross(right, fwd)
        rel = P - cam
        x, y, z = rel @ right, rel @ up, rel @ fwd
        f = 1.1 * w / 2
        u, v = (w / 2 + f * x / z).astype(int), (h / 2 - f * y / z).astype(int)
        img = np.full((h, w, 3), (232, 238, 240), np.uint8)
        for k in np.argsort(-z):
            cv2.circle(img, (int(u[k]), int(v[k])), max(3, int(52 / z[k])), tuple(int(c) for c in C[k]), -1,
                       cv2.LINE_AA)
        cv2.imwrite(str(out / f"f{i:03d}.png"), cv2.GaussianBlur(img, (7, 7), 0))


def add_past_runs(runner, data):
    """A few weeks of earlier runs, copied from the real one, so the run list and History page look lived in."""
    real = next(j for j in runner.jobs.values() if j.status == "done")
    frames = data.frames / data.frames_name(real.video)
    now = time.time()
    for name, hours, status in (("Teapot", 3, "done"), ("Desk_Plant", 20, "failed"), ("Sneaker", 30, "done"),
                                ("Ceramic_Bowl", 75, "cancelled"), ("Teapot", 120, "done"), ("Bike_Helmet", 300, "done"),
                                ("Desk_Plant", 520, "done"), ("Old_Camera", 800, "done")):
        shutil.copytree(frames, data.frames / name, dirs_exist_ok=True)
        end = now - hours * 3600
        fields = {**real.to_dict(), "id": f"past_{name}_{hours}", "video": f"videos/{name}.mov", "status": status,
                  "created": end - 900, "started": end - 860, "finished": end}
        if status != "done":
            fields.update(splat=None, clean_splat=None, object_splat=None, stage=2 if status == "failed" else 1,
                          registered="41/250 frames (16%)" if status == "failed" else None,
                          frames=250 if status == "failed" else None,
                          error="Not good enough: only 41 of 250 frames could be placed" if status == "failed" else None)
        runner.jobs[fields["id"]] = Job(data.jobs, **fields)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="splat-shots-"))
    os.environ.update({"PATH": f"{ROOT / 'tests/fakes/bin'}{os.pathsep}{os.environ['PATH']}",
                       "SPLAT_IN_CONTAINER": "1", "SPLAT_STUDIO_QUIET": "1", "FAKE_BRUSH_DELAY": "0.25",
                       "FAKE_COLMAP_DELAY": "0.01", "SPLAT_STUDIO_RATES": str(tmp / "rates.json")})
    srv, runner, data = create_server(tmp / "data", port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=ARGS)
        # 1. a demo "phone video": the synthetic vase seen from 36 angles (rendered with OpenCV)
        frames = tmp / "film"
        film_vase(frames)
        video = tmp / "Ceramic_Vase.mov"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-stream_loop", "20", "-framerate", "6",
                        "-i", str(frames / "f%03d.png"), "-r", "30", "-pix_fmt", "yuv420p", "-t", "95", str(video)],
                       check=True)

        # 2. the studio
        page = browser.new_page(viewport={"width": 1320, "height": 900}, device_scale_factor=2)
        page.goto(url + "/guide")
        page.get_by_test_id("guide").wait_for(state="visible")
        page.wait_for_timeout(400)
        page.screenshot(path=str(OUT / "recording-guide.png"))
        page.goto(url)
        page.set_input_files("#fileInput", str(video))
        page.get_by_test_id("video-advice").wait_for(state="visible")
        page.click('.seg[data-name="quality"] button[data-v="standard"]')
        page.locator("main > section.card").first.screenshot(path=str(OUT / "upload-checks.png"))
        page.click("#startBtn")
        page.wait_for_selector("[data-testid=run-active] .now-line >> text=training splat", timeout=300000)
        page.wait_for_timeout(300)
        page.locator("[data-testid=run-active]").first.screenshot(path=str(OUT / "run-progress.png"))
        page.wait_for_selector("text=View splat", timeout=180000)
        add_past_runs(runner, data)
        page.wait_for_selector("[data-testid=runs-history]", timeout=10000)
        page.wait_for_timeout(2500)
        page.evaluate("window.scrollTo(0, 0)")
        page.screenshot(path=str(OUT / "studio.png"))

        # 3. the viewer
        page.click("text=View splat")
        page.wait_for_function("(document.getElementById('overlay') || {style: {}}).style.display === 'none'", timeout=60000)
        page.wait_for_timeout(1500)
        page.screenshot(path=str(OUT / "viewer.png"))

        # 4. older runs, on the History page
        page.goto(url + "/history")
        page.get_by_test_id("hist-day").first.wait_for()
        page.wait_for_timeout(500)
        page.screenshot(path=str(OUT / "history.png"))

        # 5. background removal + comparison: a vase on a table (fog removed) vs the object alone
        from plyfile import write_room_scene
        page.goto(url)
        page.set_input_files("#importInput", str(write_room_scene(tmp / "Vase_on_table.ply")))
        page.wait_for_selector(".tile >> text=Vase_on_table.ply")
        page.click(".tile[data-path$='Vase_on_table.ply'] [data-testid=isolate-button]")
        page.wait_for_selector(".tile >> text=Vase_on_table_object.ply", timeout=120000)
        page.click(".tile[data-path$='Vase_on_table.ply'] [data-testid=clean-button]")      # fog only, for "before"
        page.wait_for_selector(".tile >> text=Vase_on_table_clean.ply", timeout=120000)
        page.click(".tile[data-path$='Vase_on_table_clean.ply'] [data-testid=compare-select]")
        page.click(".tile[data-path$='Vase_on_table_object.ply'] [data-testid=compare-select]")
        page.click("[data-testid=compare-open]")
        page.wait_for_function("window.compareViewer && window.compareViewer.ready()", timeout=120000)
        page.evaluate("window.compareViewer.orbit(0, 25)")
        for _ in range(3):                                  # turn model B on its own, 45°
            page.click("[data-testid=model-bar-b] .right")
        page.wait_for_timeout(2500)
        page.screenshot(path=str(OUT / "compare.png"), timeout=300000)

        # 6. choosing what to keep (the stand-in "disc" selection instead of SAM 2)
        os.environ["SPLAT_STUDIO_MASK_BACKEND"] = "disc"
        page.close()                                        # the comparison's WebGL is slow to unload
        page = browser.new_page(viewport={"width": 1320, "height": 900}, device_scale_factor=2)
        page.goto(url + "/keep?video=videos/Ceramic_Vase.mov")
        canvas = page.get_by_test_id("keep-canvas")
        canvas.wait_for(state="visible", timeout=30000)
        page.wait_for_function("document.getElementById('frameImg').naturalWidth > 0")
        page.evaluate("localStorage.clear()")
        box = canvas.bounding_box()
        page.mouse.click(box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.42)
        page.get_by_test_id("tool-drop").click()
        page.mouse.click(box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.88)
        page.get_by_test_id("keep-apply").click()
        page.wait_for_function("window.keepPage.masksDone() && window.keepPage.maskShown()", timeout=120000)
        page.wait_for_timeout(800)
        page.screenshot(path=str(OUT / "keep.png"))
        browser.close()
    srv.shutdown()
    print("\n".join(str(p.relative_to(ROOT)) for p in sorted(OUT.glob("*.png"))))


if __name__ == "__main__":
    main()
