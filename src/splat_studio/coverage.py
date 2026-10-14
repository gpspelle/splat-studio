"""Where the cameras were around the object, measured right after COLMAP placed them.

Missing angles show up only after a long training run (holes, blur on one side), so we check
them before training starts:

* angle around the object: the largest gap between neighbouring cameras, seen from above;
* height: how far apart the lowest and highest cameras are, as an angle seen from the object.

"Up" is the average of the cameras' own up directions (people hold the phone upright), and the
object is the point every camera looks at most closely. Standard library only.

Some sides can't be filmed: the object stands against a wall, in a corner, next to another car.
COLMAP's own 3D points show that: something standing where a person filming at the usual
distance would have to be (a wall, a cupboard, another object) blocks that direction, so it
isn't counted as a missing side. The underside of something standing on a surface is never
measured at all (only angles around the object are).

    splat-studio coverage workspace/<video>/run_<date_time>
"""
from __future__ import annotations

import argparse
import json
import math
import struct
import sys
from pathlib import Path

from .objects import read_images_bin
from .splats import look_at_point

GAP_WARN = 60.0          # degrees around the object with no camera
HEIGHT_WARN = 15.0       # degrees between the low and high cameras
# A direction is blocked when something stands where a person filming would be: between these
# multiples of the usual camera distance, reaching at least BLOCK_TALL x that distance up.
BLOCK_NEAR, BLOCK_FAR, BLOCK_TALL = 0.6, 1.3, 0.25
BLOCK_BIN = 10           # degrees per direction tested


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
    flat = sorted(math.sqrt(max(0.0, _dot(v, v) - _dot(v, up) ** 2))
                  for v in (tuple(c["center"][i] - target[i] for i in range(3)) for c in cams))
    els = sorted(r[1] for r in rows)
    q = lambda p: els[min(len(els) - 1, max(0, round(p * (len(els) - 1))))]
    r6 = lambda v: [round(x, 6) for x in v]
    return {"cameras": len(rows),
            "azimuths": [round(r[0], 1) for r in rows], "elevations": [round(r[1], 1) for r in rows],
            "gap": {"degrees": round(gap, 1), "from": round(start[0], 1), "to": round(end[0], 1),
                    "first": start[1], "last": end[1]},
            "height": {"low": round(q(0.1), 1), "high": round(q(0.9), 1), "spread": round(q(0.9) - q(0.1), 1)},
            # how far from the object people stood (median, along the ground): where a camera would be
            "distance": round(flat[len(flat) // 2], 6),
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


# ---------------------------------------------------------------- sides nobody could film

def read_points3d(path: Path) -> list[tuple]:
    """xyz of every point in a COLMAP points3D.bin."""
    data = Path(path).read_bytes()
    if len(data) < 8:
        return []
    n, off, out = struct.unpack_from("<Q", data, 0)[0], 8, []
    for _ in range(n):
        x, y, z = struct.unpack_from("<3d", data, off + 8)
        off += 43                                        # id, xyz, rgb, error
        tl = struct.unpack_from("<Q", data, off)[0]
        off += 8 + 8 * tl                                # track: (image id, point index) pairs
        out.append((x, y, z))
    return out


def _largest_gap(az: list[float]) -> tuple[float, float, float]:
    az = sorted(a % 360 for a in az)
    best = (-1.0, 0.0, 0.0)
    for i, a in enumerate(az):
        b = az[(i + 1) % len(az)]
        d = (b - a) % 360 or (360.0 if len(az) == 1 else 0.0)
        if d > best[0]:
            best = (d, a, b)
    return best


def excuse_blocked(cov: dict, points: list[tuple]) -> dict:
    """Add 'blocked' (arcs [from, to] in degrees that nobody could have filmed from, because
    something stands there) and 'gap_open' (the largest gap left once those are excused) to a
    coverage dict. Floors and table tops never block: they're flat, and blocking needs height."""
    b, dist = cov.get("basis"), cov.get("distance")
    if not b or not dist or not points:
        return cov
    t, up, e1, e2 = (tuple(b[k]) for k in ("target", "up", "e1", "e2"))
    nb = 360 // BLOCK_BIN
    heights: list[list[float]] = [[] for _ in range(nb)]
    for p in points:
        v = (p[0] - t[0], p[1] - t[1], p[2] - t[2])
        h, x, y = _dot(v, up), _dot(v, e1), _dot(v, e2)
        d = math.hypot(x, y)
        if BLOCK_NEAR * dist <= d <= BLOCK_FAR * dist and -dist <= h <= dist:
            heights[int(math.degrees(math.atan2(y, x)) % 360 // BLOCK_BIN) % nb].append(h)
    need = max(12, len(points) // 1000)
    az = cov["azimuths"]
    empty = lambda c: all(min(abs(c - a) % 360, 360 - abs(c - a) % 360) > BLOCK_BIN for a in az)
    blocked = []
    for i, hs in enumerate(heights):
        if len(hs) >= need:
            hs.sort()
            tall = hs[int(0.9 * (len(hs) - 1))] - hs[int(0.1 * (len(hs) - 1))]
            if tall >= BLOCK_TALL * dist and empty(i * BLOCK_BIN + BLOCK_BIN / 2):
                blocked.append(i)
    arcs: list[list[float]] = []
    for i in blocked:                                   # neighbouring directions -> one arc
        if arcs and arcs[-1][1] == i * BLOCK_BIN:
            arcs[-1][1] = (i + 1) * BLOCK_BIN
        else:
            arcs.append([i * BLOCK_BIN, (i + 1) * BLOCK_BIN])
    if len(arcs) > 1 and arcs[0][0] == 0 and arcs[-1][1] == 360:
        arcs[0][0] = arcs.pop()[0] - 360
    cov["blocked"] = [[a % 360, z % 360 or 360.0] for a, z in arcs]
    excused = [i * BLOCK_BIN + k for i in blocked for k in (0, 2.5, 5, 7.5, 10)]
    d, a, z = _largest_gap(list(az) + excused)
    g = cov["gap"]
    same = abs(a - g["from"]) < 0.05 and abs(z - g["to"]) < 0.05
    cov["gap_open"] = {"degrees": round(d, 1), "from": round(a, 1), "to": round(z, 1),
                       "first": g["first"] if same else None, "last": g["last"] if same else None}
    return cov


def open_gap(cov: dict) -> dict:
    """The largest side nobody filmed that somebody could have: the gap once blocked sides are excused."""
    return cov.get("gap_open") or cov["gap"]


def blocked_degrees(cov: dict) -> float:
    return sum((z - a) % 360 or 360.0 for a, z in cov.get("blocked") or [])


def problems(cov: dict | None) -> list[str]:
    """'gap' and/or 'height' when coverage is likely to leave holes or blur. A side that something
    stands in front of (a wall) isn't a gap."""
    if not cov:
        return []
    out = []
    if open_gap(cov)["degrees"] > GAP_WARN:
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
            if cov:
                try:
                    pts = read_points3d(p.with_name("points3D.bin"))
                except (OSError, struct.error):
                    pts = []
                for c in [cov] + cov.get("objects", []):
                    excuse_blocked(c, pts)
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
    blocked = blocked_degrees(cov)
    return (f"{cov['cameras']} cameras, largest gap {g['degrees']:.0f} degrees around the object"
            + (f" ({blocked:.0f} degrees of it blocked, open gap {open_gap(cov)['degrees']:.0f})" if blocked else "")
            + f", heights {h['low']:.0f} to {h['high']:.0f} degrees")


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
