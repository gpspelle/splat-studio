"""Shared test helpers: temp data folders, fake tools on PATH, sample videos and splats."""
from __future__ import annotations

import contextlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAKES = ROOT / "tests" / "fakes"
FAKE_BIN = FAKES / "bin"
sys.path.insert(0, str(FAKES))
from plyfile import write_ply  # noqa: E402,F401

# Time estimates learn from timings.json; tests must never read or change the real one.
os.environ.setdefault("SPLAT_STUDIO_TIMINGS", str(Path(tempfile.mkdtemp(prefix="splat-timings-")) / "timings.json"))

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = unittest.skipUnless(HAS_FFMPEG, "ffmpeg/ffprobe not installed")


class TempDirTestCase(unittest.TestCase):
    """Gives each test a fresh temp folder (self.tmp) and isolates saved speeds and Brush lookups."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="splat-test-")
        self.tmp = Path(self._td.name).resolve()
        self._env = mock_env({
            "SPLAT_STUDIO_RATES": str(self.tmp / "rates.json"),
            "SPLAT_STUDIO_TIMINGS": str(self.tmp / "timings.json"),
            "SPLAT_STUDIO_TOOLS": str(self.tmp / "tools"),
            "HOME": str(self.tmp / "home"),
        })
        self._env.__enter__()
        (self.tmp / "home").mkdir()

    def tearDown(self):
        self._env.__exit__(None, None, None)
        self._td.cleanup()


@contextlib.contextmanager
def mock_env(values: dict):
    old = {k: os.environ.get(k) for k in values}
    os.environ.update({k: v for k, v in values.items() if v is not None})
    for k, v in values.items():
        if v is None:
            os.environ.pop(k, None)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def fake_tools_env(**extra) -> dict:
    """Environment with the fake colmap/brush first on PATH and the package importable."""
    env = dict(os.environ)
    env["PATH"] = f"{FAKE_BIN}{os.pathsep}{env.get('PATH', '')}"
    env["PYTHONPATH"] = f"{ROOT / 'src'}{os.pathsep}{env.get('PYTHONPATH', '')}"
    env["SPLAT_IN_CONTAINER"] = "1"      # skip the macOS installer step in tests
    env.update({k: str(v) for k, v in extra.items()})
    return env


def make_video(path: Path, seconds: int = 8, size: str = "640x360", rate: int = 30) -> Path:
    """A small synthetic test video (moving test pattern) made with ffmpeg."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc2=duration={seconds}:size={size}:rate={rate}", "-pix_fmt", "yuv420p", str(path)],
                   check=True)
    return path


def run_module(module: str, args: list, cwd: Path, env: dict | None = None, timeout: int = 120):
    return subprocess.run([sys.executable, "-m", f"splat_studio.{module}", *map(str, args)], cwd=cwd,
                          env=env or fake_tools_env(), capture_output=True, text=True, timeout=timeout)


# ---------------------------------------------------------------- iPhone captures (Splat Capture packages)

def _norm(v):
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def arkit_pose(centre, target=(0.0, 0.0, 0.0), held: str = "portrait") -> list[float]:
    """ARKit camera-to-world matrix (row-major, 16 numbers) for a phone at `centre` looking at
    `target`. ARKit's camera: x right in the sensor's landscape image, y up in it, z back; world y up.
    held: "portrait" (upright phone: the image's x points down), "landscape" (image up is world up)
    or "upside-down" (landscape, turned over)."""
    f = _norm([t - c for t, c in zip(target, centre)])
    z = [-v for v in f]
    up = _norm([u - f[1] * fv for u, fv in zip((0.0, 1.0, 0.0), f)])      # world up, square to the view
    if held == "portrait":
        x = [-v for v in up]
        y = _cross(z, x)
    else:
        y = up if held == "landscape" else [-v for v in up]
        x = _cross(y, z)
    m = [[x[r], y[r], z[r], centre[r]] for r in range(3)] + [[0.0, 0.0, 0.0, 1.0]]
    return [v for row in m for v in row]


def make_capture(folder: Path, name: str = "capture_test", frames: int = 60, size=(320, 240), held="portrait",
                 limited: int = 4, points: int = 400, zip_it: bool = True, meta: dict | None = None) -> Path:
    """A small Splat Capture package: frame N of video.mov is a flat grey of brightness 20 + 3N
    (so a test can tell which frame it got), the phone circles the origin at three heights, the
    first `limited` frames have limited tracking, and points.json holds a ball of points."""
    root = folder / name
    root.mkdir(parents=True, exist_ok=True)
    w, h = size
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"color=c=gray:s={w}x{h}:r=30:d={frames / 30 + 1}",
                    "-vf", "geq=lum='min(255,20+3*N)':cb=128:cr=128", "-frames:v", str(frames),
                    "-c:v", "libx264", "-qp", "0", "-pix_fmt", "yuv420p", str(root / "video.mov")], check=True)
    K = [w * 0.8, w * 0.8, w / 2, h / 2]
    with open(root / "frames.jsonl", "w") as f:
        for i in range(frames):
            a = 2 * math.pi * i / frames
            c = (1.5 * math.cos(a), (-0.3, 0.2, 0.7)[i % 3], 1.5 * math.sin(a))
            row = {"i": i, "t": i / 30, "pose": arkit_pose(c, held=held), "K": K,
                   "tracking": "limited-initializing" if i < limited else "normal"}
            f.write(json.dumps(row) + "\n")
    pts = []
    for i in range(points):
        y = 1 - 2 * (i + 0.5) / max(1, points)
        r = math.sqrt(1 - y * y)
        a = 2.39996 * i
        pts.append({"x": 0.3 * r * math.cos(a), "y": 0.3 * y, "z": 0.3 * r * math.sin(a),
                    "r": 200, "g": 120, "b": 40, "seen": 3})
    (root / "points.json").write_text(json.dumps({"points": pts}))
    (root / "meta.json").write_text(json.dumps({
        "format": "splat-capture", "version": 1, "app": "0.1.0", "device": "iPhone15,2", "system": "17.5",
        "started": "2026-10-14T13:30:12Z", "width": w, "height": h, "fps": 30, "frames": frames,
        "duration": (frames - 1) / 30, "points": points, "lidar": False, **(meta or {})}))
    if not zip_it:
        return root
    import zipfile
    zpath = folder / f"{name}.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        for p in sorted(root.iterdir()):
            zf.write(p, f"{name}/{p.name}")
    shutil.rmtree(root)
    return zpath


def gray_level(jpg: Path) -> int:
    """Brightness of a flat grey image (0-255): its stored luma, read with ffmpeg without any
    colour conversion (a conversion can stretch the range a second time)."""
    r = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(jpg), "-vf", "crop=2:2",
                        "-f", "rawvideo", "-pix_fmt", "yuvj420p", "-"], capture_output=True, check=True)
    return r.stdout[0]
