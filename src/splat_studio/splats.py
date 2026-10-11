"""Reading, diagnosing and cleaning Gaussian splat .ply files.

    splat-studio inspect model.ply [--run RUN_DIR]     numbers that explain "fog" around a model
    splat-studio clean   model.ply [--run RUN_DIR]     writes model_clean.ply (the original is never changed)

"Fog" is usually one of two things:
  * background splats: the room around the object was seen from few angles, so the trainer
    covered it with large, faint splats far from the object;
  * faint splats everywhere: inaccurate camera positions make the trainer blur instead of
    placing sharp splats.
Cleaning keeps the splats inside a sphere around the object and drops nearly invisible
ones. The sphere comes from where the cameras were (if a COLMAP run folder is given: the
object is where the cameras were pointing) or, for any .ply, from where the opaque splats are.

Pure standard library, so it works with the Python that ships with macOS.
"""
from __future__ import annotations

import argparse
import math
import re
import struct
import sys
from array import array
from pathlib import Path

PLY_TYPES = {"float": "f", "float32": "f", "double": "d", "float64": "d", "uchar": "B", "uint8": "B",
             "char": "b", "int8": "b", "ushort": "H", "uint16": "H", "short": "h", "int16": "h",
             "uint": "I", "uint32": "I", "int": "i", "int32": "i"}
CHUNK_ROWS = 65536


class PlyError(ValueError):
    pass


# ---------------------------------------------------------------- PLY header + fields

class PlyFile:
    """A binary little-endian PLY with a single 'vertex' element of scalar properties
    (the standard 3D Gaussian splatting layout written by Brush, gsplat, Nerfstudio...)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        with open(self.path, "rb") as f:
            head = f.read(1 << 16)
        end = head.find(b"end_header\n")
        if not head.startswith(b"ply") or end < 0:
            raise PlyError(f"{self.path.name} is not a PLY file")
        self.header_size = end + len(b"end_header\n")
        self.header_lines = head[:end].decode("latin1").splitlines()
        if "format binary_little_endian 1.0" not in self.header_lines:
            raise PlyError("only binary little-endian PLY files are supported")
        self.count, self.props, in_vertex = 0, [], False
        for line in self.header_lines:
            parts = line.split()
            if parts[:2] == ["element", "vertex"]:
                self.count, in_vertex = int(parts[2]), True
            elif parts[:1] == ["element"]:
                raise PlyError("PLY files with more than one element aren't supported "
                               "(is it a compressed PLY? use the uncompressed export)")
            elif parts[:1] == ["property"] and in_vertex:
                if parts[1] == "list" or parts[1] not in PLY_TYPES:
                    raise PlyError(f"unsupported property: {line}")
                self.props.append((parts[2], PLY_TYPES[parts[1]]))
        self.row_fmt = "<" + "".join(t for _, t in self.props)
        self.stride = struct.calcsize(self.row_fmt)
        self.names = [n for n, _ in self.props]
        for need in ("x", "y", "z"):
            if need not in self.names:
                raise PlyError(f"PLY has no '{need}' property")
        expected = self.header_size + self.count * self.stride
        if self.path.stat().st_size < expected:
            raise PlyError(f"file is truncated ({self.path.stat().st_size} < {expected} bytes)")

    @property
    def comments(self) -> list[str]:
        return [l[len("comment "):] for l in self.header_lines if l.startswith("comment ")]

    def up_axis(self) -> tuple[float, float, float] | None:
        """Brush's 'Vertical axis' comment: 'x y z' numbers, or a letter like 'y'."""
        for c in self.comments:
            m = re.match(r"Vertical axis:\s*(.+)$", c)
            if not m:
                continue
            v = m.group(1).strip()
            nums = re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", v)
            if len(nums) == 3:
                return tuple(float(n) for n in nums)          # type: ignore[return-value]
            axis = {"x": (1.0, 0.0, 0.0), "y": (0.0, 1.0, 0.0), "z": (0.0, 0.0, 1.0)}.get(v.lower())
            if axis:
                return axis
        return None

    def read(self, fields: list[str]) -> dict[str, array]:
        """Read selected float fields for every splat, without loading the whole file."""
        missing = [f for f in fields if f not in self.names]
        if missing:
            raise PlyError(f"PLY has no {', '.join(missing)} propert{'y' if len(missing) == 1 else 'ies'}")
        # one struct per row that skips every byte we don't need
        fmt, want = "<", set(fields)
        order = []
        for name, t in self.props:
            if name in want:
                fmt += t
                order.append(name)
            else:
                fmt += f"{struct.calcsize('<' + t)}x"
        out = {f: array("f") for f in order}
        with open(self.path, "rb") as fh:
            fh.seek(self.header_size)
            left = self.count
            while left:
                n = min(CHUNK_ROWS, left)
                buf = fh.read(n * self.stride)
                cols = list(zip(*struct.iter_unpack(fmt, buf)))
                for name, col in zip(order, cols):
                    out[name].extend(col)
                left -= n
        return out


