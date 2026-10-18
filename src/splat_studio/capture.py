"""iPhone captures from Splat Capture: frames and camera poses without running COLMAP.

The app (github.com/gpspelle/splat-capture) records the camera image together with where ARKit
says the phone was for every frame, and saves one zip:

  capture_<date>/video.mov      the sensor's landscape image, no rotation tag
  capture_<date>/frames.jsonl   one line per video frame: camera-to-world pose (row-major,
                                ARKit axes), [fx, fy, cx, cy], tracking state
  capture_<date>/points.json    3D points ARKit tracked, with colours
  capture_<date>/meta.json      device, size, frame rate, format version

This module turns one into what the rest of the pipeline expects from COLMAP: frames in
frames/<name>/, and a run folder with images/, sparse/0/ and dense/ (images + sparse/0) holding a
PINHOLE camera, the poses and the points. Frames where tracking was limited are left out, and the
frames are turned upright using gravity (ARKit's world y is up).

Poses can come from three places (`--poses`):
  arkit    ARKit's poses and points as they are (seconds)
  refine   ARKit's poses, plus COLMAP features, matching, triangulation and bundle adjustment with
           the lens fixed: better points, poses polished (minutes)
  colmap   ignore ARKit and run COLMAP on the frames as for a video

The package format is described in docs/capture-format.md of the app's repository.
"""
from __future__ import annotations

import json
import math
import shutil
import sqlite3
import struct
import subprocess
import zipfile
from pathlib import Path

from .common import say

FORMAT = "splat-capture"
VERSION = 1
POSES = ("arkit", "refine", "colmap")
MIN_GOOD_SHARE = 0.3        # below this share of well-tracked frames, use every frame anyway
MIN_POINTS = 200            # fewer ARKit points than this: add a few to start training from
PINHOLE = 1                 # COLMAP camera model id


class CaptureError(Exception):
    pass


# ---------------------------------------------------------------- finding and reading a package

def find_root(folder: Path) -> Path | None:
    """The folder holding frames.jsonl: `folder` itself or one level down."""
    folder = Path(folder)
    if (folder / "frames.jsonl").is_file():
        return folder
    if folder.is_dir():
        for d in sorted(p for p in folder.iterdir() if p.is_dir()):
            if (d / "frames.jsonl").is_file():
                return d
    return None


def _zip_root(zf: zipfile.ZipFile) -> str | None:
    """Folder prefix of frames.jsonl inside the zip ('' at the top level), or None."""
    for n in zf.namelist():
        parts = n.split("/")
        if parts[-1] == "frames.jsonl" and len(parts) <= 2 and not n.startswith("__MACOSX"):
            return "/".join(parts[:-1])
    return None


def is_capture(path: Path) -> bool:
    path = Path(path)
    if path.is_dir():
        return find_root(path) is not None
    if path.suffix.lower() == ".zip" and path.is_file():
        try:
            with zipfile.ZipFile(path) as zf:
                return _zip_root(zf) is not None
        except (zipfile.BadZipFile, OSError):
            return False
    return False


