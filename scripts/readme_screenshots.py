#!/usr/bin/env python3
"""Regenerate the README screenshots in docs/images/.

Runs the app on a temporary data folder with the stand-in COLMAP/Brush (tests/fakes), so it
works anywhere. To get realistic thumbnails it first "films" a synthetic splat orbiting in
the viewer and uses that clip as the input video.

    python3 scripts/readme_screenshots.py          # needs: ffmpeg, playwright + chromium
"""
from __future__ import annotations

import os
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
        page.goto(url)
        page.get_by_test_id("guide").wait_for(state="visible")
        page.wait_for_timeout(400)
        page.screenshot(path=str(OUT / "recording-guide.png"))
        page.locator("#guide button:has-text('Close')").click()
        page.set_input_files("#fileInput", str(video))
        page.get_by_test_id("video-advice").wait_for(state="visible")
        page.click('.seg[data-name="quality"] button[data-v="standard"]')
        page.screenshot(path=str(OUT / "upload-checks.png"), clip={"x": 90, "y": 88, "width": 382, "height": 520})
        page.click("#startBtn")
        page.wait_for_selector("text=training splat", timeout=300000)
        page.wait_for_timeout(1500)
        page.screenshot(path=str(OUT / "run-progress.png"), clip={"x": 490, "y": 88, "width": 742, "height": 190})
        page.wait_for_selector("text=View splat", timeout=180000)
        page.wait_for_timeout(2500)
        page.screenshot(path=str(OUT / "studio.png"))

        # 3. the viewer
        page.click("text=View splat")
        page.wait_for_function("(document.getElementById('overlay') || {style: {}}).style.display === 'none'", timeout=60000)
        page.wait_for_timeout(1500)
        page.screenshot(path=str(OUT / "viewer.png"))
        browser.close()
    srv.shutdown()
    print("\n".join(str(p.relative_to(ROOT)) for p in sorted(OUT.glob("*.png"))))


if __name__ == "__main__":
    main()