# ---------------------------------------------------------------- COLMAP cameras

def qvec_to_rotmat(q):
    w, x, y, z = q
    return [[1 - 2 * y * y - 2 * z * z, 2 * x * y - 2 * w * z, 2 * x * z + 2 * w * y],
            [2 * x * y + 2 * w * z, 1 - 2 * x * x - 2 * z * z, 2 * y * z - 2 * w * x],
            [2 * x * z - 2 * w * y, 2 * y * z + 2 * w * x, 1 - 2 * x * x - 2 * y * y]]


def read_cameras(images_bin: Path) -> list[dict]:
    """Camera centres and viewing directions from COLMAP's images.bin."""
    cams = []
    with open(images_bin, "rb") as f:
        data = f.read()
    if len(data) < 8:
        return cams
    n, = struct.unpack_from("<Q", data, 0)
    o = 8
    for _ in range(n):
        _img_id, qw, qx, qy, qz, tx, ty, tz, _cam_id = struct.unpack_from("<I7dI", data, o)
        o += struct.calcsize("<I7dI")
        name_end = data.index(b"\0", o)
        name = data[o:name_end].decode("utf-8", "replace")
        o = name_end + 1
        npts, = struct.unpack_from("<Q", data, o)
        o += 8 + npts * 24
        R = qvec_to_rotmat((qw, qx, qy, qz))
        # centre C = -R^T t ; viewing direction = R^T (0, 0, 1)
        c = tuple(-(R[0][i] * tx + R[1][i] * ty + R[2][i] * tz) for i in range(3))
        d = tuple(R[2][i] for i in range(3))
        cams.append({"name": name, "center": c, "dir": d})
    return cams


def find_cameras(run_dir: Path) -> list[dict]:
    for p in (run_dir / "dense" / "sparse" / "0" / "images.bin", run_dir / "sparse" / "0" / "images.bin"):
        if p.exists() and p.stat().st_size > 8:
            try:
                return read_cameras(p)
            except (struct.error, ValueError):
                return []
    return []


def look_at_point(cams: list[dict]) -> tuple[float, float, float] | None:
    """The point closest (least squares) to every camera's viewing ray: what they film."""
    if len(cams) < 2:
        return None
    A = [[0.0] * 3 for _ in range(3)]
    b = [0.0] * 3
    for cam in cams:
        c, d = cam["center"], cam["dir"]
        for i in range(3):
            for j in range(3):
                m = (1.0 if i == j else 0.0) - d[i] * d[j]
                A[i][j] += m
                b[i] += m * c[j]
    return _solve3(A, b)


def _solve3(A, b):
    det = (A[0][0] * (A[1][1] * A[2][2] - A[1][2] * A[2][1]) - A[0][1] * (A[1][0] * A[2][2] - A[1][2] * A[2][0])
           + A[0][2] * (A[1][0] * A[2][1] - A[1][1] * A[2][0]))
    if abs(det) < 1e-12:
        return None
    res = []
    for k in range(3):
        M = [row[:] for row in A]
        for i in range(3):
            M[i][k] = b[i]
        res.append((M[0][0] * (M[1][1] * M[2][2] - M[1][2] * M[2][1]) - M[0][1] * (M[1][0] * M[2][2] - M[1][2] * M[2][0])
                    + M[0][2] * (M[1][0] * M[2][1] - M[1][1] * M[2][0])) / det)
    return tuple(res)


# ---------------------------------------------------------------- statistics

