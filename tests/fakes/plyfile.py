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


if __name__ == "__main__":
    print(write_ply(Path(sys.argv[1]), int(sys.argv[2]) if len(sys.argv) > 2 else 20000))
