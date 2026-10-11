"""Small helpers shared by every stage."""
from __future__ import annotations

import os
import sys
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
# The folder that contains the `splat_studio` package. Added to PYTHONPATH for child
# processes, so `python -m splat_studio.<stage>` works without installing the package.
PACKAGE_PARENT = PACKAGE_DIR.parent

# Where Brush is cloned and built when it isn't found anywhere else.
TOOLS_DIR = Path(os.environ.get("SPLAT_STUDIO_TOOLS", Path.home() / ".splat-studio"))


def say(msg: str = "") -> None:
    print(msg, flush=True)


def fmt_time(seconds: float | None) -> str:
    """12 -> '12s', 754 -> '12m34s', 4000 -> '1h06m', None -> '?'."""
    if seconds is None:
        return "?"
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def to_seconds(ts: str) -> float:
    """'1:05' -> 65.0, '0:01:05.5' -> 65.5, '12' -> 12.0."""
    secs = 0.0
    for part in str(ts).split(":"):
        secs = secs * 60 + float(part)
    return secs


def fmt_timestamp(seconds: float) -> str:
    """65.4 -> '1:05' (for pointing at moments in a video)."""
    s = max(0, int(round(seconds)))
    return f"{s // 60}:{s % 60:02d}"


def child_env(extra: dict | None = None) -> dict:
    """Environment for child processes that run `python -m splat_studio.*`."""
    env = dict(os.environ)
    paths = [str(PACKAGE_PARENT)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    env["PYTHONPATH"] = os.pathsep.join(paths)
    env.setdefault("PYTHONUNBUFFERED", "1")
    if extra:
        env.update(extra)
    return env


def module_cmd(module: str, *args) -> list[str]:
    """Command line to run one of the package's stages as a child process."""
    return [sys.executable, "-u", "-m", f"splat_studio.{module}", *map(str, args)]
