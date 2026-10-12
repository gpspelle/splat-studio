"""SAM 2 side of 'Choose what to keep'. Runs inside the SAM 2 environment (Python 3.10+,
PyTorch, sam2), never in the app's own Python: masks.py starts it as a separate process.

    python -m splat_studio.maskwork propagate JOB.json     # masks for every frame
    python -m splat_studio.maskwork serve CKPT CONFIG      # JSON lines on stdin: one-frame previews

Frames are processed in chunks so memory stays bounded on long videos (SAM 2 keeps every
frame of a chunk in memory at 1024x1024). Each chunk starts from the person's clicks on its
frames, or from the previous chunk's last mask, and runs forwards from the first clicked
frame and backwards from it. Several objects are propagated one after the other, each into
masks/objects/<id>/, and their union is written to masks/ (what training keeps).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

CHUNK = 64


def device():
    import torch
    if torch.cuda.is_available():
        return torch.device("cuda")
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def load_predictor(ckpt: str, config: str):
    from sam2.build_sam import build_sam2_video_predictor
    return build_sam2_video_predictor(config, ckpt, device=device())


def link_frames(paths: list[Path], tmp: Path) -> None:
    """SAM 2 reads a folder of <index>.jpg files."""
    for i, p in enumerate(paths):
        os.symlink(os.path.abspath(p), tmp / f"{i:05d}.jpg")


def frame_size(path: Path) -> tuple[int, int]:
    from PIL import Image
    with Image.open(path) as im:
        return im.size


def points_arrays(points: list[list], size: tuple[int, int]):
    import numpy as np
    w, h = size
    xy = np.array([[p[0] * w, p[1] * h] for p in points], dtype=np.float32)
    labels = np.array([int(p[2]) for p in points], dtype=np.int32)
    return xy, labels


def save_mask(mask_logits, out: Path) -> "object":
    import numpy as np
    from PIL import Image
    m = (mask_logits[0] > 0.0).cpu().numpy()
    m = m[0] if m.ndim == 3 else m
    Image.fromarray((m.astype(np.uint8) * 255)).save(out)
    return m


class Progress:
    """'Masking frames [k/N]' across every object."""
    def __init__(self, total: int):
        self.total, self.count, self.seen = total, 0, set()

    def tick(self, key) -> None:
        if key not in self.seen:
            self.seen.add(key)
            self.count += 1
            print(f"    Masking frames [{self.count}/{self.total}]", flush=True)


def run_chunk(pred, paths: list[Path], prompts: dict, carry, carry_at: int, reverse: bool, out: Path,
              progress: "Progress") -> "object":
    """Propagate through one chunk; returns the mask of the chunk's last processed frame."""
    import torch
    with tempfile.TemporaryDirectory() as td:
        link_frames(paths, Path(td))
        with torch.inference_mode():
            state = pred.init_state(video_path=td, offload_video_to_cpu=True)
            size = frame_size(paths[0])
            prompted = False
            for i, p in enumerate(paths):
                pts = prompts.get(p.name)
                if pts:
                    xy, lab = points_arrays(pts, size)
                    pred.add_new_points_or_box(state, frame_idx=i, obj_id=1, points=xy, labels=lab)
                    prompted = True
            if carry is not None and not prompts.get(paths[carry_at].name):
                pred.add_new_mask(state, frame_idx=carry_at, obj_id=1, mask=carry)
                prompted = True
            if not prompted:
                return None
            last = None
            start = len(paths) - 1 if reverse else 0
            for idx, _ids, logits in pred.propagate_in_video(state, start_frame_idx=start, reverse=reverse):
                last = save_mask(logits, out / (paths[idx].stem + ".png"))
                progress.tick((str(out), paths[idx].name))
            pred.reset_state(state)
            return last


def propagate_object(pred, frames: list[Path], prompts: dict, out: Path, progress: "Progress") -> None:
    """Masks for one object in every frame: forwards from its first clicked frame, then backwards."""
    names = [f.name for f in frames]
    # start from a frame where the object itself was clicked (not one with only "leave out" clicks)
    anchors = sorted(names.index(n) for n, pts in prompts.items() if n in names and any(p[2] == 1 for p in pts))
    if not anchors:
        sys.exit("no clicks on the object on any frame")
    out.mkdir(parents=True, exist_ok=True)
    a = anchors[0]
    carry, cur = None, a                                      # forwards from the first clicked frame
    while cur < len(frames):
        end = min(len(frames), cur + CHUNK)
        carry = run_chunk(pred, frames[cur:end], prompts, carry, 0, False, out, progress)
        if end >= len(frames):
            break
        cur = end - 1                                         # overlap one frame: the carried mask
    carry, cur = None, a                                      # backwards from it
    if a > 0:
        from PIL import Image
        import numpy as np
        carry = np.array(Image.open(out / (frames[a].stem + ".png"))) > 127
        while cur > 0:
            start = max(0, cur - CHUNK + 1)
            chunk = frames[start:cur + 1]
            carry = run_chunk(pred, chunk, prompts, carry, len(chunk) - 1, True, out, progress)
            cur = start


def write_union(frames: list[Path], dirs: list[Path], out: Path) -> None:
    import numpy as np
    from PIL import Image
    for f in frames:
        m = None
        for d in dirs:
            a = np.array(Image.open(d / (f.stem + ".png"))) > 127
            m = a if m is None else (m | a)
        Image.fromarray(m.astype(np.uint8) * 255).save(out / (f.stem + ".png"))


