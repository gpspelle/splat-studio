"""Choose what to keep: per-frame masks of the object, from a few clicks.

The person clicks the object (keep) and things to drop (remove: a stand, a hand, a holder)
on one or more frames. With several objects (a table with three things on it), each click
says which object it is: object 1, 2, 3... Each object gets its own masks in
masks/objects/<id>/, and masks/<frame>.png is all of them together (what training keeps). A segmentation model follows that choice through every frame and
writes one mask per frame (white = keep, black = drop) to frames/<video>/masks/<frame>.png.
Training then makes everything outside the mask transparent, so only the chosen object is
in the splat ("transparent" mode), or just ignores it ("ignore" mode).

Backends
  sam2  Meta's SAM 2.1 video model. Needs PyTorch and Python 3.10+, so it runs in its own
        environment (~/.splat-studio/sam2, created by `splat-studio setup --masking`) through
        splat_studio/maskwork.py. Runs on the Mac's GPU (MPS) when it can.
  disc  A stand-in for tests: a disc around the "keep" clicks. No dependencies.

This module itself uses only the standard library.

    splat-studio mask frames/vase --prompts prompts.json      # writes frames/vase/masks/*.png
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import threading
import zlib
from pathlib import Path

from .common import TOOLS_DIR, child_env, say

SAM2_DIR = TOOLS_DIR / "sam2"
SAM2_CHECKPOINT = "sam2.1_hiera_small.pt"
SAM2_CONFIG = "configs/sam2.1/sam2.1_hiera_s.yaml"
SAM2_URL = "https://dl.fbaipublicfiles.com/segment_anything_2/092824/" + SAM2_CHECKPOINT
SAM2_REPO = "https://github.com/facebookresearch/sam2"
SAM2_REF = "2b90b9f5ceec907a1c18123530e92e794ad901a4"       # includes the fix for Apple GPUs (MPS)
MODES = ("transparent", "ignore")
MAX_OBJECTS = 8


# ---------------------------------------------------------------- where things live

def masks_dir(frames: Path) -> Path:
    return Path(frames) / "masks"


def frame_list(frames: Path) -> list[Path]:
    return sorted(Path(frames).glob("*.jpg"))


def load_prompts(frames: Path) -> dict:
    try:
        return json.loads((masks_dir(frames) / "prompts.json").read_text())
    except (OSError, ValueError):
        return {}


def load_objects(frames: Path) -> list[dict]:
    """[{id, name}] of the saved selection ([] if none)."""
    try:
        objs = json.loads((masks_dir(frames) / "objects.json").read_text())
        return [o for o in objs if isinstance(o, dict) and isinstance(o.get("id"), int)]
    except (OSError, ValueError, TypeError):
        return []


def split_prompt_file(data) -> tuple[dict, list]:
    """A prompts file is either {frame: points} or {"points": {frame: points}, "objects": [...]}."""
    if isinstance(data, dict) and isinstance(data.get("points"), dict):
        return data["points"], data.get("objects") or []
    return (data if isinstance(data, dict) else {}), []


def object_ids(prompts: dict) -> list[int]:
    """The objects that have at least one click."""
    return sorted({p[2] for pts in prompts.values() for p in pts if p[2] >= 1})


def normalise_objects(objects, ids: list[int]) -> list[dict]:
    """[{id, name}] for every clicked object; names default to "Object <id>"."""
    names = {}
    for o in objects or []:
        try:
            names[int(o["id"])] = " ".join(str(o.get("name") or "").split())[:40]
        except (TypeError, ValueError, KeyError):
            continue
    return [{"id": i, "name": names.get(i) or f"Object {i}"} for i in ids]


def points_for(prompts: dict, oid: int) -> dict[str, list[list]]:
    """One object's clicks as keep (1) / remove (0) points, frame by frame. On frames where
    it was clicked, the other objects' clicks also say "not this one". "Leave out" clicks
    count against every object, on frames where it was clicked or where no object was."""
    out = {}
    for name, pts in prompts.items():
        own = [[p[0], p[1], 1] for p in pts if p[2] == oid]
        anyone = any(p[2] >= 1 for p in pts)
        if own:
            out[name] = own + [[p[0], p[1], 0] for p in pts if p[2] != oid]
        elif not anyone:
            out[name] = [[p[0], p[1], 0] for p in pts]
    return out


def normalise_prompts(prompts: dict, names: list[str]) -> dict[str, list[list]]:
    """{frame name: [[x, y, label], ...]} with x, y as fractions of the frame (0..1); label 0
    leaves out, 1..MAX_OBJECTS is the object kept. Unknown frames and malformed points are
    dropped."""
    out: dict[str, list[list]] = {}
    known = set(names)
    for name, pts in (prompts or {}).items():
        if name not in known or not isinstance(pts, list):
            continue
        good = []
        for p in pts:
            try:
                x, y, lab = float(p[0]), float(p[1]), int(p[2])
            except (TypeError, ValueError, IndexError):
                continue
            if 0 <= x <= 1 and 0 <= y <= 1 and 0 <= lab <= MAX_OBJECTS:
                good.append([round(x, 5), round(y, 5), lab])
        if good:
            out[name] = good
    return out


def has_masks(frames: Path) -> bool:
    d = masks_dir(frames)
    return d.is_dir() and any(d.glob("*.png")) and (d / "done.json").exists()


# ---------------------------------------------------------------- tiny image helpers (stdlib)

def jpeg_size(path: Path) -> tuple[int, int]:
    """(width, height) from a JPEG's SOF header."""
    with open(path, "rb") as f:
        data = f.read(1 << 20)
    i = 2
    while i < len(data) - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    raise ValueError(f"not a JPEG (or no size found): {path}")


