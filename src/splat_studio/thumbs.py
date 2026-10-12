"""Library pictures that show the object, not just the first frame of the video.

When a run was trained on a selection ("Choose what to keep"), its picture is a frame where
the selection is large and fully in view, cropped around it, with everything else blurred and
darkened. With several objects, each object's splat gets its own picture the same way, from
that object's masks.

    run_dir/thumbs/run.jpg            the run (and its splats without their own picture)
    run_dir/thumbs/<splat stem>.jpg   one object of several

Standard library plus ffmpeg (which the app needs anyway).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

WIDTH = 480
ASPECT = 16 / 10            # the library tiles
SAMPLES = 24
PROBE_WIDTH = 160


def _probe(mask: Path) -> tuple[int, int, bytes] | None:
    r = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(mask), "-vf",
                        f"scale={PROBE_WIDTH}:-2:flags=neighbor,format=gray", "-f", "rawvideo", "-"],
                       capture_output=True)
    if r.returncode != 0 or not r.stdout or len(r.stdout) % PROBE_WIDTH:
        return None
    return PROBE_WIDTH, len(r.stdout) // PROBE_WIDTH, r.stdout


def _bbox(w: int, h: int, px: bytes) -> tuple[int, tuple[int, int, int, int] | None]:
    area, x0, y0, x1, y1 = 0, w, h, -1, -1
    for y in range(h):
        row = px[y * w:(y + 1) * w]
        xs = [x for x in range(w) if row[x] > 127]
        if xs:
            area += len(xs)
            x0, x1 = min(x0, xs[0]), max(x1, xs[-1])
            y0, y1 = min(y0, y), max(y1, y)
    return area, ((x0, y0, x1, y1) if area else None)


def best_frame(mask_dir: Path) -> tuple[Path, tuple[float, float, float, float]] | None:
    """The mask (among ~24 spread over the video) where the selection is largest without
    touching the frame's edge, and its box as fractions of the frame."""
    masks = sorted(Path(mask_dir).glob("*.png"))
    if not masks:
        return None
    step = max(1, len(masks) // SAMPLES)
    best, best_score = None, -1.0
    for m in masks[::step]:
        probe = _probe(m)
        if not probe:
            continue
        w, h, px = probe
        area, box = _bbox(w, h, px)
        if not box:
            continue
        inside = box[0] > 0 and box[1] > 0 and box[2] < w - 1 and box[3] < h - 1
        score = area * (1.0 if inside else 0.35)          # cut off by the edge: only if nothing better
        if score > best_score:
            best_score = score
            best = (m, (box[0] / w, box[1] / h, (box[2] + 1) / w, (box[3] + 1) / h))
    return best


def crop_box(box, fw: int, fh: int, pad: float = 0.18) -> tuple[int, int, int, int]:
    """(x, y, w, h) in pixels: the box with some room around it, at the tiles' aspect ratio,
    kept inside the frame."""
    x0, y0, x1, y1 = box[0] * fw, box[1] * fh, box[2] * fw, box[3] * fh
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    w, h = (x1 - x0) * (1 + 2 * pad), (y1 - y0) * (1 + 2 * pad)
    w, h = max(w, h * ASPECT), max(h, w / ASPECT)
    if w > fw:
        w, h = fw, fw / ASPECT
    if h > fh:
        h, w = fh, fh * ASPECT
    w, h = max(16, int(w) // 2 * 2), max(16, int(h) // 2 * 2)
    x = int(min(max(0, cx - w / 2), fw - w))
    y = int(min(max(0, cy - h / 2), fh - h))
    return x, y, w, h


def make(frames_dir: Path, mask_dir: Path, out: Path) -> Path | None:
    """Picture of the selection in mask_dir (masks named like the frames). None if there's nothing to show."""
    from .masks import jpeg_size
    found = best_frame(mask_dir)
    if not found:
        return None
    mask, box = found
    frame = Path(frames_dir) / (mask.stem + ".jpg")
    if not frame.exists():
        return None
    fw, fh = jpeg_size(frame)
    x, y, w, h = crop_box(box, fw, fh)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".part.jpg")
    graph = (f"[1]scale={fw}:{fh}:flags=bilinear,format=gray,boxblur=2:1,format=gbrp[m];"
             f"[0]format=gbrp,split[f][g];[g]boxblur=20:2,eq=brightness=-0.32:saturation=0.3[bg];"
             f"[bg][f][m]maskedmerge,crop={w}:{h}:{x}:{y},scale={WIDTH}:-2,format=yuvj420p")
    r = subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(frame), "-i", str(mask),
                        "-filter_complex", graph, "-frames:v", "1", "-q:v", "3", str(tmp)], capture_output=True)
    if r.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        return None
    tmp.replace(out)
    return out


def part_mask_dir(frames_dir: Path, splat: Path) -> Path | None:
    """The masks of the object a '<stem>_part-<name>.ply' splat was split for."""
    from .masks import load_objects, masks_dir
    from .objects import slug
    m = re.search(r"_part-(.+)$", Path(splat).stem)
    if not m:
        return None
    want = m.group(1)
    for o in load_objects(frames_dir):
        s = slug(o["name"])
        if want == s or re.fullmatch(re.escape(s) + r"-\d+", want):
            d = masks_dir(frames_dir) / "objects" / str(o["id"])
            return d if d.is_dir() else None
    return None
