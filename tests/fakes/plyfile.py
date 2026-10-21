"""Writes small synthetic Gaussian splats in the standard 3DGS PLY layout Brush exports.

The scene is a striped vase on a ground disc, stored y-down like COLMAP data, with Brush's
"Vertical axis" header comment, plus a few far-away floaters (the viewer should ignore them
when framing the camera).
"""
from __future__ import annotations

import math
import random
import struct
import sys
from pathlib import Path

C0 = 0.28209479177387814
PROPS = (["x", "y", "z", "nx", "ny", "nz", "f_dc_0", "f_dc_1", "f_dc_2"] + [f"f_rest_{i}" for i in range(45)]
         + ["opacity", "scale_0", "scale_1", "scale_2", "rot_0", "rot_1", "rot_2", "rot_3"])


def _dc(c: float) -> float:
    return (c - 0.5) / C0


def write_ply(path: Path, n: int = 20000, up_axis: str | None = "0 -1 0", seed: int = 1) -> Path:
    rnd = random.Random(seed)
    pts = []
    for _ in range(int(n * 0.7)):
        h, a = rnd.random(), rnd.random() * 2 * math.pi
        r = 0.35 + 0.18 * math.sin(h * math.pi * 1.6) - 0.12 * h
        col = (0.85, 0.35, 0.15) if int(h * 8) % 2 else (0.95, 0.85, 0.6)
        pts.append((r * math.cos(a), h * 1.2, r * math.sin(a), col, 0.012))
    while len(pts) < n:
        rr, a = math.sqrt(rnd.random()) * 1.4, rnd.random() * 2 * math.pi
        g = 0.35 + 0.15 * ((int(rr * 6) + int(a * 3)) % 2)
        pts.append((rr * math.cos(a), 0.0, rr * math.sin(a), (g, g, g * 1.05), 0.02))
    for _ in range(40):
        pts.append((rnd.uniform(-30, 30), rnd.uniform(-30, 30), rnd.uniform(-30, 30), (1, 1, 1), 0.05))
    header = "ply\nformat binary_little_endian 1.0\ncomment Exported from Brush\n"
    if up_axis:
        header += f"comment Vertical axis: {up_axis}\n"
    header += f"element vertex {len(pts)}\n" + "".join(f"property float {p}\n" for p in PROPS) + "end_header\n"
    row = struct.Struct("<" + "f" * len(PROPS))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(header.encode())
        for x, y, z, col, s in pts:
            ls = math.log(s)
            f.write(row.pack(x, -y, z, 0, 0, 0, _dc(col[0]), _dc(col[1]), _dc(col[2]), *([0.0] * 45),
                             4.0, ls, ls, ls, 1, 0, 0, 0))
    return path


def write_foggy_scene(path: Path, n_object: int = 8000, n_fog: int = 6000, seed: int = 3,
                      up_axis: str | None = "0 -1 0") -> Path:
    """An object (dense, opaque, small splats) inside a 'room' of large, faint splats -
    what an indoor capture with a poorly-filmed background looks like after training.
    Object splats come first, then fog: tests can tell them apart by index."""
    rnd = random.Random(seed)
    rows = []
    for _ in range(n_object):                         # vase-like object, centre (0, 0.6, 0), ~0.5 tall
        h, a = rnd.random(), rnd.random() * 2 * math.pi
        r = 0.2 + 0.08 * math.sin(h * math.pi * 1.6)
        rows.append((r * math.cos(a), 0.35 + h * 0.5, r * math.sin(a), (0.85, 0.4, 0.2), 3.0, 0.008))
    for _ in range(n_fog):                            # room: shell 1.5..6 around it, big and faint
        u, v = rnd.uniform(-1, 1), rnd.random() * 2 * math.pi
        d = rnd.uniform(1.5, 6.0)
        s = math.sqrt(1 - u * u)
        rows.append((d * s * math.cos(v), 0.6 + d * u * 0.6, d * s * math.sin(v),
                     (rnd.random(), rnd.random(), rnd.random()), rnd.uniform(-3.0, -0.8), rnd.uniform(0.15, 0.6)))
    header = "ply\nformat binary_little_endian 1.0\ncomment Exported from Brush\n"
    if up_axis:
        header += f"comment Vertical axis: {up_axis}\n"
    header += f"element vertex {len(rows)}\n" + "".join(f"property float {p}\n" for p in PROPS) + "end_header\n"
    row = struct.Struct("<" + "f" * len(PROPS))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(header.encode())
        for x, y, z, col, logit, s in rows:
            ls = math.log(s)
            f.write(row.pack(x, -y, z, 0, 0, 0, _dc(col[0]), _dc(col[1]), _dc(col[2]), *([0.0] * 45),
                             logit, ls, ls, ls, 1, 0, 0, 0))
    return path


