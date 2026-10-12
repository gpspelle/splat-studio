"""Where the cameras were around the object, measured right after COLMAP placed them.

Missing angles show up only after a long training run (holes, blur on one side), so we check
them before training starts:

* angle around the object: the largest gap between neighbouring cameras, seen from above;
* height: how far apart the lowest and highest cameras are, as an angle seen from the object.

"Up" is the average of the cameras' own up directions (people hold the phone upright), and the
object is the point every camera looks at most closely. Standard library only.

    splat-studio coverage workspace/<video>/run_<date_time>
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

from .objects import read_images_bin
from .splats import look_at_point

GAP_WARN = 60.0          # degrees around the object with no camera
HEIGHT_WARN = 15.0       # degrees between the low and high cameras


def _norm(v):
    n = math.sqrt(sum(c * c for c in v)) or 1.0
    return tuple(c / n for c in v)


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def measure(images: list[dict]) -> dict | None:
    """Coverage from COLMAP images (R, t, name). None with fewer than 3 cameras."""
    if len(images) < 3:
        return None
    cams = []
    for im in images:
        R, t = im["R"], im["t"]
        centre = tuple(-(R[0][i] * t[0] + R[1][i] * t[1] + R[2][i] * t[2]) for i in range(3))
        cams.append({"name": im["name"], "center": centre, "dir": tuple(R[2]), "up": tuple(-c for c in R[1])})
    target = look_at_point(cams)
    if target is None:
        return None
    up = _norm(tuple(sum(c["up"][i] for c in cams) for i in range(3)))
    seed = (1.0, 0.0, 0.0) if abs(up[0]) < 0.9 else (0.0, 1.0, 0.0)
    e1 = _norm(_cross(up, seed))
    e2 = _cross(up, e1)
    rows = []
    for c in cams:
        v = tuple(c["center"][i] - target[i] for i in range(3))
        h = _dot(v, up)
        flat = tuple(v[i] - h * up[i] for i in range(3))
        az = math.degrees(math.atan2(_dot(flat, e2), _dot(flat, e1))) % 360
        el = math.degrees(math.atan2(h, math.sqrt(_dot(flat, flat)) or 1e-9))
        rows.append((az, el, c["name"]))
    rows.sort()
    # the largest empty arc, wrapping around
    gap, start, end = -1.0, None, None
    for i, (az, _el, name) in enumerate(rows):
        nxt = rows[(i + 1) % len(rows)]
        d = (nxt[0] - az) % 360 or (360.0 if len(rows) == 1 else 0.0)
        if d > gap:
            gap, start, end = d, (az, name), (nxt[0], nxt[2])
    els = sorted(r[1] for r in rows)
    q = lambda p: els[min(len(els) - 1, max(0, round(p * (len(els) - 1))))]
    return {"cameras": len(rows),
            "azimuths": [round(r[0], 1) for r in rows], "elevations": [round(r[1], 1) for r in rows],
            "gap": {"degrees": round(gap, 1), "from": round(start[0], 1), "to": round(end[0], 1),
                    "first": start[1], "last": end[1]},
            "height": {"low": round(q(0.1), 1), "high": round(q(0.9), 1), "spread": round(q(0.9) - q(0.1), 1)}}


def problems(cov: dict | None) -> list[str]:
    """'gap' and/or 'height' when coverage is likely to leave holes or blur."""
    if not cov:
        return []
    out = []
    if cov["gap"]["degrees"] > GAP_WARN:
        out.append("gap")
    if cov["height"]["spread"] < HEIGHT_WARN:
        out.append("height")
    return out


def for_run(run_dir: Path) -> dict | None:
    for p in (Path(run_dir) / "dense" / "sparse" / "0" / "images.bin", Path(run_dir) / "sparse" / "0" / "images.bin"):
        if p.exists() and p.stat().st_size > 8:
            try:
                return measure(read_images_bin(p))
            except (ValueError, IndexError, OSError):
                return None
    return None


def summary_line(cov: dict) -> str:
    g, h = cov["gap"], cov["height"]
    return (f"{cov['cameras']} cameras, largest gap {g['degrees']:.0f} degrees around the object, "
            f"heights {h['low']:.0f} to {h['high']:.0f} degrees")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="splat-studio coverage",
                                 description="Where the cameras were around the object (after COLMAP).")
    ap.add_argument("run", type=Path, help="a run folder, e.g. workspace/vase/latest")
    a = ap.parse_args(argv)
    cov = for_run(a.run)
    if not cov:
        sys.exit("no camera poses in that run")
    print(summary_line(cov))
    for p in problems(cov):
        print("  warning:", {"gap": "one side of the object was barely filmed",
                             "height": "the cameras were all at about the same height"}[p])
    print(json.dumps(cov))


if __name__ == "__main__":
    main()