def sigmoid(x: float) -> float:
    if x >= 0:
        return 1 / (1 + math.exp(-x))
    e = math.exp(x)
    return e / (1 + e)


def percentile(sorted_vals, q: float) -> float:
    if not sorted_vals:
        return float("nan")
    return sorted_vals[min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))]


def median3(xs, ys, zs) -> tuple[float, float, float]:
    return (percentile(sorted(xs), 0.5), percentile(sorted(ys), 0.5), percentile(sorted(zs), 0.5))


def load(ply: PlyFile) -> dict:
    f = ply.read(["x", "y", "z", "opacity", "scale_0", "scale_1", "scale_2"])
    opac = array("f", (sigmoid(v) for v in f["opacity"]))
    size = array("f", (math.exp(max(a, b, c)) for a, b, c in zip(f["scale_0"], f["scale_1"], f["scale_2"])))
    return {"x": f["x"], "y": f["y"], "z": f["z"], "opacity": opac, "size": size}


def object_region(d: dict, cams: list[dict] | None = None) -> dict:
    """Centre and radius of the object: from the cameras when known, else from opaque splats."""
    if cams:
        centre = look_at_point(cams)
        if centre:
            dists = sorted(math.dist(c["center"], centre) for c in cams)
            ring = percentile(dists, 0.5)
            # the object sits well inside the circle the cameras walked
            return {"center": centre, "radius": 0.8 * ring, "camera_distance": ring, "source": "cameras"}
    idx = [i for i, o in enumerate(d["opacity"]) if o >= 0.5] or list(range(len(d["opacity"])))
    xs, ys, zs = ([d[k][i] for i in idx] for k in ("x", "y", "z"))
    centre, radius = densest_cluster(xs, ys, zs)
    return {"center": centre, "radius": radius, "camera_distance": None, "source": "opaque splats"}