def write_room_scene(path: Path, seed: int = 5, up_axis: str | None = "0 -1 0") -> Path:
    """A harder, more realistic capture: a vase standing on a table top, two solid (opaque)
    walls behind it, and faint fog in the room. Splat sizes tell the parts apart in tests:
    vase 0.008, table 0.02, walls 0.035, fog >= 0.15. World is y-down like COLMAP; the vase
    stands at the origin with its base on the table (y = 0.35 in object-up coordinates)."""
    rnd = random.Random(seed)
    rows = []
    for _ in range(6000):                          # vase on the table
        h, a = rnd.random(), rnd.random() * 2 * math.pi
        r = 0.18 + 0.07 * math.sin(h * math.pi * 1.6)
        rows.append((r * math.cos(a), 0.35 + h * 0.45, r * math.sin(a), (0.8, 0.35, 0.2), 3.0, 0.008))
    for _ in range(5000):                          # table top: disc of radius 1.1 at y = 0.35
        rr, a = math.sqrt(rnd.random()) * 1.1, rnd.random() * 2 * math.pi
        rows.append((rr * math.cos(a), 0.35, rr * math.sin(a), (0.55, 0.4, 0.25), 3.0, 0.02))
    for _ in range(4000):                          # two opaque walls, 2.5 away
        u, v = rnd.uniform(-2.5, 2.5), rnd.uniform(-0.5, 2.0)
        x, z = (u, -2.5) if rnd.random() < 0.5 else (2.5, u)
        rows.append((x, v, z, (0.8, 0.8, 0.75), 3.0, 0.035))
    for _ in range(3000):                          # fog
        u, w = rnd.uniform(-1, 1), rnd.random() * 2 * math.pi
        dd = rnd.uniform(1.4, 4.0)
        sq = math.sqrt(1 - u * u)
        rows.append((dd * sq * math.cos(w), 0.6 + dd * u * 0.5, dd * sq * math.sin(w),
                     (rnd.random(), rnd.random(), rnd.random()), rnd.uniform(-3.0, -0.8), rnd.uniform(0.15, 0.6)))
    header = "ply\nformat binary_little_endian 1.0\ncomment Exported from Brush\n"
    if up_axis:
        header += f"comment Vertical axis: {up_axis}\n"
    header += f"element vertex {len(rows)}\n" + "".join(f"property float {p}\n" for p in PROPS) + "end_header\n"
    row = struct.Struct("<" + "f" * len(PROPS))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(header.encode())
        for x, y, z, col, logit, sz in rows:
            ls = math.log(sz)
            f.write(row.pack(x, -y, z, 0, 0, 0, _dc(col[0]), _dc(col[1]), _dc(col[2]), *([0.0] * 45),
                             logit, ls, ls, ls, 1, 0, 0, 0))
    return path


