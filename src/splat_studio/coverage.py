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


def _cams(images: list[dict]) -> list[dict]:
    cams = []
    for im in images:
        R, t = im["R"], im["t"]
        centre = tuple(-(R[0][i] * t[0] + R[1][i] * t[1] + R[2][i] * t[2]) for i in range(3))
        cams.append({"name": im["name"], "center": centre, "dir": tuple(R[2]), "up": tuple(-c for c in R[1]),
                     "R": R, "camera": im.get("camera")})
    return cams


def frame_basis(cams: list[dict]) -> tuple | None:
    """(target, up, e1, e2): what the cameras look at, the average up, and two axes across it."""
    target = look_at_point(cams)
    if target is None:
        return None
    up = _norm(tuple(sum(c["up"][i] for c in cams) for i in range(3)))
    seed = (1.0, 0.0, 0.0) if abs(up[0]) < 0.9 else (0.0, 1.0, 0.0)
    e1 = _norm(_cross(up, seed))
    return target, up, e1, _cross(up, e1)


def measure(images: list[dict], target=None, basis=None, only: set | None = None) -> dict | None:
    """Coverage from COLMAP images (R, t, name). `target`: measure around this point instead of
    the point the cameras look at; `basis`: reuse (up, e1, e2) so several rings line up; `only`:
    count just these frames (where an object is visible). None with fewer than 3 cameras."""
    all_cams = _cams(images)
    if len(all_cams) < 3:
        return None
    b = frame_basis(all_cams)
    if b is None:
        return None
    up, e1, e2 = basis or b[1:]
    target = tuple(target) if target is not None else b[0]
    cams = [c for c in all_cams if only is None or c["name"] in only]
    if len(cams) < 3:
        return None
    rows = []
    for c in cams:
        v = tuple(c["center"][i] - target[i] for i in range(3))
        h = _dot(v, up)
        flat = tuple(v[i] - h * up[i] for i in range(3))
        az = math.degrees(math.atan2(_dot(flat, e2), _dot(flat, e1))) % 360
        el = math.degrees(math.atan2(h, math.sqrt(_dot(flat, flat)) or 1e-9))
        n = math.sqrt(_dot(v, v)) or 1.0
        rows.append((az, el, c["name"], tuple(round(x / n, 3) for x in v)))
    rows.sort()
    # the largest empty arc, wrapping around
    gap, start, end = -1.0, None, None
    for i, (az, _el, name, _d) in enumerate(rows):
        nxt = rows[(i + 1) % len(rows)]
        d = (nxt[0] - az) % 360 or (360.0 if len(rows) == 1 else 0.0)
        if d > gap:
            gap, start, end = d, (az, name), (nxt[0], nxt[2])
    els = sorted(r[1] for r in rows)
    q = lambda p: els[min(len(els) - 1, max(0, round(p * (len(els) - 1))))]
    r6 = lambda v: [round(x, 6) for x in v]
    return {"cameras": len(rows),
            "azimuths": [round(r[0], 1) for r in rows], "elevations": [round(r[1], 1) for r in rows],
            "gap": {"degrees": round(gap, 1), "from": round(start[0], 1), "to": round(end[0], 1),
                    "first": start[1], "last": end[1]},
            "height": {"low": round(q(0.1), 1), "high": round(q(0.9), 1), "spread": round(q(0.9) - q(0.1), 1)},
            "basis": {"target": r6(target), "up": r6(up), "e1": r6(e1), "e2": r6(e2)},
            # each camera's direction from the centre, in the model's own coordinates (the viewer's globe)
            "dirs": [list(r[3]) for r in rows]}


# ---------------------------------------------------------------- one ring per object

def _mask_stats(path: Path):
    """(share of the frame, centre x, centre y as fractions) of a mask, or None."""
    from .objects import read_mask
    try:
        w, h, px = read_mask(path, 96)
    except (RuntimeError, OSError):
        return None
    n = sx = sy = 0
    for i, v in enumerate(px):
        if v > 127:
            n += 1
            sx += i % w
            sy += i // w
    return (n / (w * h), (sx / n + 0.5) / w, (sy / n + 0.5) / h) if n else (0.0, 0.5, 0.5)


