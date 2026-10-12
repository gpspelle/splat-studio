"""SAM 2 side of 'Choose what to keep'. Runs inside the SAM 2 environment (Python 3.10+,
PyTorch, sam2), never in the app's own Python: masks.py starts it as a separate process.

    python -m splat_studio.maskwork propagate JOB.json     # masks for every frame
    python -m splat_studio.maskwork serve CKPT CONFIG      # JSON lines on stdin: one-frame previews

Frames are processed in chunks so memory stays bounded on long videos (SAM 2 keeps every
frame of a chunk in memory at 1024x1024). Each chunk starts from the person's clicks on its
frames, or from the previous chunk's last mask, and runs forwards from the first clicked
frame and backwards from it.
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


def run_chunk(pred, paths: list[Path], prompts: dict, carry, carry_at: int, reverse: bool, out: Path,
              done: set, total: int) -> "object":
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
                if paths[idx].name not in done:
                    done.add(paths[idx].name)
                    print(f"    Masking frames [{len(done)}/{total}]", flush=True)
            pred.reset_state(state)
            return last


def propagate(job_file: str) -> None:
    job = json.loads(Path(job_file).read_text())
    frames = [Path(p) for p in job["frames"]]
    prompts, out = job["prompts"], Path(job["out"])
    names = [f.name for f in frames]
    anchors = sorted(names.index(n) for n in prompts if n in names)
    if not anchors:
        sys.exit("no clicks on any frame")
    pred = load_predictor(job["checkpoint"], job["config"])
    print(f"    SAM 2 on {device()}", flush=True)
    done: set = set()
    a = anchors[0]
    carry, cur = None, a                                      # forwards from the first clicked frame
    while cur < len(frames):
        end = min(len(frames), cur + CHUNK)
        carry = run_chunk(pred, frames[cur:end], prompts, carry, 0, False, out, done, len(frames))
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
            carry = run_chunk(pred, chunk, prompts, carry, len(chunk) - 1, True, out, done, len(frames))
            cur = start


def serve(ckpt: str, config: str) -> None:
    """One-frame previews: {"frame", "points", "out"} per line -> {"out"} or {"error"}."""
    import torch
    pred = load_predictor(ckpt, config)
    print(json.dumps({"ready": True, "device": str(device())}), flush=True)
    for line in sys.stdin:
        try:
            req = json.loads(line)
            frame, out = Path(req["frame"]), Path(req["out"])
            with tempfile.TemporaryDirectory() as td, torch.inference_mode():
                link_frames([frame], Path(td))
                state = pred.init_state(video_path=td)
                xy, lab = points_arrays(req["points"], frame_size(frame))
                _, _, logits = pred.add_new_points_or_box(state, frame_idx=0, obj_id=1, points=xy, labels=lab)
                out.parent.mkdir(parents=True, exist_ok=True)
                save_mask(logits, out)
                pred.reset_state(state)
            print(json.dumps({"out": str(out)}), flush=True)
        except Exception as e:                                # report and keep serving
            print(json.dumps({"error": f"{type(e).__name__}: {e}"}), flush=True)


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "propagate":
        propagate(sys.argv[2])
    elif len(sys.argv) >= 4 and sys.argv[1] == "serve":
        serve(sys.argv[2], sys.argv[3])
    else:
        sys.exit("usage: maskwork propagate JOB.json | serve CHECKPOINT CONFIG")
