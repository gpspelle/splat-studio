"""Shared test helpers: temp data folders, fake tools on PATH, sample videos and splats."""
from __future__ import annotations

import contextlib
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

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = unittest.skipUnless(HAS_FFMPEG, "ffmpeg/ffprobe not installed")


class TempDirTestCase(unittest.TestCase):
    """Gives each test a fresh temp folder (self.tmp) and isolates saved speeds and Brush lookups."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="splat-test-")
        self.tmp = Path(self._td.name).resolve()
        self._env = mock_env({
            "SPLAT_STUDIO_RATES": str(self.tmp / "rates.json"),
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