def per_object(run_dir: Path, frames_dir: Path, basis: dict, samples: int = 90) -> list[dict]:
    """Coverage of each chosen object: around its own centre (where the rays through its mask
    centres meet), counting only the frames where it's visible (its mask covers at least 0.2%
    of the frame). [] without several objects or without the distorted camera model."""
    from .masks import load_objects, masks_dir
    from .objects import read_cameras_bin, read_images_bin
    objs = [o for o in load_objects(frames_dir) if (masks_dir(frames_dir) / "objects" / str(o["id"])).is_dir()]
    if len(objs) < 2:
        return []
    try:
        model = Path((Path(run_dir) / "dense" / "source_model.txt").read_text().strip())
        cams_in = read_cameras_bin(model / "cameras.bin")
        images = read_images_bin(model / "images.bin")
    except (OSError, ValueError, IndexError):
        return []
    images = sorted((i for i in images if i["camera"] in cams_in), key=lambda i: i["name"])
    step = max(1, len(images) // samples)
    picked = images[::step]
    cams = _cams(images)
    up, e1, e2 = (tuple(basis[k]) for k in ("up", "e1", "e2"))
    out = []
    for o in objs:
        d = masks_dir(frames_dir) / "objects" / str(o["id"])
        seen, rays = set(), []
        for im in picked:
            st = _mask_stats(d / (Path(im["name"]).stem + ".png"))
            if not st or st[0] < 0.002:
                continue
            seen.add(im["name"])
            cam = cams_in[im["camera"]]
            k = cam["params"]
            one_f = cam["model"].startswith("SIMPLE") or cam["model"] in ("RADIAL", "RADIAL_FISHEYE", "FOV")
            fx, fy, cx, cy = (k[0], k[0], k[1], k[2]) if one_f else k[:4]
            x, y = (st[1] * cam["width"] - cx) / fx, (st[2] * cam["height"] - cy) / fy
            R = im["R"]
            ray = _norm(tuple(R[0][i] * x + R[1][i] * y + R[2][i] for i in range(3)))   # R^T (x, y, 1)
            c = next(c for c in cams if c["name"] == im["name"])
            rays.append({"center": c["center"], "dir": ray})
        centre = look_at_point(rays) if len(rays) >= 3 else None
        cov = measure(images, target=centre, basis=(up, e1, e2), only=seen) if centre else None
        if cov:
            cov["sampled"] = len(picked)
            out.append({"id": o["id"], "name": o["name"], **cov})
    return out


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


def for_run(run_dir: Path, frames_dir: Path | None = None) -> dict | None:
    """Coverage of a run; with the frames folder of a several-objects selection, one ring per object too."""
    for p in (Path(run_dir) / "dense" / "sparse" / "0" / "images.bin", Path(run_dir) / "sparse" / "0" / "images.bin"):
        if p.exists() and p.stat().st_size > 8:
            try:
                cov = measure(read_images_bin(p))
            except (ValueError, IndexError, OSError):
                return None
            if cov and frames_dir is not None:
                objs = per_object(run_dir, frames_dir, cov["basis"])
                if objs:
                    cov["objects"] = objs
            return cov
    return None


def has_poses(run_dir: Path) -> bool:
    """Whether COLMAP placed cameras for this run (there's something to measure)."""
    return any(p.exists() and p.stat().st_size > 8
               for p in (Path(run_dir) / "dense" / "sparse" / "0" / "images.bin", Path(run_dir) / "sparse" / "0" / "images.bin"))


def saved(run_dir: Path) -> dict | None:
    """coverage.json of a run if it's up to date (has the basis and camera directions), else None."""
    try:
        cov = json.loads((Path(run_dir) / "coverage.json").read_text())
    except (OSError, ValueError):
        return None
    return cov if isinstance(cov, dict) and "basis" in cov and "dirs" in cov else None


def load(run_dir: Path, frames_dir: Path | None = None) -> dict | None:
    """coverage.json of a run, measured now (and saved) for runs made before it existed or before
    it had a basis. Measuring can take a while with several objects (their masks are read)."""
    cov = saved(run_dir)
    if cov:
        return cov
    f = Path(run_dir) / "coverage.json"
    cov = for_run(run_dir, frames_dir)
    if cov:
        try:
            f.write_text(json.dumps(cov))
        except OSError:
            pass
    return cov


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