def propagate(job_file: str) -> None:
    job = json.loads(Path(job_file).read_text())
    frames = [Path(p) for p in job["frames"]]
    out = Path(job["out"])
    objects = job.get("objects") or {"1": job["prompts"]}    # older jobs: one object
    pred = load_predictor(job["checkpoint"], job["config"])
    print(f"    SAM 2 on {device()}", flush=True)
    segments = job.get("segments")                           # redo only these frames (after a few changed clicks)
    if segments is not None:
        progress = Progress(sum(hi - lo + 1 for v in segments.values() for lo, hi in v))
        multi = len(objects) > 1
        touched = set()
        for oid, ranges in sorted(segments.items(), key=lambda kv: int(kv[0])):
            d = out / "objects" / str(oid) if multi else out
            for lo, hi in ranges:
                propagate_object(pred, frames[lo:hi + 1], objects[str(oid)], d, progress)
                touched.update(range(lo, hi + 1))
        if multi:
            write_union([frames[i] for i in sorted(touched)],
                        [out / "objects" / str(k) for k in sorted(objects, key=int)], out)
        return
    progress = Progress(len(frames) * len(objects))
    if len(objects) == 1:
        propagate_object(pred, frames, next(iter(objects.values())), out, progress)
        return
    dirs = []
    for oid, prompts in sorted(objects.items(), key=lambda kv: int(kv[0])):
        d = out / "objects" / str(oid)
        propagate_object(pred, frames, prompts, d, progress)
        dirs.append(d)
    write_union(frames, dirs, out)


def pick_suggestions(masks: list[dict], limit: int = 8, min_area: float = 0.004, max_area: float = 0.5):
    """SAM 2 automatic masks -> distinct, object-sized ones. Larger masks come first, so a whole
    object wins over its parts (a part often scores higher than the whole); any mask mostly inside
    or over one already kept is skipped. Returns the kept dicts."""
    import numpy as np
    if not masks:
        return []
    total = masks[0]["segmentation"].size
    cands = [m for m in masks if min_area <= m["area"] / total <= max_area]
    cands.sort(key=lambda m: (m["area"], m.get("predicted_iou", 0) * m.get("stability_score", 1)), reverse=True)
    kept = []
    for m in cands:
        seg = m["segmentation"]
        if any(np.logical_and(seg, k["segmentation"]).sum() > 0.5 * min(seg.sum(), k["segmentation"].sum())
               for k in kept):
            continue
        kept.append(m)
        if len(kept) >= limit:
            break
    return kept


def inside_point(seg) -> tuple[float, float]:
    """A point inside the mask (fractions of the frame): its centre if that's inside, else the
    mask pixel nearest to it."""
    import numpy as np
    ys, xs = np.nonzero(seg)
    cy, cx = ys.mean(), xs.mean()
    if not seg[int(round(cy)), int(round(cx))]:
        i = int(np.argmin((ys - cy) ** 2 + (xs - cx) ** 2))
        cy, cx = ys[i], xs[i]
    h, w = seg.shape
    return round(float(cx) / w, 5), round(float(cy) / h, 5)


def suggest(gen, frame: Path, outdir: Path, limit: int) -> list[dict]:
    import numpy as np
    from PIL import Image
    img = np.array(Image.open(frame).convert("RGB"))
    out = []
    outdir.mkdir(parents=True, exist_ok=True)
    for i, m in enumerate(pick_suggestions(gen.generate(img), limit)):
        seg = m["segmentation"]
        p = outdir / f"{frame.stem}_s{i}.png"
        Image.fromarray(seg.astype(np.uint8) * 255).save(p)
        x, y = inside_point(seg)
        out.append({"x": x, "y": y, "area": round(float(seg.mean()), 4), "mask": str(p)})
    return out


def serve(ckpt: str, config: str) -> None:
    """One-frame previews: {"id", "frame", "points", "out"} per line -> {"id", "out"} or {"id", "error"}."""
    import torch
    pred = load_predictor(ckpt, config)
    gen = None                                                # the automatic mask generator, made on first use
    print(json.dumps({"ready": True, "device": str(device())}), flush=True)
    for line in sys.stdin:
        req = {}
        try:
            req = json.loads(line)
            if req.get("kind") == "suggest":                  # objects on one frame, for one-click selection
                if gen is None:
                    from sam2.automatic_mask_generator import SAM2AutomaticMaskGenerator
                    from sam2.build_sam import build_sam2
                    gen = SAM2AutomaticMaskGenerator(build_sam2(config, ckpt, device=device()), points_per_side=16,
                                                     pred_iou_thresh=0.8, stability_score_thresh=0.9)
                with torch.inference_mode():
                    sug = suggest(gen, Path(req["frame"]), Path(req["outdir"]), int(req.get("limit", 8)))
                print(json.dumps({"id": req.get("id"), "suggestions": sug}), flush=True)
                continue
            frame, out = Path(req["frame"]), Path(req["out"])
            with tempfile.TemporaryDirectory() as td, torch.inference_mode():
                link_frames([frame], Path(td))
                state = pred.init_state(video_path=td)
                xy, lab = points_arrays(req["points"], frame_size(frame))
                _, _, logits = pred.add_new_points_or_box(state, frame_idx=0, obj_id=1, points=xy, labels=lab)
                out.parent.mkdir(parents=True, exist_ok=True)
                save_mask(logits, out)
                pred.reset_state(state)
            print(json.dumps({"id": req.get("id"), "out": str(out)}), flush=True)
        except Exception as e:                                # report and keep serving
            print(json.dumps({"id": req.get("id") if isinstance(req, dict) else None,
                              "error": f"{type(e).__name__}: {e}"}), flush=True)


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "propagate":
        propagate(sys.argv[2])
    elif len(sys.argv) >= 4 and sys.argv[1] == "serve":
        serve(sys.argv[2], sys.argv[3])
    else:
        sys.exit("usage: maskwork propagate JOB.json | serve CHECKPOINT CONFIG")