def densest_cluster(xs, ys, zs) -> tuple[tuple[float, float, float], float]:
    """Centre and radius of the object without camera information.

    Start at the densest spot of opaque splats and flood-fill through occupied voxels: the
    object and what it stands on form one connected piece, while walls and furniture further
    away are separate pieces and are left out. (If a floor connects everything, the whole
    connected room is kept - camera positions from a run avoid that.)"""
    n = len(xs)
    if n == 0:
        return (0.0, 0.0, 0.0), 1.0
    sx, sy, sz = sorted(xs), sorted(ys), sorted(zs)
    span = max(percentile(sx, 0.98) - percentile(sx, 0.02), percentile(sy, 0.98) - percentile(sy, 0.02),
               percentile(sz, 0.98) - percentile(sz, 0.02)) or 1.0
    cell = span / 40
    key = lambda x, y, z: (int(math.floor(x / cell)), int(math.floor(y / cell)), int(math.floor(z / cell)))
    occupied: dict = {}
    for i, p in enumerate(zip(xs, ys, zs)):
        occupied.setdefault(key(*p), []).append(i)
    # densest spot: the voxel whose 5x5x5 neighbourhood holds the most points
    coarse: dict = {}
    for k, v in occupied.items():
        ck = (k[0] // 5, k[1] // 5, k[2] // 5)
        coarse[ck] = coarse.get(ck, 0) + len(v)
    best_c = max(coarse, key=lambda c: sum(coarse.get((c[0] + a, c[1] + b, c[2] + e), 0)
                                            for a in (-1, 0, 1) for b in (-1, 0, 1) for e in (-1, 0, 1)))
    seed = (best_c[0] * 5 + 2, best_c[1] * 5 + 2, best_c[2] * 5 + 2)
    blob = _largest_cluster_near(occupied, seed)
    idx = [i for k in blob for i in occupied[k]]
    if len(idx) < 0.05 * n:                       # a tiny speck won: fall back to everything
        idx = list(range(n))
    pts = [(xs[i], ys[i], zs[i]) for i in idx]
    centre = median3(*zip(*pts))
    dist = sorted(math.dist(p, centre) for p in pts)
    return centre, 1.15 * percentile(dist, 0.98) + cell


def analyze(path: Path, run_dir: Path | None = None) -> dict:
    ply = PlyFile(path)
    d = load(ply)
    cams = find_cameras(run_dir) if run_dir else []
    region = object_region(d, cams)
    c, r = region["center"], region["radius"]
    n = ply.count
    dist = [math.dist((x, y, z), c) for x, y, z in zip(d["x"], d["y"], d["z"])]
    op_sorted, size_sorted = sorted(d["opacity"]), sorted(d["size"])
    median_size = percentile(size_sorted, 0.5)
    faint = sum(1 for o in d["opacity"] if o < 0.1)
    outside = [i for i, dd in enumerate(dist) if dd > r]
    fog = sum(1 for i in outside if d["opacity"][i] < 0.35 and d["size"][i] > 3 * median_size)
    # what the viewer would frame: middle 94% of splats with opacity >= 0.15
    vis = [i for i, o in enumerate(d["opacity"]) if o >= 0.15]
    def span(idx):
        if not idx:
            return 0.0
        axes = [sorted(d[k][i] for i in idx) for k in ("x", "y", "z")]
        return math.sqrt(sum((percentile(a, 0.97) - percentile(a, 0.03)) ** 2 for a in axes))
    inside_opaque = [i for i, dd in enumerate(dist) if dd <= r and d["opacity"][i] >= 0.5]
    return {
        "file": str(path), "splats": n, "up_axis": ply.up_axis(), "region": region,
        "cameras": len(cams),
        "opacity": {"p10": percentile(op_sorted, 0.1), "median": percentile(op_sorted, 0.5),
                    "p90": percentile(op_sorted, 0.9), "faint_share": faint / n if n else 0},
        "size": {"median": median_size, "p99": percentile(size_sorted, 0.99)},
        "outside_share": len(outside) / n if n else 0,
        "fog_share": fog / n if n else 0,
        "scene_span": span(vis), "object_span": span(inside_opaque) or 2 * r,
        "max_distance": max(dist) if dist else 0,
    }


def diagnosis(a: dict) -> list[str]:
    """Plain-language conclusions from analyze()."""
    out = []
    if a["fog_share"] > 0.1:
        out.append(f"{a['fog_share']:.0%} of splats are large and faint, outside the object: background fog. "
                   "`splat-studio clean` removes them (the original file is kept).")
    if a["outside_share"] > 0.4:
        out.append(f"{a['outside_share']:.0%} of splats lie outside the object region: the model contains a lot "
                   "of background. Cropping (clean, or the viewer's Crop slider) will help most.")
    ratio = a["scene_span"] / a["object_span"] if a["object_span"] else 0
    if ratio > 3:
        out.append(f"The viewer frames everything ({a['scene_span']:.2g} units across) while the object is about "
                   f"{a['object_span']:.2g}: the camera starts inside the background cloud.")
    if a["opacity"]["faint_share"] > 0.5:
        out.append(f"{a['opacity']['faint_share']:.0%} of all splats are nearly transparent, even near the object. "
                   "That points to inaccurate camera positions or too little training - re-run COLMAP, "
                   "or train with higher quality.")
    if a["up_axis"] is None:
        out.append("The file has no up-axis comment, so the viewer guesses the orientation (use Flip if needed).")
    if not out:
        out.append("No obvious fog problem found in the file: if it still looks foggy, the cause is likely the "
                   "capture itself (blur, few angles).")
    return out


def format_report(a: dict) -> str:
    r = a["region"]
    lines = [
        f"File:            {a['file']}",
        f"Splats:          {a['splats']:,}",
        f"Up axis:         {a['up_axis'] if a['up_axis'] else 'not recorded'}",
        f"Object region:   centre ({', '.join(f'{v:.3g}' for v in r['center'])}), radius {r['radius']:.3g}"
        f"  [from {r['source']}{', %d cameras' % a['cameras'] if a['cameras'] else ''}]",
        f"Opacity:         10%: {a['opacity']['p10']:.2f}  median: {a['opacity']['median']:.2f}  "
        f"90%: {a['opacity']['p90']:.2f}   nearly invisible (<0.1): {a['opacity']['faint_share']:.0%}",
        f"Splat size:      median {a['size']['median']:.3g}, 99%: {a['size']['p99']:.3g}",
        f"Outside object:  {a['outside_share']:.0%} of splats   (farthest {a['max_distance']:.3g} from the centre)",
        f"Fog-like:        {a['fog_share']:.0%} of splats (large, faint, outside the object)",
        f"Framing:         whole scene {a['scene_span']:.3g} across vs object {a['object_span']:.3g}",
        "",
        "Diagnosis:",
    ] + [f"  - {d}" for d in diagnosis(a)]
    return "\n".join(lines)


# ---------------------------------------------------------------- cleaning

def clean(path: Path, out: Path | None = None, run_dir: Path | None = None, radius_scale: float = 1.0,
          min_opacity: float = 0.02) -> dict:
    """Write a copy keeping only splats inside the object region and not nearly invisible.
    Returns stats. The input file is only read."""
    path = Path(path)
    out = Path(out) if out else path.with_name(path.stem + "_clean" + path.suffix)
    if out.resolve() == path.resolve():
        raise ValueError("refusing to overwrite the original file")
    ply = PlyFile(path)
    d = load(ply)
    cams = find_cameras(run_dir) if run_dir else []
    region = object_region(d, cams)
    c, r = region["center"], region["radius"] * radius_scale
    keep = bytearray(ply.count)
    kept = 0
    for i, (x, y, z, o) in enumerate(zip(d["x"], d["y"], d["z"], d["opacity"])):
        if o >= min_opacity and math.dist((x, y, z), c) <= r:
            keep[i] = 1
            kept += 1
    header = []
    for line in ply.header_lines:
        if line.startswith("element vertex"):
            line = f"element vertex {kept}"
        header.append(line)
    header.insert(2, f"comment Cleaned by Splat Studio: kept {kept} of {ply.count} splats "
                     f"(radius {r:.4g} around the object, opacity >= {min_opacity})")
    tmp = out.with_name(out.name + ".part")
    with open(path, "rb") as src, open(tmp, "wb") as dst:
        dst.write(("\n".join(header) + "\nend_header\n").encode("latin1"))
        src.seek(ply.header_size)
        i = 0
        while i < ply.count:
            n = min(CHUNK_ROWS, ply.count - i)
            buf = src.read(n * ply.stride)
            mv = memoryview(buf)
            out_buf = bytearray()
            for k in range(n):
                if keep[i + k]:
                    out_buf += mv[k * ply.stride:(k + 1) * ply.stride]
            dst.write(out_buf)
            i += n
    tmp.replace(out)
    return {"input": str(path), "output": str(out), "kept": kept, "total": ply.count,
            "removed_share": 1 - kept / ply.count if ply.count else 0, "region": {**region, "radius": r}}


# ---------------------------------------------------------------- command line

def main_inspect(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="splat-studio inspect", description="Explain what's in a splat .ply file.")
    ap.add_argument("ply", type=Path)
    ap.add_argument("--run", type=Path, help="the COLMAP run folder it was trained from (uses camera positions)")
    a = ap.parse_args(argv)
    try:
        print(format_report(analyze(a.ply, a.run)))
    except (PlyError, OSError) as e:
        sys.exit(f"Can't inspect {a.ply}: {e}")


def main_clean(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="splat-studio clean",
                                 description="Remove background fog: writes NAME_clean.ply, keeps the original.")
    ap.add_argument("ply", type=Path)
    ap.add_argument("--run", type=Path, help="the COLMAP run folder it was trained from (uses camera positions)")
    ap.add_argument("--out", type=Path, help="output file (default: NAME_clean.ply next to the input)")
    ap.add_argument("--radius", type=float, default=1.0, help="scale the kept region (e.g. 1.3 keeps more)")
    ap.add_argument("--min-opacity", type=float, default=0.02, help="drop splats fainter than this (0-1)")
    a = ap.parse_args(argv)
    try:
        s = clean(a.ply, a.out, a.run, a.radius, a.min_opacity)
    except (PlyError, OSError, ValueError) as e:
        sys.exit(f"Can't clean {a.ply}: {e}")
    print(f"Kept {s['kept']:,} of {s['total']:,} splats ({s['removed_share']:.0%} removed)")
    print(f"Clean copy: {s['output']}")                      # parsed by the web UI


# ---------------------------------------------------------------- background removal (object only)

def _plane_from(p1, p2, p3):
    u = [p2[i] - p1[i] for i in range(3)]
    v = [p3[i] - p1[i] for i in range(3)]
    n = [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]]
    ln = math.sqrt(sum(c * c for c in n))
    if ln < 1e-12:
        return None
    n = [c / ln for c in n]
    return n, sum(n[i] * p1[i] for i in range(3))