def write_png_gray(path: Path, w: int, h: int, rows: list[bytes]) -> None:
    """8-bit grayscale PNG."""
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + r for r in rows)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".part")
    tmp.write_bytes(png)
    tmp.replace(path)


# ---------------------------------------------------------------- backends

def sam2_python() -> Path | None:
    """The Python of the SAM 2 environment, if it is installed and complete."""
    env = os.environ.get("SPLAT_STUDIO_MASK_PYTHON")
    if env:
        return Path(env)
    py = SAM2_DIR / "venv" / "bin" / "python"
    return py if py.exists() and (SAM2_DIR / SAM2_CHECKPOINT).exists() else None


def backend() -> str | None:
    forced = os.environ.get("SPLAT_STUDIO_MASK_BACKEND")
    if forced in ("disc", "sam2"):
        return forced
    return "sam2" if sam2_python() else None


def disc_mask(w: int, h: int, points: list[list]) -> list[bytes]:
    """Stand-in segmentation: a disc around the keep clicks, reaching halfway to the nearest
    remove click (or 30% of the frame)."""
    keep = [(p[0] * w, p[1] * h) for p in points if p[2] == 1]
    drop = [(p[0] * w, p[1] * h) for p in points if p[2] == 0]
    if not keep:
        return [bytes(w)] * h
    cx, cy = sum(p[0] for p in keep) / len(keep), sum(p[1] for p in keep) / len(keep)
    r = 0.3 * min(w, h)
    if drop:
        r = min(r, 0.5 * min(math.dist((cx, cy), d) for d in drop))
    rows = []
    for y in range(h):
        dy = y - cy
        if abs(dy) >= r:
            rows.append(bytes(w))
            continue
        half = math.sqrt(r * r - dy * dy)
        x0, x1 = max(0, int(cx - half)), min(w, int(cx + half) + 1)
        rows.append(bytes(x0) + b"\xff" * (x1 - x0) + bytes(w - x1))
    return rows


def run_disc(frames: list[Path], objects: dict[int, dict], out: Path, progress=print,
             segments: dict[int, list] | None = None) -> None:
    """Disc backend, one object at a time: the latest prompted frame's clicks apply to every
    frame after it. With several objects, each goes to objects/<id>/ and the union to out/.
    With `segments` ({object: [(first, last), ...]} frame indexes), only those frames are redone."""
    multi = len(objects) > 1
    todo = {oid: (sorted({i for lo, hi in segments.get(oid, []) for i in range(lo, hi + 1)}) if segments is not None
                  else list(range(len(frames)))) for oid in objects}
    total, step = sum(len(v) for v in todo.values()), 0
    names = [f.name for f in frames]
    for oid, prompts in objects.items():
        dest = out / "objects" / str(oid) if multi else out
        order = sorted(prompts, key=names.index)
        for i in todo[oid]:
            f = frames[i]
            before = [n for n in order if n <= f.name] or order[:1]
            w, h = jpeg_size(f)
            rows = disc_mask(w, h, prompts[before[-1]]) if before else [bytes(w)] * h
            write_png_gray(dest / (f.stem + ".png"), w, h, rows)
            step += 1
            progress(f"    Masking frames [{step}/{total}]")
    if multi:
        write_union([frames[i] for i in sorted({i for v in todo.values() for i in v})],
                    [out / "objects" / str(oid) for oid in objects], out)