def write_inside_room(path: Path, seed: int = 6, up_axis: str | None = "0 -1 0") -> Path:
    """A room filmed from inside it: a floor (y = 0) and four walls 2.5 from the middle, 2.6 high,
    a sofa-sized box by one wall, faint fog in the air, and junk seen through a window far
    outside. Splat sizes tell them apart: room 0.03, furniture 0.015, fog 0.3 or more, outside
    0.05 (opaque, 9 to 14 away). World is y-down like COLMAP."""
    rnd = random.Random(seed)
    rows = []
    for _ in range(6000):                          # floor
        rows.append((rnd.uniform(-2.5, 2.5), 0.0, rnd.uniform(-2.5, 2.5), (0.5, 0.4, 0.3), 3.0, 0.03))
    for _ in range(8000):                          # four walls
        u, v, w = rnd.uniform(-2.5, 2.5), rnd.uniform(0, 2.6), rnd.choice((-2.5, 2.5))
        x, z = (u, w) if rnd.random() < 0.5 else (w, u)
        rows.append((x, v, z, (0.85, 0.85, 0.8), 3.0, 0.03))
    for _ in range(3000):                          # a box against the wall at z = -2.5
        x, y, z = rnd.uniform(-1, 1), rnd.uniform(0, 0.45), rnd.uniform(-2.5, -1.7)
        k = rnd.randrange(3)
        if k == 0:
            y = 0.45
        elif k == 1:
            z = -1.7
        else:
            x = rnd.choice((-1.0, 1.0))
        rows.append((x, y, z, (0.3, 0.3, 0.6), 3.0, 0.015))
    for _ in range(2000):                          # fog in the air
        rows.append((rnd.uniform(-2, 2), rnd.uniform(0.3, 2.3), rnd.uniform(-2, 2),
                     (rnd.random(), rnd.random(), rnd.random()), rnd.uniform(-3.0, -1.0), rnd.uniform(0.3, 0.8)))
    for _ in range(500):                           # seen through a window: far outside
        d, a = rnd.uniform(9, 14), rnd.uniform(-0.5, 0.5)
        rows.append((d * math.cos(a), rnd.uniform(0, 3), d * math.sin(a), (0.3, 0.6, 0.3), 3.0, 0.05))
    header = "ply\nformat binary_little_endian 1.0\ncomment Exported from Brush\n"
    if up_axis:
        header += f"comment Vertical axis: {up_axis}\n"
    header += f"element vertex {len(rows)}\n" + "".join(f"property float {p}\n" for p in PROPS) + "end_header\n"
    row = struct.Struct("<" + "f" * len(PROPS))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(header.encode())
        for x, y, z, col, logit, sz in rows:
            ls = math.log(sz)
            f.write(row.pack(x, -y, z, 0, 0, 0, _dc(col[0]), _dc(col[1]), _dc(col[2]), *([0.0] * 45),
                             logit, ls, ls, ls, 1, 0, 0, 0))
    return path


def write_points(path: Path, pts, up_axis: str | None = "0 -1 0") -> Path:
    """A splat PLY from (x, y, z, (r, g, b), size, opacity) tuples, in world coordinates as given."""
    header = "ply\nformat binary_little_endian 1.0\ncomment Exported from Brush\n"
    if up_axis:
        header += f"comment Vertical axis: {up_axis}\n"
    header += f"element vertex {len(pts)}\n" + "".join(f"property float {p}\n" for p in PROPS) + "end_header\n"
    row = struct.Struct("<" + "f" * len(PROPS))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(header.encode())
        for x, y, z, col, s, op in pts:
            ls = math.log(s)
            logit = math.log(op / (1 - op))
            f.write(row.pack(x, y, z, 0, 0, 0, _dc(col[0]), _dc(col[1]), _dc(col[2]), *([0.0] * 45),
                             logit, ls, ls, ls, 1, 0, 0, 0))
    return path


THREE_OBJECTS = {1: (-0.8, -0.25, 0.0), 2: (0.0, -0.25, 0.0), 3: (0.8, -0.25, 0.0)}   # y-down, on a table at y=0