def find_support_plane(points, eps: float, up=None, iters: int = 200, seed: int = 7, min_share: float = 0.12):
    """RANSAC: the flat surface the object stands on (table, floor). Returns (normal, d, share)
    or None. With a known up axis, only near-horizontal planes count."""
    import random
    rnd = random.Random(seed)
    if len(points) < 50:
        return None
    sample = points if len(points) <= 6000 else rnd.sample(points, 6000)
    best = None
    count = lambda n, d: sum(1 for p in sample if abs(n[0] * p[0] + n[1] * p[1] + n[2] * p[2] - d) < eps)
    if up is not None:
        # a support surface is horizontal: try the most crowded height along the up axis first,
        # so detection doesn't depend on RANSAC luck when the surface is a small share
        nu = math.sqrt(sum(v * v for v in up)) or 1.0
        n0 = [v / nu for v in up]
        heights = [n0[0] * p[0] + n0[1] * p[1] + n0[2] * p[2] for p in sample]
        bins = {}
        for h in heights:
            b = math.floor(h / eps)
            bins[b] = bins.get(b, 0) + 1
        top = max(bins, key=lambda b: bins[b] + bins.get(b - 1, 0) + bins.get(b + 1, 0))
        near = [h for h in heights if (top - 1) * eps <= h < (top + 2) * eps]
        d0 = sorted(near)[len(near) // 2]                    # the crowded height itself
        best = (n0, d0, count(n0, d0))
    for _ in range(iters):
        pl = _plane_from(*rnd.sample(sample, 3))
        if not pl:
            continue
        n, d = pl
        if up is not None and abs(sum(n[i] * up[i] for i in range(3))) < 0.85:
            continue
        inl = count(n, d)
        if best is None or inl > best[2]:
            best = (n, d, inl)
    if not best or best[2] / len(sample) < min_share:
        return None
    # refine: least-squares-free re-centring - average the inliers' offset along the normal
    n, d, _ = best
    inliers = [p for p in sample if abs(n[0] * p[0] + n[1] * p[1] + n[2] * p[2] - d) < eps]
    d = sum(n[0] * p[0] + n[1] * p[1] + n[2] * p[2] for p in inliers) / len(inliers)
    return n, d, len(inliers) / len(sample)


def _largest_cluster_near(voxels: dict, seed_key) -> set:
    """Flood-fill over occupied voxels (26-neighbourhood), starting from the occupied voxel
    closest to seed_key; returns the voxel keys of that connected blob."""
    if not voxels:
        return set()
    start = min(voxels, key=lambda k: sum((k[i] - seed_key[i]) ** 2 for i in range(3)))
    seen, stack = {start}, [start]
    while stack:
        x, y, z = stack.pop()
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    k = (x + dx, y + dy, z + dz)
                    if k in voxels and k not in seen:
                        seen.add(k)
                        stack.append(k)
    return seen


def isolate(path: Path, out: Path | None = None, run_dir: Path | None = None, radius_scale: float = 1.0,
            min_opacity: float = 0.05) -> dict:
    """Write NAME_object.ply with only the object: no fog, no table/floor, no surroundings.

    1. keep the region around the object (like clean());
    2. find the flat surface it stands on and drop it, plus everything on the far side of it;
    3. keep the connected blob of splats around the object's centre.
    The input file is only read."""
    path = Path(path)
    out = Path(out) if out else path.with_name(path.stem + "_object" + path.suffix)
    if out.resolve() == path.resolve():
        raise ValueError("refusing to overwrite the original file")
    ply = PlyFile(path)
    d = load(ply)
    cams = find_cameras(run_dir) if run_dir else []
    region = object_region(d, cams)
    c, r = region["center"], region["radius"] * radius_scale
    n_all = ply.count
    pts = list(zip(d["x"], d["y"], d["z"]))
    inside = [i for i in range(n_all) if d["opacity"][i] >= min_opacity and math.dist(pts[i], c) <= r]
    solid = [i for i in inside if d["opacity"][i] >= 0.3]

    # 2. support plane
    up = ply.up_axis()
    eps = max(0.012 * r, 1e-6)
    plane = find_support_plane([pts[i] for i in solid], eps, up)
    removed_plane = 0
    if plane:
        n, dd, _ = plane
        side = lambda p: n[0] * p[0] + n[1] * p[1] + n[2] * p[2] - dd
        # the object is on the side where most solid splats away from the plane are
        above = sum(1 for i in solid if side(pts[i]) > 2 * eps)
        below = sum(1 for i in solid if side(pts[i]) < -2 * eps)
        sign = 1 if above >= below else -1
        keep_side = [i for i in inside if sign * side(pts[i]) > 1.5 * eps]
        removed_plane = len(inside) - len(keep_side)
        inside = keep_side
        solid = [i for i in inside if d["opacity"][i] >= 0.3]

    # 3. connected blob around the centre (on a voxel grid of the solid splats)
    vox = r / 30
    key = lambda p: (int(math.floor(p[0] / vox)), int(math.floor(p[1] / vox)), int(math.floor(p[2] / vox)))
    occupied = {}
    for i in solid:
        occupied.setdefault(key(pts[i]), []).append(i)
    if occupied:
        centre_of_rest = median3([pts[i][0] for i in solid], [pts[i][1] for i in solid], [pts[i][2] for i in solid])
        blob = _largest_cluster_near(occupied, key(centre_of_rest))
        grown = {(x + a, y + b, z + e) for (x, y, z) in blob for a in (-1, 0, 1) for b in (-1, 0, 1) for e in (-1, 0, 1)}
        keep_idx = {i for i in inside if key(pts[i]) in grown}
    else:
        keep_idx = set(inside)

    keep = bytearray(n_all)
    for i in keep_idx:
        keep[i] = 1
    kept = len(keep_idx)
    header = [f"element vertex {kept}" if l.startswith("element vertex") else l for l in ply.header_lines]
    header.insert(2, f"comment Background removed by Splat Studio: kept {kept} of {n_all} splats"
                     f"{' (support plane removed)' if plane else ''}")
    _write_rows(path, ply, out, header, keep)
    return {"input": str(path), "output": str(out), "kept": kept, "total": n_all,
            "removed_share": 1 - kept / n_all if n_all else 0, "plane": bool(plane),
            "plane_removed": removed_plane, "region": {**region, "radius": r}}


def _write_rows(path: Path, ply: PlyFile, out: Path, header: list[str], keep: bytearray) -> None:
    tmp = out.with_name(out.name + ".part")
    with open(path, "rb") as src, open(tmp, "wb") as dst:
        dst.write(("\n".join(header) + "\nend_header\n").encode("latin1"))
        src.seek(ply.header_size)
        i = 0
        while i < ply.count:
            n = min(CHUNK_ROWS, ply.count - i)
            mv = memoryview(src.read(n * ply.stride))
            buf = bytearray()
            for k in range(n):
                if keep[i + k]:
                    buf += mv[k * ply.stride:(k + 1) * ply.stride]
            dst.write(buf)
            i += n
    tmp.replace(out)


def main_isolate(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="splat-studio isolate",
                                 description="Remove the background: writes NAME_object.ply with only the object "
                                             "(no table, floor or room). The original is kept.")
    ap.add_argument("ply", type=Path)
    ap.add_argument("--run", type=Path, help="the COLMAP run folder it was trained from (uses camera positions)")
    ap.add_argument("--out", type=Path, help="output file (default: NAME_object.ply next to the input)")
    ap.add_argument("--radius", type=float, default=1.0, help="scale the search region (e.g. 1.3 keeps more)")
    a = ap.parse_args(argv)
    try:
        s = isolate(a.ply, a.out, a.run, a.radius)
    except (PlyError, OSError, ValueError) as e:
        sys.exit(f"Can't remove the background from {a.ply}: {e}")
    print(f"Kept {s['kept']:,} of {s['total']:,} splats ({s['removed_share']:.0%} removed"
          f"{', support surface removed' if s['plane'] else ''})")
    print(f"Object copy: {s['output']}")                     # parsed by the web UI
