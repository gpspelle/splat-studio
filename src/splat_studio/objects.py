"""One splat per object, from a splat trained on several objects at once.

    splat-studio split-objects NAME.ply --run workspace/<video>/run_<date_time>

The run was trained with "Choose what to keep" and several objects, so the splat holds all of
them (everything else is transparent), and frames/<video>/masks/objects/<id>/ holds each
object's mask in every frame. Every splat is given to the object whose mask it falls into in
most of the frames that see it:

1. Splats are grouped on a fine voxel grid (so a million splats need only a few thousand
   projections per view).
2. Each voxel's centre is projected into ~48 evenly spread frames with COLMAP's camera poses
   and lens model (the original, distorted one: the masks are of the original frames).
3. A voxel belongs to an object if it lands inside that object's mask in at least half of the
   frames where it's in view. Seen from other angles, an object hidden behind another one
   still wins most of its votes, so occlusion doesn't cut it apart.

Writes NAME_part-<name>.ply next to the input for each object (the input is only read).
Standard library only; masks are read through ffmpeg, scaled down to MASK_WIDTH pixels.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import struct
import subprocess
import sys
from pathlib import Path

from .splats import PlyError, PlyFile, _write_rows, qvec_to_rotmat, sigmoid

VIEWS = 48
MASK_WIDTH = 320
VOXELS_ACROSS = 120
MIN_OPACITY = 0.02
# COLMAP camera models: id -> (name, number of parameters)
CAMERA_MODELS = {0: ("SIMPLE_PINHOLE", 3), 1: ("PINHOLE", 4), 2: ("SIMPLE_RADIAL", 4), 3: ("RADIAL", 5),
                 4: ("OPENCV", 8), 5: ("OPENCV_FISHEYE", 8), 6: ("FULL_OPENCV", 12), 7: ("FOV", 5),
                 8: ("SIMPLE_RADIAL_FISHEYE", 4), 9: ("RADIAL_FISHEYE", 5), 10: ("THIN_PRISM_FISHEYE", 12)}


# ---------------------------------------------------------------- COLMAP model

def read_cameras_bin(path: Path) -> dict[int, dict]:
    data = Path(path).read_bytes()
    n, = struct.unpack_from("<Q", data, 0)
    o, cams = 8, {}
    for _ in range(n):
        cam_id, model, w, h = struct.unpack_from("<iiQQ", data, o)
        o += 24
        name, k = CAMERA_MODELS.get(model, (None, 0))
        if name is None:
            raise ValueError(f"unknown COLMAP camera model {model}")
        params = struct.unpack_from(f"<{k}d", data, o)
        o += 8 * k
        cams[cam_id] = {"model": name, "width": w, "height": h, "params": params}
    return cams


def read_images_bin(path: Path) -> list[dict]:
    data = Path(path).read_bytes()
    n, = struct.unpack_from("<Q", data, 0)
    o, out = 8, []
    for _ in range(n):
        _iid, qw, qx, qy, qz, tx, ty, tz, cam_id = struct.unpack_from("<I7dI", data, o)
        o += struct.calcsize("<I7dI")
        end = data.index(b"\0", o)
        name = data[o:end].decode("utf-8", "replace")
        o = end + 1
        npts, = struct.unpack_from("<Q", data, o)
        o += 8 + npts * 24
        out.append({"name": name, "R": qvec_to_rotmat((qw, qx, qy, qz)), "t": (tx, ty, tz), "camera": cam_id})
    return out


def project(cam: dict, img: dict, p) -> tuple[float, float] | None:
    """Pixel position of world point p in this image (None if behind the camera)."""
    R, t = img["R"], img["t"]
    x = R[0][0] * p[0] + R[0][1] * p[1] + R[0][2] * p[2] + t[0]
    y = R[1][0] * p[0] + R[1][1] * p[1] + R[1][2] * p[2] + t[1]
    z = R[2][0] * p[0] + R[2][1] * p[1] + R[2][2] * p[2] + t[2]
    if z <= 1e-9:
        return None
    u, v = x / z, y / z
    m, k = cam["model"], cam["params"]
    if m in ("SIMPLE_PINHOLE", "SIMPLE_RADIAL", "RADIAL", "SIMPLE_RADIAL_FISHEYE", "RADIAL_FISHEYE", "FOV"):
        fx = fy = k[0]
        cx, cy = k[1], k[2]
        k1 = k[3] if len(k) > 3 and m != "FOV" else 0.0
        k2 = k[4] if len(k) > 4 and m in ("RADIAL", "RADIAL_FISHEYE") else 0.0
        p1 = p2 = 0.0
    else:                                              # PINHOLE, OPENCV, FULL_OPENCV, fisheyes (approx.)
        fx, fy, cx, cy = k[:4]
        k1, k2, p1, p2 = (list(k[4:8]) + [0.0] * 4)[:4]
        if m == "OPENCV_FISHEYE":
            p1 = p2 = 0.0
    if k1 or k2 or p1 or p2:
        r2 = u * u + v * v
        radial = k1 * r2 + k2 * r2 * r2
        du = u * radial + 2 * p1 * u * v + p2 * (r2 + 2 * u * u)
        dv = v * radial + 2 * p2 * u * v + p1 * (r2 + 2 * v * v)
        u, v = u + du, v + dv
    return fx * u + cx, fy * v + cy


# ---------------------------------------------------------------- masks

def read_mask(path: Path, width: int = MASK_WIDTH) -> tuple[int, int, bytes]:
    """A mask scaled to `width` pixels wide, as raw grey bytes (via ffmpeg)."""
    r = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(path), "-vf",
                        f"scale={width}:-2:flags=neighbor,format=gray", "-f", "rawvideo", "-"],
                       capture_output=True)
    if r.returncode != 0 or not r.stdout or len(r.stdout) % width:
        raise RuntimeError(f"couldn't read the mask {path.name}: {r.stderr.decode(errors='replace')[-300:]}")
    return width, len(r.stdout) // width, r.stdout


def pick_views(images: list[dict], n: int = VIEWS) -> list[dict]:
    images = sorted(images, key=lambda i: i["name"])
    if len(images) <= n:
        return images
    step = len(images) / n
    return [images[int(i * step)] for i in range(n)]


# ---------------------------------------------------------------- the split

def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s[:32] or "object"


def part_path(ply: Path, name: str, taken: set) -> Path:
    base, k = slug(name), 1
    s = base
    while s in taken:
        k += 1
        s = f"{base}-{k}"
    taken.add(s)
    return ply.with_name(f"{ply.stem}_part-{s}{ply.suffix}")


def assign(points, cams: dict, views: list[dict], masks: dict[int, dict[str, tuple]], min_share: float = 0.5,
           min_views: int = 3) -> list[int]:
    """Object id (0 = none) for every point, by majority over the views that see it."""
    ids = sorted(masks)
    out = []
    for p in points:
        seen, votes = 0, dict.fromkeys(ids, 0)
        for img in views:
            cam = cams[img["camera"]]
            uv = project(cam, img, p)
            if uv is None:
                continue
            u, v = uv
            if not (0 <= u < cam["width"] and 0 <= v < cam["height"]):
                continue
            seen += 1
            for oid in ids:
                m = masks[oid].get(img["name"])
                if m is None:
                    continue
                mw, mh, px = m
                mx, my = int(u * mw / cam["width"]), int(v * mh / cam["height"])
                if px[min(my, mh - 1) * mw + min(mx, mw - 1)] > 127:
                    votes[oid] += 1
        best = max(ids, key=lambda i: votes[i]) if ids else 0
        out.append(best if seen >= min_views and votes.get(best, 0) >= min_share * seen else 0)
    return out


def split(path: Path, run_dir: Path, frames_dir: Path, objects: list[dict] | None = None,
          views: int = VIEWS, log=print) -> list[dict]:
    """Write one splat per object; returns [{id, name, output, kept}] (objects with no splats are skipped)."""
    from . import masks as masksmod
    path, run_dir, frames_dir = Path(path), Path(run_dir), Path(frames_dir)
    objects = objects or masksmod.load_objects(frames_dir)
    obj_root = masksmod.masks_dir(frames_dir) / "objects"
    objects = [o for o in objects if (obj_root / str(o["id"])).is_dir()]
    if len(objects) < 2:
        raise ValueError("this selection has fewer than two objects")
    try:
        model = Path((run_dir / "dense" / "source_model.txt").read_text().strip())
    except OSError:
        raise ValueError("this run has no record of its camera model (run COLMAP again)") from None
    cams = read_cameras_bin(model / "cameras.bin")
    images = [i for i in read_images_bin(model / "images.bin") if i["camera"] in cams]
    chosen = pick_views(images, views)
    if len(chosen) < 3:
        raise ValueError("too few camera poses to tell the objects apart")
    masks: dict[int, dict[str, tuple]] = {}
    for o in objects:
        masks[o["id"]] = {}
        for img in chosen:
            f = obj_root / str(o["id"]) / (Path(img["name"]).stem + ".png")
            if f.exists():
                masks[o["id"]][img["name"]] = read_mask(f)

    ply = PlyFile(path)
    f = ply.read(["x", "y", "z", "opacity"])
    n = ply.count
    opaque = [i for i in range(n) if sigmoid(f["opacity"][i]) >= MIN_OPACITY]
    if not opaque:
        raise ValueError("the splat has no visible splats")
    # 1. voxels of the visible splats
    xs = sorted(f["x"][i] for i in opaque)
    ys = sorted(f["y"][i] for i in opaque)
    zs = sorted(f["z"][i] for i in opaque)
    q = lambda a, s: a[min(len(a) - 1, int(s * (len(a) - 1)))]
    extent = max(q(xs, .98) - q(xs, .02), q(ys, .98) - q(ys, .02), q(zs, .98) - q(zs, .02), 1e-6)
    size = extent / VOXELS_ACROSS
    vox: dict[tuple, list] = {}
    for i in opaque:
        key = (math.floor(f["x"][i] / size), math.floor(f["y"][i] / size), math.floor(f["z"][i] / size))
        vox.setdefault(key, []).append(i)
    keys = list(vox)
    centres = []
    for key in keys:
        idx = vox[key]
        centres.append((sum(f["x"][i] for i in idx) / len(idx), sum(f["y"][i] for i in idx) / len(idx),
                        sum(f["z"][i] for i in idx) / len(idx)))
    log(f"    {len(opaque):,} visible splats in {len(keys):,} voxels, {len(chosen)} views, {len(objects)} objects")
    # 2 + 3. votes
    labels = assign(centres, cams, chosen, masks)
    per_obj: dict[int, bytearray] = {o["id"]: bytearray(n) for o in objects}
    counts = dict.fromkeys(per_obj, 0)
    for key, lab in zip(keys, labels):
        if lab:
            for i in vox[key]:
                per_obj[lab][i] = 1
            counts[lab] += len(vox[key])
    results, taken = [], set()
    for o in objects:
        if not counts[o["id"]]:
            log(f"    {o['name']}: no splats found - skipped")
            continue
        out = part_path(path, o["name"], taken)
        header = [f"element vertex {counts[o['id']]}" if l.startswith("element vertex") else l for l in ply.header_lines]
        header.insert(2, f"comment Object '{o['name']}' split out by Splat Studio: kept {counts[o['id']]} of {n} splats")
        _write_rows(path, ply, out, header, per_obj[o["id"]])
        results.append({"id": o["id"], "name": o["name"], "output": str(out), "kept": counts[o["id"]]})
    return results


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="splat-studio split-objects",
                                 description="One splat per object, from a run trained with several objects "
                                             "chosen in 'Choose what to keep'. Writes NAME_part-<object>.ply.")
    ap.add_argument("ply", type=Path)
    ap.add_argument("--run", type=Path, required=True, help="the run folder it was trained from")
    ap.add_argument("--frames", type=Path, help="the frames folder with the masks (default: frames/<video>)")
    a = ap.parse_args(argv)
    frames = a.frames or Path("frames") / a.run.resolve().parent.name
    try:
        for r in split(a.ply, a.run, frames):
            print(f"Part copy ({r['name']}): {r['output']}")
    except (OSError, ValueError, RuntimeError, PlyError) as e:
        sys.exit(f"Couldn't split the objects: {e}")


if __name__ == "__main__":
    main()