def unpack(path: Path, dest_parent: Path) -> Path:
    """The package folder: a folder is used where it is, a zip is extracted once to
    dest_parent/<zip name>/ (and reused if it's already there)."""
    path = Path(path)
    if path.is_dir():
        root = find_root(path)
        if root is None:
            raise CaptureError(f"{path} has no frames.jsonl: not a Splat Capture package")
        return root
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise CaptureError(f"{path} isn't a readable zip: {e}") from None
    with zf:
        prefix = _zip_root(zf)
        if prefix is None:
            raise CaptureError(f"{path} has no frames.jsonl: not a Splat Capture package")
        dest = Path(dest_parent) / path.stem.replace(" ", "_")
        done = dest / ".unpacked"
        if done.exists() and done.read_text() == f"{path.stat().st_size} {path.stat().st_mtime_ns}":
            return find_root(dest)
        shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True)
        base = dest.resolve()
        for info in zf.infolist():
            if info.is_dir() or info.filename.startswith("__MACOSX"):
                continue
            target = (dest / info.filename).resolve()
            if base not in target.parents:                 # "../" in a name: never write outside dest
                raise CaptureError(f"{path}: unsafe file name in the zip: {info.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
        done.write_text(f"{path.stat().st_size} {path.stat().st_mtime_ns}")
    root = find_root(dest)
    if root is None:
        raise CaptureError(f"{path}: frames.jsonl went missing while unpacking")
    return root


def load(root: Path) -> dict:
    """meta, rows (one per video frame) and points of a package folder."""
    root = Path(root)
    try:
        meta = json.loads((root / "meta.json").read_text())
    except OSError:
        raise CaptureError(f"{root} has no meta.json") from None
    except ValueError as e:
        raise CaptureError(f"{root}/meta.json isn't valid JSON: {e}") from None
    if meta.get("format") != FORMAT:
        raise CaptureError(f"{root} isn't a Splat Capture package (format: {meta.get('format')!r})")
    if int(meta.get("version", 0)) > VERSION:
        raise CaptureError(f"{root} was saved by a newer Splat Capture (format version {meta['version']}); "
                           "update Splat Studio")
    rows = []
    for n, line in enumerate((root / "frames.jsonl").read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            r = json.loads(line)
            if len(r["pose"]) != 16 or len(r["K"]) != 4:
                raise ValueError("pose needs 16 numbers and K 4")
        except (ValueError, KeyError, TypeError) as e:
            raise CaptureError(f"frames.jsonl line {n}: {e}") from None
        rows.append(r)
    if not rows:
        raise CaptureError(f"{root}/frames.jsonl is empty")
    if not (root / "video.mov").is_file():
        raise CaptureError(f"{root} has no video.mov")
    points = []
    try:
        points = json.loads((root / "points.json").read_text()).get("points", [])
    except (OSError, ValueError):
        pass
    return {"root": root, "meta": meta, "rows": rows, "points": points}


def choose_frames(rows: list[dict], target: int) -> tuple[list[int], int]:
    """About `target` evenly spread row positions, leaving out frames where tracking was limited
    (their poses are unreliable). Returns (positions, how many limited frames were left out)."""
    good = [i for i, r in enumerate(rows) if r.get("tracking") == "normal"]
    pool = good if len(good) >= max(2, MIN_GOOD_SHARE * len(rows)) else list(range(len(rows)))
    left_out = len(rows) - len(pool)
    if len(pool) <= target:
        return pool, left_out
    step = len(pool) / target
    return [pool[int(k * step)] for k in range(target)], left_out


# ---------------------------------------------------------------- geometry

def matmul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def transpose(a):
    return [[a[j][i] for j in range(3)] for i in range(3)]


def colmap_camera(pose16) -> tuple[list[list[float]], list[float]]:
    """(camera-to-world rotation with COLMAP camera axes, camera centre) from an ARKit pose.
    ARKit's camera has y up and looks along -z; COLMAP's has y down and looks along +z."""
    m = [list(map(float, pose16[r * 4:r * 4 + 4])) for r in range(3)]
    R = [[m[r][0], -m[r][1], -m[r][2]] for r in range(3)]
    return R, [m[0][3], m[1][3], m[2][3]]


def turn_cw(R, K, w, h):
    """The same camera after turning its image 90 degrees clockwise: new x = -old y, new y = old x;
    a pixel (u, v) moves to (h - v, u). K is (fx, fy, cx, cy)."""
    fx, fy, cx, cy = K
    R2 = [[-R[r][1], R[r][0], R[r][2]] for r in range(3)]
    return R2, (fy, fx, h - cy, cx), h, w


def upright_turns(rotations, up=(0.0, 1.0, 0.0)) -> int:
    """How many clockwise quarter turns make the images upright: world up should point to the top
    of the image (-y in COLMAP's camera). Uses the median direction over all frames."""
    angles = []
    for R in rotations:                       # camera-to-world; world up in camera axes = R^T up
        ux = sum(R[k][0] * up[k] for k in range(3))
        uy = sum(R[k][1] * up[k] for k in range(3))
        if math.hypot(ux, uy) > 0.3:          # skip frames looking straight up or down
            angles.append(math.atan2(ux, -uy))   # 0: already upright; +90 degrees: up is to the right
    if not angles:
        return 0
    sx = sum(math.sin(a) for a in angles)
    cx = sum(math.cos(a) for a in angles)
    a = math.degrees(math.atan2(sx, cx))
    # up pointing right (+90) needs a quarter turn anticlockwise = three clockwise
    return round(-a / 90) % 4


def quaternion(R) -> tuple[float, float, float, float]:
    """(qw, qx, qy, qz) of a rotation matrix, qw >= 0."""
    tr = R[0][0] + R[1][1] + R[2][2]
    if tr > 0:
        s = math.sqrt(tr + 1.0) * 2
        q = (0.25 * s, (R[2][1] - R[1][2]) / s, (R[0][2] - R[2][0]) / s, (R[1][0] - R[0][1]) / s)
    elif R[0][0] > R[1][1] and R[0][0] > R[2][2]:
        s = math.sqrt(1.0 + R[0][0] - R[1][1] - R[2][2]) * 2
        q = ((R[2][1] - R[1][2]) / s, 0.25 * s, (R[0][1] + R[1][0]) / s, (R[0][2] + R[2][0]) / s)
    elif R[1][1] > R[2][2]:
        s = math.sqrt(1.0 + R[1][1] - R[0][0] - R[2][2]) * 2
        q = ((R[0][2] - R[2][0]) / s, (R[0][1] + R[1][0]) / s, 0.25 * s, (R[1][2] + R[2][1]) / s)
    else:
        s = math.sqrt(1.0 + R[2][2] - R[0][0] - R[1][1]) * 2
        q = ((R[1][0] - R[0][1]) / s, (R[0][2] + R[2][0]) / s, (R[1][2] + R[2][1]) / s, 0.25 * s)
    n = math.sqrt(sum(v * v for v in q))
    q = tuple(v / n for v in q)
    return q if q[0] >= 0 else tuple(-v for v in q)


def median(xs):
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


# ---------------------------------------------------------------- COLMAP binary model

def write_model(folder: Path, camera: dict, images: list[dict], points: list[dict]) -> None:
    """cameras.bin (one PINHOLE camera), images.bin (no 2D points) and points3D.bin (no tracks).
    camera: {id, width, height, params: (fx, fy, cx, cy)}; images: {id, name, R (world-to-camera),
    t, camera}; points: {x, y, z, r, g, b}."""
    folder.mkdir(parents=True, exist_ok=True)
    cam = struct.pack("<Q", 1) + struct.pack("<iiQQ", camera["id"], PINHOLE, camera["width"], camera["height"]) \
        + struct.pack("<4d", *camera["params"])
    (folder / "cameras.bin").write_bytes(cam)
    out = bytearray(struct.pack("<Q", len(images)))
    for im in images:
        out += struct.pack("<I7dI", im["id"], *quaternion(im["R"]), *im["t"], im["camera"])
        out += im["name"].encode() + b"\0" + struct.pack("<Q", 0)
    (folder / "images.bin").write_bytes(bytes(out))
    out = bytearray(struct.pack("<Q", len(points)))
    for i, p in enumerate(points, 1):
        out += struct.pack("<Q3d3BdQ", i, p["x"], p["y"], p["z"], int(p["r"]), int(p["g"]), int(p["b"]), 0.0, 0)
    (folder / "points3D.bin").write_bytes(bytes(out))


def model_counts(folder: Path) -> tuple[int, int]:
    """(images, points) in a binary model, from the counts at the start of each file."""
    def count(name):
        try:
            with open(Path(folder) / name, "rb") as f:
                head = f.read(8)
            return struct.unpack("<Q", head)[0] if len(head) == 8 else 0
        except OSError:
            return 0
    return count("images.bin"), count("points3D.bin")


def fill_points(points: list[dict], centres: list[list[float]]) -> list[dict]:
    """Too few ARKit points to start training from: add grey points in a ball around where the
    cameras were looking (inside the ring of cameras), so training has something to grow."""
    if len(points) >= MIN_POINTS or not centres:
        return points
    c = [median([p[k] for p in centres]) for k in range(3)]
    radius = median([math.dist(p, c) for p in centres]) * 0.5 or 1.0
    extra, n = [], MIN_POINTS - len(points)
    golden = math.pi * (3 - math.sqrt(5))
    for i in range(n):                         # evenly over a ball: deterministic, no random
        y = 1 - 2 * (i + 0.5) / n
        r = math.sqrt(max(0.0, 1 - y * y))
        a = golden * i
        s = radius * ((i % 7) + 1) / 7
        extra.append({"x": c[0] + s * r * math.cos(a), "y": c[1] + s * y, "z": c[2] + s * r * math.sin(a),
                      "r": 128, "g": 128, "b": 128})
    return points + extra


# ---------------------------------------------------------------- frames

def scaled_size(w: int, h: int, max_size: int) -> tuple[int, int]:
    s = min(1.0, max_size / max(w, h))
    return max(2, int(round(w * s / 2)) * 2), max(2, int(round(h * s / 2)) * 2)


TURN_FILTERS = {0: [], 1: ["transpose=1"], 2: ["transpose=1", "transpose=1"], 3: ["transpose=2"]}


def extract_frames(video: Path, indices: list[int], out_dir: Path, prefix: str, size: tuple[int, int],
                   turns: int) -> list[str]:
    """Video frames number `indices` (sorted) to out_dir/<prefix>_0001.jpg..., scaled to `size` and
    turned clockwise `turns` quarter turns. Returns the file names, in the order of `indices`."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.jpg"):
        old.unlink()
    pick = "+".join(f"eq(n\\,{i})" for i in indices)
    vf = ",".join([f"select='{pick}'", f"scale={size[0]}:{size[1]}:flags=area"] + TURN_FILTERS[turns])
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-noautorotate", "-i", str(video),
           "-vf", vf, "-fps_mode", "passthrough", "-qscale:v", "2", str(out_dir / f"{prefix}_%04d.jpg")]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise CaptureError("ffmpeg couldn't read the capture's video:\n" + r.stderr[-1500:])
    names = sorted(p.name for p in out_dir.glob(f"{prefix}_*.jpg"))
    if len(names) != len(indices):
        raise CaptureError(f"the video has fewer frames than frames.jsonl ({len(names)} of {len(indices)} "
                           "found): the capture may be damaged")
    return names


# ---------------------------------------------------------------- the whole import

def prepare(package: Path, frames_dir: Path, target: int, max_size: int, unpack_to: Path) -> dict:
    """Unpack, choose frames, extract them upright, and work out every frame's camera.
    Saves the result to frames_dir/capture.json and reuses it when it matches."""
    root = unpack(package, unpack_to)
    cap = load(root)
    rows, meta = cap["rows"], cap["meta"]
    plan_file = frames_dir / "capture.json"
    key = {"package": str(Path(root).resolve()), "rows": len(rows), "target": target, "max_size": max_size}
    try:
        plan = json.loads(plan_file.read_text())
        if plan.get("key") == key and len(list(frames_dir.glob("*.jpg"))) == len(plan["frames"]):
            plan["reused"] = True
            return plan
    except (OSError, ValueError):
        pass

    if target == "auto":                         # same rule as for videos: from the recording's length
        from .frames import auto_target
        target = auto_target(float(rows[-1].get("t", 0)) - float(rows[0].get("t", 0)))
    picked, left_out = choose_frames(rows, target)
    cams = [colmap_camera(rows[i]["pose"]) for i in picked]
    turns = upright_turns([R for R, _ in cams])
    w0, h0 = int(meta.get("width") or 0), int(meta.get("height") or 0)
    if not (w0 and h0):
        raise CaptureError("meta.json has no image width and height")
    w1, h1 = scaled_size(w0, h0, max_size)
    sx, sy = w1 / w0, h1 / h0
    K = [median([rows[i]["K"][k] for i in picked]) for k in range(4)]
    K = (K[0] * sx, K[1] * sy, K[2] * sx, K[3] * sy)
    names = extract_frames(root / "video.mov", picked, frames_dir, frames_dir.name, (w1, h1), turns)

    frames = []
    for name, i, (R, C) in zip(names, picked, cams):
        k, w, h = K, w1, h1
        for _ in range(turns):
            R, k, w, h = turn_cw(R, k, w, h)
        frames.append({"name": name, "row": i, "R_c2w": R, "centre": C})
    for _ in range(turns):
        K, w1, h1 = turn_cw([[1, 0, 0], [0, 1, 0], [0, 0, 1]], K, w1, h1)[1:]
    plan = {"key": key, "meta": meta, "turns": turns, "left_out": left_out, "rows": len(rows),
            "camera": {"id": 1, "width": w1, "height": h1, "params": list(K)},
            "frames": frames, "points": cap["points"]}
    plan_file.write_text(json.dumps(plan))
    plan["reused"] = False
    return plan


def images_of(plan: dict, ids: dict[str, int] | None = None, camera_id: int = 1) -> list[dict]:
    out = []
    for n, f in enumerate(plan["frames"], 1):
        R = transpose(f["R_c2w"])                         # world-to-camera
        t = [-sum(R[r][k] * f["centre"][k] for k in range(3)) for r in range(3)]
        out.append({"id": (ids or {}).get(f["name"], n), "name": f["name"], "R": R, "t": t, "camera": camera_id})
    return out


def write_arkit_run(plan: dict, frames_dir: Path, ws: Path) -> dict:
    """images/, sparse/0 and dense/ (images + sparse/0 + source_model.txt) from ARKit alone."""
    images = ws / "images"
    shutil.rmtree(images, ignore_errors=True)
    images.mkdir(parents=True)
    for f in plan["frames"]:
        shutil.copy2(frames_dir / f["name"], images / f["name"])
    points = fill_points([{"x": p["x"], "y": p["y"], "z": p["z"], "r": p["r"], "g": p["g"], "b": p["b"]}
                          for p in plan["points"]], [f["centre"] for f in plan["frames"]])
    sparse = ws / "sparse" / "0"
    write_model(sparse, plan["camera"], images_of(plan), points)
    publish_dense(ws, sparse)
    return {"images": len(plan["frames"]), "points": len(points), "arkit_points": len(plan["points"])}


def publish_dense(ws: Path, model: Path) -> None:
    """dense/ is what Brush trains on. The camera is PINHOLE (ARKit images aren't distorted), so
    the images are used as they are; the model is copied in."""
    dense = ws / "dense"
    shutil.rmtree(dense, ignore_errors=True)
    (dense / "sparse" / "0").mkdir(parents=True)
    shutil.copytree(ws / "images", dense / "images")
    for b in ("cameras.bin", "images.bin", "points3D.bin"):
        shutil.copy2(model / b, dense / "sparse" / "0" / b)
    (dense / "source_model.txt").write_text(str(Path(model).resolve()))


def refine_run(plan: dict, ws: Path, max_size: int) -> dict:
    """ARKit's poses, COLMAP's points: features and sequential matching on the frames, then
    triangulation with the poses fixed and a bundle adjustment that keeps the lens as ARKit
    measured it. Replaces sparse/0 and dense/ of an ARKit run."""
    from .colmap import Colmap
    from .progress import Progress
    work = ws / "refine"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    c = Colmap(ws, Progress(max_size))
    n = len(plan["frames"])
    db = work / "database.db"
    fx, fy, cx, cy = plan["camera"]["params"]
    c.run("feature_extractor", "--database_path", db, "--image_path", ws / "images",
          "--ImageReader.single_camera", "1", "--ImageReader.camera_model", "PINHOLE",
          "--ImageReader.camera_params", f"{fx},{fy},{cx},{cy}", *c.extract_gpu,
          label="extracting features", kind="extract", units=n)
    c.run("sequential_matcher", "--database_path", db, "--SequentialMatching.overlap", 15, *c.match_gpu,
          label="matching (sequential)", kind="match", units=n * 15)
    with sqlite3.connect(db) as con:                      # COLMAP's ids for our images and camera
        rows = con.execute("SELECT image_id, name, camera_id FROM images").fetchall()
    ids = {name: iid for iid, name, _ in rows}
    camera_id = rows[0][2] if rows else 1
    known = work / "known"
    write_model(known, {**plan["camera"], "id": camera_id}, images_of(plan, ids, camera_id), [])
    tri = work / "triangulated"
    tri.mkdir()
    c.run("point_triangulator", "--database_path", db, "--image_path", ws / "images",
          "--input_path", known, "--output_path", tri, label="triangulating points", kind="map", units=n,
          n_images=n)
    adjusted = work / "adjusted"
    adjusted.mkdir()
    c.run("bundle_adjuster", "--input_path", tri, "--output_path", adjusted,
          "--BundleAdjustment.refine_focal_length", 0, "--BundleAdjustment.refine_principal_point", 0,
          "--BundleAdjustment.refine_extra_params", 0, label="polishing poses", kind="map", units=n, n_images=n)
    sparse = ws / "sparse" / "0"
    for b in ("cameras.bin", "images.bin", "points3D.bin"):
        shutil.copy2(adjusted / b, sparse / b)
    publish_dense(ws, sparse)
    n_img, n_pts = model_counts(sparse)
    return {"images": n_img, "points": n_pts}


def summary_lines(plan: dict, package: Path, frames_dir: Path) -> list[tuple[str, str]]:
    m = plan["meta"]
    turned = {0: "", 1: ", turned a quarter clockwise", 2: ", turned upside down",
              3: ", turned a quarter anticlockwise"}[plan["turns"]]
    named = f" \"{m['name']}\"" if m.get("name") else ""
    out = [("frames", f"{frames_dir} ({len(plan['frames'])} jpg)"),
           ("capture", f"{package.name}{named}: {m.get('device', '?')}, {m.get('frames', plan['rows'])} frames, "
                       f"{m.get('duration', 0):.0f} s, {m.get('width')}x{m.get('height')}{turned}"
                       + (", focus and exposure locked" if m.get("cameraLocked") else "")),
           ("left out", f"{plan['left_out']} frames where tracking was limited")]
    if phone_check(m):
        out.append(("phone check", phone_check(m)))
    return out


def phone_check(meta: dict) -> str:
    """The app's own check of the recording (meta.json `quality`), in one line; '' if absent."""
    q = meta.get("quality")
    if not isinstance(q, dict):
        return ""
    parts = []
    if q.get("gap") is not None:
        parts.append(f"{360 - q['gap']:.0f} degrees around")
    if q.get("heights") is not None:
        parts.append(f"heights over {q['heights']:.0f} degrees")
    if q.get("unsteady") is not None:
        parts.append(f"{q['unsteady']:.0%} unsteady")
    if q.get("blurry") is not None:
        parts.append(f"{q['blurry']:.0%} blurry")
    return ", ".join(parts)


def describe(plan: dict) -> str:
    c = plan["camera"]
    named = f"\"{plan['meta']['name']}\": " if plan["meta"].get("name") else ""
    return (f"{named}{len(plan['frames'])} frames of {plan['rows']} ({plan['left_out']} with limited tracking left out), "
            f"{c['width']}x{c['height']}, {len(plan['points'])} ARKit points")