def write_three_objects(path: Path, n: int = 1500, seed: int = 9) -> Path:
    """Three balls (radius 0.22) in a row on a table, as a splat trained on all three would be:
    the objects opaque, a little faint table left over."""
    rnd = random.Random(seed)
    cols = {1: (0.9, 0.3, 0.2), 2: (0.2, 0.6, 0.9), 3: (0.3, 0.8, 0.3)}
    pts = []
    for oid, (cx, cy, cz) in THREE_OBJECTS.items():
        for _ in range(n):
            u, v = rnd.uniform(-1, 1), rnd.uniform(0, 2 * math.pi)
            r = 0.22 * rnd.random() ** 0.3
            w = math.sqrt(1 - u * u)
            pts.append((cx + r * w * math.cos(v), cy + r * u, cz + r * w * math.sin(v), cols[oid], 0.01, 0.95))
    for _ in range(n // 3):                            # leftover table, nearly transparent
        pts.append((rnd.uniform(-1.6, 1.6), 0.0, rnd.uniform(-0.8, 0.8), (0.5, 0.5, 0.5), 0.03, 0.03))
    return write_points(path, pts)


def write_cameras_bin(path: Path, width: int = 320, height: int = 240, params=(260.0, 260.0, 160.0, 120.0, 0, 0, 0, 0),
                      model: int = 4) -> Path:
    """COLMAP cameras.bin with one camera (OPENCV by default: fx fy cx cy k1 k2 p1 p2)."""
    out = struct.pack("<Q", 1) + struct.pack("<iiQQ", 1, model, width, height) + struct.pack(f"<{len(params)}d", *params)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(out)
    return path


def write_images_bin(path: Path, n: int = 36, radius: float = 1.6, target=(0.0, -0.6, 0.0), seed: int = 4,
                     outward: bool = False) -> Path:
    """COLMAP images.bin with n cameras walking around `target` at ~radius, at three heights,
    each looking at the target (world coordinates match write_foggy_scene, which is y-down).
    outward: each looks away from it instead, as when a room is filmed from inside."""
    rnd = random.Random(seed)
    out = bytearray(struct.pack("<Q", n))
    for i in range(n):
        a = 2 * math.pi * i / n
        h = (-0.4, 0.0, 0.5)[i % 3]
        c = (target[0] + radius * math.cos(a), target[1] - h, target[2] + radius * math.sin(a))
        # camera z axis looks at the target (with a little hand-held wobble)
        fwd = [(c[k] - target[k] if outward else target[k] - c[k]) + rnd.uniform(-0.02, 0.02) for k in range(3)]
        nf = math.sqrt(sum(v * v for v in fwd))
        z = [v / nf for v in fwd]
        up = (0.0, -1.0, 0.0)
        x = [up[1] * z[2] - up[2] * z[1], up[2] * z[0] - up[0] * z[2], up[0] * z[1] - up[1] * z[0]]
        nx = math.sqrt(sum(v * v for v in x))
        x = [v / nx for v in x]
        y = [z[1] * x[2] - z[2] * x[1], z[2] * x[0] - z[0] * x[2], z[0] * x[1] - z[1] * x[0]]
        R = [x, y, z]                                  # world -> camera rows
        t = [-sum(R[r][k] * c[k] for k in range(3)) for r in range(3)]
        qw, qx, qy, qz = _rot_to_quat(R)
        out += struct.pack("<I7dI", i + 1, qw, qx, qy, qz, *t, 1)
        out += f"frame_{i + 1:04d}.jpg".encode() + b"\0"
        out += struct.pack("<Q", 0)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))
    return path


def _rot_to_quat(R):
    tr = R[0][0] + R[1][1] + R[2][2]
    if tr > 0:
        s = math.sqrt(tr + 1.0) * 2
        return (0.25 * s, (R[2][1] - R[1][2]) / s, (R[0][2] - R[2][0]) / s, (R[1][0] - R[0][1]) / s)
    if R[0][0] > R[1][1] and R[0][0] > R[2][2]:
        s = math.sqrt(1.0 + R[0][0] - R[1][1] - R[2][2]) * 2
        return ((R[2][1] - R[1][2]) / s, 0.25 * s, (R[0][1] + R[1][0]) / s, (R[0][2] + R[2][0]) / s)
    if R[1][1] > R[2][2]:
        s = math.sqrt(1.0 + R[1][1] - R[0][0] - R[2][2]) * 2
        return ((R[0][2] - R[2][0]) / s, (R[0][1] + R[1][0]) / s, 0.25 * s, (R[1][2] + R[2][1]) / s)
    s = math.sqrt(1.0 + R[2][2] - R[0][0] - R[1][1]) * 2
    return ((R[1][0] - R[0][1]) / s, (R[0][2] + R[2][0]) / s, (R[1][2] + R[2][1]) / s, 0.25 * s)


if __name__ == "__main__":
    print(write_ply(Path(sys.argv[1]), int(sys.argv[2]) if len(sys.argv) > 2 else 20000))