def write_union(frames: list[Path], dirs: list[Path], out: Path) -> None:
    """masks/<frame>.png = every object's mask together (what training keeps)."""
    for f in frames:
        u, w, h = None, 0, 0
        for d in dirs:
            w, h, rows = read_png_gray(d / (f.stem + ".png"))
            flat = b"".join(rows)
            u = bytearray(flat) if u is None else bytearray(a | b for a, b in zip(u, flat))
        write_png_gray(out / (f.stem + ".png"), w, h, [bytes(u[y * w:(y + 1) * w]) for y in range(h)])


def read_png_gray(path: Path) -> tuple[int, int, list[bytes]]:
    """(width, height, rows) of an 8-bit greyscale PNG (any row filter)."""
    data = Path(path).read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"not a PNG: {path}")
    w, h, depth, ctype = struct.unpack(">IIBB", data[16:26])
    if depth != 8 or ctype != 0:
        raise ValueError(f"{path.name}: only 8-bit greyscale masks are supported")
    i, idat = 8, b""
    while i < len(data):
        n = struct.unpack(">I", data[i:i + 4])[0]
        if data[i + 4:i + 8] == b"IDAT":
            idat += data[i + 8:i + 8 + n]
        i += 12 + n
    raw, rows, prev = zlib.decompress(idat), [], bytearray(w)
    for y in range(h):
        f, line = raw[y * (w + 1)], bytearray(raw[y * (w + 1) + 1:(y + 1) * (w + 1)])
        for x in range(w):
            a = line[x - 1] if x else 0
            b, c = prev[x], (prev[x - 1] if x else 0)
            if f == 1:
                line[x] = (line[x] + a) & 255
            elif f == 2:
                line[x] = (line[x] + b) & 255
            elif f == 3:
                line[x] = (line[x] + (a + b) // 2) & 255
            elif f == 4:
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        rows.append(bytes(line))
        prev = line
    return w, h, rows


def update_segments(names: list[str], prompts: dict, changed: list[str], ids: list[int]) -> dict[int, list[tuple[int, int]]]:
    """Which frames to redo after clicks changed on some frames: for each object clicked on a
    changed frame (every object, for a frame with only "leave out" clicks or none left), from
    the previous frame that object was clicked on to the next one. {object: [(first, last)]}."""
    idx = {n: i for i, n in enumerate(names)}
    segs: dict[int, list[tuple[int, int]]] = {}
    for c in changed:
        if c not in idx:
            continue
        ci = idx[c]
        hit = sorted({p[2] for p in prompts.get(c, []) if p[2] >= 1}) or list(ids)
        for oid in hit:
            anchors = sorted(idx[n] for n, ps in prompts.items() if n in idx and any(p[2] == oid for p in ps))
            if not anchors:
                continue
            lo = max([a for a in anchors if a < ci], default=0)
            hi = min([a for a in anchors if a > ci], default=len(names) - 1)
            segs.setdefault(oid, []).append((lo, hi))
    for oid, lst in segs.items():                      # merge overlapping ranges
        merged: list[tuple[int, int]] = []
        for lo, hi in sorted(lst):
            if merged and lo <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
            else:
                merged.append((lo, hi))
        segs[oid] = merged
    return segs


def run_sam2(frames: list[Path], objects: dict[int, dict], out: Path, python: Path,
             segments: dict[int, list] | None = None) -> int:
    """SAM 2 in its own environment; its progress lines go straight to our output."""
    job = out / "job.json"
    data = {"frames": [str(f) for f in frames], "objects": {str(k): v for k, v in objects.items()},
            "out": str(out), "checkpoint": str(SAM2_DIR / SAM2_CHECKPOINT), "config": SAM2_CONFIG}
    if segments is not None:
        data["segments"] = {str(k): [list(r) for r in v] for k, v in segments.items()}
    job.write_text(json.dumps(data))
    env = child_env({"PYTORCH_ENABLE_MPS_FALLBACK": "1"})
    return subprocess.run([str(python), "-u", "-m", "splat_studio.maskwork", "propagate", str(job)], env=env).returncode


DISC_SUGGESTIONS = ((0.25, 0.55), (0.5, 0.5), (0.75, 0.55))     # the stand-in backend's fixed "objects"


def suggest(frame: Path, outdir: Path, limit: int = 8) -> list[dict]:
    """Distinct objects on one frame, for one-click selection: [{x, y, area, mask}] with x, y a
    point inside the object (fractions of the frame), area as a fraction, mask a PNG path."""
    b = backend()
    if b is None:
        raise RuntimeError("the selection tool isn't installed (splat-studio setup --masking)")
    outdir.mkdir(parents=True, exist_ok=True)
    cache = outdir / f"{frame.stem}.json"                   # suggestions are made once per frame
    try:
        saved = json.loads(cache.read_text())
        if saved.get("frame_mtime") == frame.stat().st_mtime and all(Path(x["mask"]).exists() for x in saved["items"]):
            return [{**x, "mask": Path(x["mask"])} for x in saved["items"]][:limit]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    for old in outdir.glob(f"{frame.stem}_s*.png"):
        old.unlink()
    items = _suggest(b, frame, outdir, limit)
    cache.write_text(json.dumps({"frame_mtime": frame.stat().st_mtime,
                                 "items": [{**x, "mask": str(x["mask"])} for x in items]}))
    return items


def _suggest(b: str, frame: Path, outdir: Path, limit: int) -> list[dict]:
    if b == "disc":
        w, h = jpeg_size(frame)
        out = []
        for i, (x, y) in enumerate(DISC_SUGGESTIONS[:limit]):
            rows = disc_mask(w, h, [[x, y, 1], [min(1, x + 0.25), y, 0], [max(0, x - 0.25), y, 0]])
            p = outdir / f"{frame.stem}_s{i}.png"
            write_png_gray(p, w, h, rows)
            out.append({"x": x, "y": y, "area": round(sum(r.count(255) for r in rows) / (w * h), 4), "mask": p})
        return out
    return Sam2Server.get().suggest(frame, outdir, limit)


def preview_objects(frame: Path, points: list[list], outdir: Path) -> dict[int, Path]:
    """One mask per clicked object on this frame: {object id: png}."""
    res = {}
    for oid in object_ids({frame.name: points}):
        pts = points_for({frame.name: points}, oid)[frame.name]
        res[oid] = preview(frame, pts, outdir / f"{frame.stem}_{oid}.png")
    return res


def preview(frame: Path, points: list[list], out: Path) -> Path:
    """Mask for one frame from its clicks, as keep (1) / remove (0) points."""
    b = backend()
    if b is None:
        raise RuntimeError("the selection tool isn't installed (splat-studio setup --masking)")
    out.parent.mkdir(parents=True, exist_ok=True)
    if b == "disc":
        w, h = jpeg_size(frame)
        write_png_gray(out, w, h, disc_mask(w, h, points))
        return out
    return Sam2Server.get().preview(frame, points, out)


class Sam2Server:
    """One long-lived SAM 2 process for previews, so each click doesn't reload the model.

    The web server answers requests on several threads, so one request talks to the process
    at a time, and each reply carries the id of the request it answers: a reply is never
    handed to the wrong click (a frame's mask shown on another frame)."""
    _inst: "Sam2Server | None" = None
    _start = threading.Lock()

    def __init__(self, python: Path):
        env = child_env({"PYTORCH_ENABLE_MPS_FALLBACK": "1"})
        self.proc = subprocess.Popen([str(python), "-u", "-m", "splat_studio.maskwork", "serve",
                                      str(SAM2_DIR / SAM2_CHECKPOINT), SAM2_CONFIG],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
        self.lock = threading.Lock()
        self.next_id = 0

    @classmethod
    def get(cls) -> "Sam2Server":
        with cls._start:
            if cls._inst is None or cls._inst.proc.poll() is not None:
                py = sam2_python()
                if py is None:
                    raise RuntimeError("the selection tool isn't installed (splat-studio setup --masking)")
                cls._inst = cls(py)
            return cls._inst

    def suggest(self, frame: Path, outdir: Path, limit: int) -> list[dict]:
        reply = self._ask({"kind": "suggest", "frame": str(frame), "outdir": str(outdir), "limit": limit})
        return [{**s, "mask": Path(s["mask"])} for s in reply.get("suggestions", [])]

    def _ask(self, req: dict) -> dict:
        with self.lock:
            self.next_id += 1
            rid = self.next_id
            self.proc.stdin.write(json.dumps({"id": rid, **req}) + "\n")
            self.proc.stdin.flush()
            while True:
                line = self.proc.stdout.readline()
                if not line:
                    raise RuntimeError("the selection tool stopped unexpectedly")
                try:
                    reply = json.loads(line)
                except ValueError:
                    continue                         # library chatter
                if not isinstance(reply, dict) or reply.get("id") != rid:
                    continue                         # the "ready" line, or a reply to an older request
                if reply.get("error"):
                    raise RuntimeError(reply["error"])
                return reply

    def preview(self, frame: Path, points: list[list], out: Path) -> Path:
        return Path(self._ask({"frame": str(frame), "points": points, "out": str(out)})["out"])


# ---------------------------------------------------------------- making masks for every frame

PERSON_SCRIPT = Path(__file__).with_name("person_mask.swift")
PERSON_CLICKS = 6                     # frames clicked (at the centre) when SAM 2 finds the person


def person_backend() -> str | None:
    """How --person finds the person: Apple's Vision on a Mac (nothing to install), else SAM 2
    from a click at the centre of the frame (the person filmed stands there), else nothing."""
    forced = os.environ.get("SPLAT_STUDIO_PERSON_BACKEND")
    if forced in ("vision", "sam2", "disc"):
        return forced
    if sys.platform == "darwin" and shutil.which("swift"):
        return "vision"
    return backend()


def make_person_masks(frames_dir: Path, which: str | None = None) -> Path:
    """Masks of the person in every frame, without clicks: the object kept is "Person"."""
    frames_dir = Path(frames_dir)
    frames = frame_list(frames_dir)
    if not frames:
        raise ValueError(f"no frames in {frames_dir}")
    which = which or person_backend()
    if which is None:
        raise RuntimeError("no way to find the person: on a Mac install the Xcode command line tools "
                           "(xcode-select --install), elsewhere run: splat-studio setup --masking")
    objects = [{"id": 1, "name": "Person"}]
    if which != "vision":
        step = max(1, len(frames) // PERSON_CLICKS)
        return make_masks(frames_dir, {f.name: [[0.5, 0.5, 1]] for f in frames[::step]}, which, objects)
    out = masks_dir(frames_dir)
    if out.exists():
        for old in out.glob("*.png"):
            old.unlink()
        shutil.rmtree(out / "objects", ignore_errors=True)
        (out / "done.json").unlink(missing_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    (out / "prompts.json").write_text("{}")
    (out / "objects.json").write_text(json.dumps(objects, indent=1))
    say(f"==> Finding the person: {len(frames)} frames, Vision")
    r = subprocess.run(["swift", str(PERSON_SCRIPT), str(out), *map(str, frames)])
    missing = [f.name for f in frames if not (out / (f.stem + ".png")).exists()]
    if r.returncode != 0 or missing:
        raise RuntimeError(f"Vision found no person in {len(missing)} frame(s)" + (f", e.g. {missing[0]}" if missing else ""))
    (out / "done.json").write_text(json.dumps({"frames": len(frames), "backend": which, "objects": 1}))
    say(f"Masks: {out}")                                  # parsed by the web UI
    return out


def make_masks(frames_dir: Path, prompts: dict, which: str | None = None, objects: list | None = None) -> Path:
    frames_dir = Path(frames_dir)
    frames = frame_list(frames_dir)
    if not frames:
        raise ValueError(f"no frames in {frames_dir}")
    prompts = normalise_prompts(prompts, [f.name for f in frames])
    ids = object_ids(prompts)
    if not ids:
        raise ValueError("click the object at least once (a keep point)")
    objects = normalise_objects(objects, ids)
    which = which or backend()
    if which is None:
        raise RuntimeError("the selection tool isn't installed. Run: splat-studio setup --masking")
    out = masks_dir(frames_dir)
    if out.exists():
        for old in out.glob("*.png"):
            old.unlink()
        shutil.rmtree(out / "objects", ignore_errors=True)
        (out / "done.json").unlink(missing_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    (out / "prompts.json").write_text(json.dumps(prompts, indent=1))
    (out / "objects.json").write_text(json.dumps(objects, indent=1))
    per_object = {oid: points_for(prompts, oid) for oid in ids}
    say(f"==> Choosing what to keep: {len(frames)} frames, {len(ids)} object(s), "
        f"clicks on {len(prompts)} frame(s), {which}")
    if which == "disc":
        run_disc(frames, per_object, out)
    else:
        py = sam2_python()
        if py is None or run_sam2(frames, per_object, out, py) != 0:
            raise RuntimeError("SAM 2 failed - see its output above")
    dirs = [out] + ([out / "objects" / str(i) for i in ids] if len(ids) > 1 else [])
    missing = [f.name for d in dirs for f in frames if not (d / (f.stem + ".png")).exists()]
    if missing:
        raise RuntimeError(f"{len(missing)} frame(s) got no mask, e.g. {missing[0]}")
    (out / "done.json").write_text(json.dumps({"frames": len(frames), "backend": which, "objects": len(ids)}))
    say(f"Masks: {out}")                                  # parsed by the web UI
    return out


def can_update(frames_dir: Path, prompts: dict) -> bool:
    """A finished selection with the same objects: changed frames can be redone on their own."""
    names = [f.name for f in frame_list(frames_dir)]
    ids = object_ids(normalise_prompts(prompts, names))
    return bool(ids) and has_masks(frames_dir) and ([o["id"] for o in load_objects(frames_dir)] or [1]) == ids


def update_masks(frames_dir: Path, prompts: dict, changed: list[str], which: str | None = None,
                 objects: list | None = None) -> Path:
    """After clicks changed on a few frames, redo only the frames around them (see
    update_segments); every other frame keeps its mask. Needs a finished selection with the
    same objects; otherwise use make_masks."""
    frames_dir = Path(frames_dir)
    frames = frame_list(frames_dir)
    names = [f.name for f in frames]
    prompts = normalise_prompts(prompts, names)
    ids = object_ids(prompts)
    if not ids:
        raise ValueError("click the object at least once (a keep point)")
    if not can_update(frames_dir, prompts):
        raise ValueError("the objects changed since the last Apply: apply to all frames")
    objects = normalise_objects(objects, ids)
    which = which or backend()
    if which is None:
        raise RuntimeError("the selection tool isn't installed. Run: splat-studio setup --masking")
    changed = [c for c in dict.fromkeys(changed) if c in names]
    segs = update_segments(names, prompts, changed, ids)
    out = masks_dir(frames_dir)
    (out / "done.json").unlink(missing_ok=True)
    (out / "prompts.json").write_text(json.dumps(prompts, indent=1))
    (out / "objects.json").write_text(json.dumps(objects, indent=1))
    per_object = {oid: points_for(prompts, oid) for oid in ids}
    n = len({i for v in segs.values() for lo, hi in v for i in range(lo, hi + 1)})
    say(f"==> Updating the selection: {len(changed)} changed frame(s), {n} of {len(frames)} frames to redo, {which}")
    if segs:
        if which == "disc":
            run_disc(frames, per_object, out, segments=segs)
        else:
            py = sam2_python()
            if py is None or run_sam2(frames, per_object, out, py, segments=segs) != 0:
                raise RuntimeError("SAM 2 failed - see its output above")
    (out / "done.json").write_text(json.dumps({"frames": len(frames), "backend": which, "objects": len(ids),
                                               "updated": n}))
    say(f"Masks: {out}")                                  # parsed by the web UI
    return out


# ---------------------------------------------------------------- masks for training

def prepare_for_dense(frames_dir: Path, ws: Path, mode: str = "transparent") -> int:
    """Give Brush a mask for every training image: dense/masks/<image stem>.png.

    COLMAP resized the frames (ws/images) and undistorted them (ws/dense/images), so each mask
    goes through the same steps: resized to its image's size, then undistorted by COLMAP with
    the same camera model. Returns the number of masks written."""
    src, ws = masks_dir(frames_dir), Path(ws)
    dense, images = ws / "dense", ws / "images"
    if not (dense / "images").is_dir():
        raise ValueError(f"{dense} has no undistorted images yet")
    names = sorted(p.name for p in (dense / "images").iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png"))
    stage = ws / "mask_images"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir()
    for name in names:                               # 1. resize like the image (nearest: keep it binary)
        m = src / (Path(name).stem + ".png")
        if not m.exists():
            raise ValueError(f"no mask for {name}")
        w, h = jpeg_size(images / name)
        _ffmpeg(["-i", str(m), "-vf", f"scale={w}:{h}:flags=neighbor,format=gray", "-q:v", "1", str(stage / name)])
    # 2. undistort with COLMAP, exactly as the images were (into a separate folder)
    out = ws / "dense_masks"
    shutil.rmtree(out, ignore_errors=True)
    try:                                             # the distorted model COLMAP undistorted with
        sparse_in = Path((dense / "source_model.txt").read_text().strip())
    except OSError:
        raise ValueError("this run was made before masks were supported; run COLMAP again") from None
    r = subprocess.run(["colmap", "image_undistorter", "--image_path", str(stage), "--input_path", str(sparse_in),
                        "--output_path", str(out), "--output_type", "COLMAP"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if r.returncode != 0:
        raise RuntimeError("COLMAP couldn't undistort the masks:\n" + r.stderr[-1500:])
    dst = dense / "masks"
    shutil.rmtree(dst, ignore_errors=True)
    dst.mkdir()
    for name in names:                               # 3. back to clean black/white PNGs
        _ffmpeg(["-i", str(out / "images" / name), "-vf", "format=gray,lut='if(gt(val,127),255,0)'",
                 str(dst / (Path(name).stem + ".png"))])
    (dense / "masks.json").write_text(json.dumps({"mode": mode, "count": len(names)}))
    shutil.rmtree(stage, ignore_errors=True)
    shutil.rmtree(out, ignore_errors=True)
    return len(names)


def dense_mask_mode(dense: Path) -> str | None:
    """The mask mode a run was prepared with, or None if it trains on full frames."""
    try:
        mode = json.loads((Path(dense) / "masks.json").read_text()).get("mode")
    except (OSError, ValueError):
        return None
    return mode if mode in MODES and (Path(dense) / "masks").is_dir() else None


def _ffmpeg(args: list[str]) -> None:
    r = subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg failed on a mask: " + r.stderr[-800:])


# ---------------------------------------------------------------- command line

def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="splat-studio mask",
                                 description="Masks of the object for every frame, from a few clicks "
                                             "(see the 'Choose what to keep' page in the app).")
    ap.add_argument("frames", type=Path, help="frames folder, e.g. frames/vase")
    ap.add_argument("--prompts", type=Path, required=True,
                    help='JSON {"frame.jpg": [[x, y, 1], [x, y, 0]]}, x/y as 0..1 of the frame; 1 = keep, 0 = remove. '
                         'Several objects: labels 1, 2, 3..., and optionally {"points": {...}, '
                         '"objects": [{"id": 1, "name": "mug"}, ...]}')
    ap.add_argument("--backend", choices=["sam2", "disc"], help="default: SAM 2 if installed")
    a = ap.parse_args(argv)
    try:
        data = json.loads(a.prompts.read_text())
        points, objects = split_prompt_file(data)
        changed = (data.get("changed") if isinstance(data, dict) else None) or []
        if changed and can_update(a.frames, points):     # only the frames around changed clicks
            update_masks(a.frames, points, changed, a.backend, objects)
        else:
            make_masks(a.frames, points, a.backend, objects)
    except (OSError, ValueError, RuntimeError) as e:
        sys.exit(f"Couldn't make the masks: {e}")


if __name__ == "__main__":
    main()
