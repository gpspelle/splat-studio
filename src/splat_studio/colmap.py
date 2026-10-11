"""Stage 2: self-correcting camera-pose reconstruction with COLMAP (CPU).

Escalates automatically when the camera track breaks:
  1. sequential matching (fast)
  2. wider overlap + loop closure (first vs last frames) + relaxed mapper
  3. exhaustive matching (every frame vs every frame; skipped above 300 images)
  4./5. if still broken and --step > 1: attempts 1-2 again with every frame
It stops at the first attempt whose largest model registers >= 90% of frames, reports
where the track broke, and prepares an undistorted folder for Brush.

Every run gets its own folder (workspace/<frames name>/run_<date_time>/) with a
summary.txt, and a lock file stops two runs from sharing a folder.

    python -m splat_studio.colmap frames/vase --step 1
"""
from __future__ import annotations

import argparse
import atexit
import datetime
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from .common import fmt_time, say
from .progress import Progress, parse_progress

TARGET = 0.90                # stop escalating once the largest model registers this share
ACCEPT = 0.60                # below this, the result isn't worth training on
EXHAUSTIVE_MAX_IMAGES = 300  # exhaustive matching is O(n^2): too slow on a CPU beyond this
SEQ_OVERLAP = 15
WIDE_OVERLAP = 40
LOOP_N = 25


# ---------------------------------------------------------------- COLMAP process wrapper

class Colmap:
    def __init__(self, ws: Path, progress: Progress):
        self.ws, self.p = ws, progress
        self.log = ws / "colmap.log"
        if "FeatureExtraction.use_gpu" in self._help("feature_extractor"):   # COLMAP >= 3.12 names
            self.extract_gpu = ["--FeatureExtraction.use_gpu", "0"]
            self.match_gpu = ["--FeatureMatching.use_gpu", "0"]
        else:
            self.extract_gpu = ["--SiftExtraction.use_gpu", "0"]
            self.match_gpu = ["--SiftMatching.use_gpu", "0"]

    @staticmethod
    def _help(cmd: str) -> str:
        r = subprocess.run(["colmap", cmd, "-h"], capture_output=True, text=True)
        return r.stdout + r.stderr

    def run(self, *args, label=None, kind=None, units=0, n_images=0) -> None:
        """Run a COLMAP command, logging everything and driving the progress bar from its output."""
        args = [str(a) for a in args]
        tracked = label is not None
        if tracked:
            self.p.start(label, kind, units)
        with open(self.log, "a") as log:
            log.write(f"\n$ colmap {' '.join(args)}\n")
            log.flush()
            proc = subprocess.Popen(["colmap", *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, bufsize=1, errors="replace")
            lines: queue.Queue = queue.Queue()

            def pump():
                for l in proc.stdout:
                    lines.put(l)
                lines.put(None)

            threading.Thread(target=pump, daemon=True).start()
            while True:
                try:
                    line = lines.get(timeout=0.5)
                except queue.Empty:
                    if tracked:
                        self.p.draw()          # keep the clock moving while COLMAP is quiet
                    continue
                if line is None:
                    break
                log.write(line)
                if tracked:
                    self.p.line(line)
                    self.p.update(parse_progress(line, kind, n_images))
            proc.wait()
        if tracked:
            self.p.finish()
        if proc.returncode != 0:
            tail = self.log.read_text().splitlines()[-25:]
            sys.exit(f"\nCOLMAP failed: colmap {args[0]}\n" + "\n".join(tail))


# ---------------------------------------------------------------- matching pairs (for estimates)

def sequential_pairs(n: int, overlap: int, quadratic: bool) -> set[tuple[int, int]]:
    """Image pairs COLMAP's sequential matcher compares (as index pairs i < j)."""
    pairs = set()
    for i in range(n):
        for o in range(1, overlap + 1):
            if i + o < n:
                pairs.add((i, i + o))
        if quadratic:
            for k in range(overlap):
                j = i + (1 << k)
                if j >= n:
                    break
                pairs.add((i, j))
    return pairs


def loop_pairs(n: int, k: int = LOOP_N) -> set[tuple[int, int]]:
    """First k frames x last k frames: closes the loop of a walkaround."""
    return {(min(a, b), max(a, b)) for a in range(min(k, n)) for b in range(max(n - k, 0), n) if a != b}


def exhaustive_pairs(n: int) -> set[tuple[int, int]]:
    return {(i, j) for i in range(n) for j in range(i + 1, n)}


def build_plan(n_frames: int, step: int) -> list[tuple]:
    """Every attempt that could run, in order: (step, n_images, name, relaxed, pair_kinds)."""
    plan = []
    for s in [step] + ([1] if step > 1 else []):
        n = math.ceil(n_frames / s)
        plan.append((s, n, "sequential matching", False, ["seq"]))
        plan.append((s, n, "wider overlap + loop closure, relaxed mapper", True, ["wide", "loop"]))
        if n <= EXHAUSTIVE_MAX_IMAGES:
            plan.append((s, n, "exhaustive matching, relaxed mapper", True, ["all"]))
    return plan


def pair_set(kind: str, n: int) -> set[tuple[int, int]]:
    return {"seq": lambda: sequential_pairs(n, SEQ_OVERLAP, False),
            "wide": lambda: sequential_pairs(n, WIDE_OVERLAP, True),
            "loop": lambda: loop_pairs(n),
            "all": lambda: exhaustive_pairs(n)}[kind]()


def plan_costs(plan: list[tuple]) -> list[dict]:
    """Units of work per attempt. Matches accumulate in COLMAP's database, so each matcher
    only pays for pairs not matched by an earlier attempt of the same round."""
    costs, matched, prev_step = [], set(), None
    for s, n, _, _, kinds in plan:
        new_round = s != prev_step
        if new_round:
            matched = set()            # a new round starts with a fresh database
        prev_step = s
        match_units = []
        for k in kinds:
            ps = pair_set(k, n)
            match_units.append(("match_exhaustive" if k == "all" else "match", len(ps - matched)))
            matched |= ps
        costs.append({"extract": n if new_round else 0, "match": match_units, "map": n, "n": n})
    return costs


def remaining_time(p: Progress, costs: list[dict], attempt: int, phase: str, match_idx: int) -> tuple[float, float]:
    """(time left if this attempt succeeds, worst case) - excluding the step in progress."""
    if phase == "undistort":
        return 0.0, 0.0
    cost = costs[attempt]
    rest = 0.0
    if phase in ("prepare", "extract"):
        rest += sum(p.estimate(k, u) for k, u in cost["match"]) + p.estimate("map", cost["map"])
    elif phase == "match":
        rest += sum(p.estimate(k, u) for k, u in cost["match"][match_idx + 1:]) + p.estimate("map", cost["map"])
    if_success = rest + p.estimate("undistort", cost["n"])
    worst = rest
    for later in costs[attempt + 1:]:
        worst += (p.estimate("extract", later["extract"]) + sum(p.estimate(k, u) for k, u in later["match"])
                  + p.estimate("map", later["map"]))
    worst += p.estimate("undistort", costs[-1]["n"])
    return if_success, worst


# ---------------------------------------------------------------- image + model helpers

def resize_images(paths: list[Path], max_size: int) -> None:
    """Shrink images in place so the longest side is max_size (never enlarges).
    macOS: sips. Elsewhere: OpenCV, else ffmpeg."""
    if not paths:
        return
    if shutil.which("sips"):
        subprocess.run(["sips", "-Z", str(max_size), *map(str, paths)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return
    try:
        import cv2
        for p in paths:
            img = cv2.imread(str(p))
            if img is None:
                continue
            h, w = img.shape[:2]
            scale = max_size / max(h, w)
            if scale < 1:
                img = cv2.resize(img, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)
                cv2.imwrite(str(p), img, [cv2.IMWRITE_JPEG_QUALITY, 95])
        return
    except ImportError:
        pass
    if shutil.which("ffmpeg"):
        flt = f"scale='if(gt(iw,ih),min({max_size},iw),-2)':'if(gt(iw,ih),-2,min({max_size},ih))'"
        for p in paths:
            tmp = p.with_suffix(".tmp.jpg")
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(p), "-vf", flt, "-qscale:v", "2",
                            str(tmp)], check=True)
            tmp.replace(p)
        return
    say("    (no sips, OpenCV or ffmpeg found - images not resized)")


def prepare_images(src: Path, ws: Path, step: int, max_size: int) -> list[str]:
    img_dir = ws / "images"
    if img_dir.exists():
        shutil.rmtree(img_dir)
    img_dir.mkdir(parents=True)
    frames = sorted(src.glob("*.jpg"))
    if not frames:
        sys.exit(f"No .jpg frames found in {src}")
    for f in frames[::step]:
        shutil.copy2(f, img_dir / f.name)
    resize_images(sorted(img_dir.glob("*.jpg")), max_size)
    return sorted(p.name for p in img_dir.glob("*.jpg"))


def parse_images_txt(path: Path) -> set[str]:
    """Registered image names from COLMAP's images.txt. It alternates an image line
    (ending with the name) and a line of 2D points, which can be empty."""
    lines = [l for l in path.read_text().splitlines() if not l.startswith("#")]
    return {lines[i].split()[-1] for i in range(0, len(lines), 2) if lines[i].strip()}


def find_gaps(all_names: list[str], registered: set[str]) -> list[tuple[str, str, int]]:
    """Runs of consecutive unregistered frames: [(first, last, count)]."""
    gaps, start, count, last = [], None, 0, None
    for name in all_names + [None]:
        if name is not None and name not in registered:
            if start is None:
                start, count = name, 0
            count += 1
            last = name
        elif start is not None:
            gaps.append((start, last, count))
            start = None
    return gaps


def reprojection_error(model_dir: Path) -> float | None:
    r = subprocess.run(["colmap", "model_analyzer", "--path", str(model_dir)], capture_output=True, text=True)
    m = re.search(r"Mean reprojection error:\s*([\d.]+)", r.stdout + r.stderr)
    return float(m.group(1)) if m else None


# ---------------------------------------------------------------- COLMAP steps

def extract_features(c: Colmap, n: int) -> None:
    db = c.ws / "database.db"
    db.unlink(missing_ok=True)
    c.run("feature_extractor", "--database_path", db, "--image_path", c.ws / "images",
          "--ImageReader.single_camera", "1", "--ImageReader.camera_model", "OPENCV", *c.extract_gpu,
          label="extracting features", kind="extract", units=n)


def match_sequential(c: Colmap, overlap: int, quadratic: bool, new_pairs: int) -> None:
    c.run("sequential_matcher", "--database_path", c.ws / "database.db",
          "--SequentialMatching.overlap", overlap, "--SequentialMatching.quadratic_overlap", int(quadratic),
          *c.match_gpu, label="matching (sequential)", kind="match", units=new_pairs)


def match_loop_closure(c: Colmap, names: list[str], new_pairs: int) -> None:
    head, tail = names[:LOOP_N], names[-LOOP_N:]
    pairs = c.ws / "loop_pairs.txt"
    pairs.write_text("\n".join(f"{a} {b}" for a in head for b in tail if a != b) + "\n")
    c.run("matches_importer", "--database_path", c.ws / "database.db", "--match_list_path", pairs,
          "--match_type", "pairs", *c.match_gpu, label="matching (loop closure)", kind="match", units=new_pairs)


def match_exhaustive(c: Colmap, new_pairs: int) -> None:
    c.run("exhaustive_matcher", "--database_path", c.ws / "database.db", *c.match_gpu,
          label="matching (exhaustive)", kind="match_exhaustive", units=new_pairs)


def map_poses(c: Colmap, out: Path, relaxed: bool, n: int) -> None:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    extra = (["--Mapper.init_min_num_inliers", "50", "--Mapper.abs_pose_min_num_inliers", "15",
              "--Mapper.abs_pose_min_inlier_ratio", "0.15"] if relaxed else [])
    c.run("mapper", "--database_path", c.ws / "database.db", "--image_path", c.ws / "images",
          "--output_path", out, *extra, label="reconstructing poses", kind="map", units=n, n_images=n)


def read_models(c: Colmap, sparse: Path) -> list[tuple[Path, set[str]]]:
    models = []
    for d in sorted(p for p in sparse.iterdir() if p.is_dir() and p.name.isdigit()):
        txt = sparse / f"_txt_{d.name}"
        txt.mkdir(exist_ok=True)
        c.run("model_converter", "--input_path", d, "--output_path", txt, "--output_type", "TXT")
        models.append((d, parse_images_txt(txt / "images.txt")))
    return models


def finalize(c: Colmap, model_dir: Path, n: int) -> Path:
    dense = c.ws / "dense"
    if dense.exists():
        shutil.rmtree(dense)
    c.run("image_undistorter", "--image_path", c.ws / "images", "--input_path", model_dir,
          "--output_path", dense, "--output_type", "COLMAP", label="undistorting images", kind="undistort", units=n)
    sparse0 = dense / "sparse" / "0"
    sparse0.mkdir(parents=True, exist_ok=True)
    for f in (dense / "sparse").glob("*.bin"):
        shutil.move(str(f), sparse0 / f.name)
    return dense


# ---------------------------------------------------------------- workspace safety + summary

def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class WorkspaceError(Exception):
    pass


def claim_workspace(ws: Path, overwrite: bool = False) -> Path:
    """Create ws and lock it to this process. Refuses if another live run holds it or it
    already contains a run (unless overwrite). Stale locks from crashed runs are cleared."""
    lock = ws / ".lock"
    if lock.exists():
        try:
            pid = int(lock.read_text().strip())
        except ValueError:
            pid = None
        if pid and pid != os.getpid() and pid_alive(pid):
            raise WorkspaceError(f"{ws} is in use by another run (process {pid}).\n"
                                 f"Wait for it, stop it (kill {pid}), or let the tool pick a new folder.")
        lock.unlink()
    if ws.exists() and any(ws.iterdir()):
        if not overwrite:
            raise WorkspaceError(f"{ws} already contains a previous run.\n"
                                 "Leave out the workspace argument to get a new folder, or add --overwrite.")
        shutil.rmtree(ws)
    ws.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(os.getpid()))
    atexit.register(lambda: lock.unlink(missing_ok=True))
    return lock


def new_run_dir(parent: Path) -> Path:
    """parent/run_<date_time>, with _2, _3... if a run already started in the same second."""
    base = parent / f"run_{datetime.datetime.now():%Y%m%d_%H%M%S}"
    ws, i = base, 2
    while ws.exists():
        ws = base.with_name(f"{base.name}_{i}")
        i += 1
    return ws


def point_latest(ws: Path) -> None:
    """workspace/<name>/latest -> newest run folder."""
    latest = ws.parent / "latest"
    try:
        if latest.is_symlink() or latest.exists():
            latest.unlink()
        latest.symlink_to(ws.name)
    except OSError:
        pass


class Summary:
    """summary.txt: one `key: value` line per fact, rewritten after every addition."""

    def __init__(self, ws: Path):
        self.path = ws / "summary.txt"
        self.lines: list[str] = []

    def add(self, key: str, value) -> None:
        self.lines.append(f"{key + ':':<22} {value}")
        self.path.write_text("\n".join(self.lines) + "\n")


# ---------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", type=Path, help="folder with extracted .jpg frames")
    ap.add_argument("ws", type=Path, nargs="?", help="workspace folder (default: workspace/<name>/run_<date_time>)")
    ap.add_argument("--overwrite", action="store_true", help="replace an existing workspace folder")
    ap.add_argument("--step", type=int, default=2, help="keep every Nth frame (default 2)")
    ap.add_argument("--max-size", type=int, default=1600, help="longest image side in px (default 1600)")
    args = ap.parse_args(argv)

    if not shutil.which("colmap"):
        sys.exit("colmap not found on PATH (macOS: brew install colmap)")
    n_frames = len(list(args.src.glob("*.jpg")))
    if not n_frames:
        sys.exit(f"No .jpg frames found in {args.src}")
    auto_named = args.ws is None
    if auto_named:
        args.ws = new_run_dir(Path("workspace") / args.src.name)
    try:
        claim_workspace(args.ws, args.overwrite)
    except WorkspaceError as e:
        sys.exit(str(e))
    if auto_named:
        point_latest(args.ws)
    say(f"Workspace: {args.ws}")

    summary = Summary(args.ws)
    summary.add("started", f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
    summary.add("frames", f"{args.src} ({n_frames} jpg)")
    summary.add("settings", f"--step {args.step} --max-size {args.max_size}")

    p = Progress(args.max_size)
    c = Colmap(args.ws, p)
    plan = build_plan(n_frames, args.step)
    costs = plan_costs(plan)
    say(f"Full COLMAP log: {c.log}")
    say(f"Time estimates use speeds measured in earlier runs ({p.rates_file})" if p.saved else
        "First run on this computer: time estimates are rough (~) until matching and reconstruction are timed")
    say(f"Up to {len(plan)} attempt(s) planned; stops at the first that registers >= {TARGET:.0%} of frames.")

    state = {"attempt": 0, "phase": "prepare", "match_idx": 0}
    p.plan_left = lambda: remaining_time(p, costs, state["attempt"], state["phase"], state["match_idx"])

    best, names, current_step = None, [], None
    for idx, (s, n, label, relaxed, kinds) in enumerate(plan):
        state.update(attempt=idx, phase="prepare", match_idx=0)
        if s != current_step:
            if current_step is not None:
                say("\n==> Still fragmented - retrying with every frame to bridge the gaps")
            say(f"\n==> Preparing images (every {s} frame(s), max {args.max_size}px)")
            names = prepare_images(args.src, args.ws, s, args.max_size)
            say(f"    {len(names)} images")
            state["phase"] = "extract"
            extract_features(c, len(names))
            current_step = s

        say(f"\n==> Attempt {idx + 1}/{len(plan)}: {label}")
        state["phase"] = "match"
        for mi, k in enumerate(kinds):
            state["match_idx"] = mi
            units = costs[idx]["match"][mi][1]
            if k == "seq":
                match_sequential(c, SEQ_OVERLAP, False, units)
            elif k == "wide":
                match_sequential(c, WIDE_OVERLAP, True, units)
            elif k == "loop":
                match_loop_closure(c, names, units)
            else:
                match_exhaustive(c, units)

        state["phase"] = "map"
        sparse = args.ws / f"sparse_try{idx + 1}"
        map_poses(c, sparse, relaxed, len(names))
        models = read_models(c, sparse)
        if not models:
            say("    no model could be built")
            continue
        model_dir, registered = max(models, key=lambda m: len(m[1]))
        ratio = len(registered) / len(names)
        say(f"    {len(models)} model(s); largest registers {len(registered)}/{len(names)} frames ({ratio:.0%})")
        summary.add(f"attempt {idx + 1}",
                    f"{label}: {len(models)} model(s), largest {len(registered)}/{len(names)} ({ratio:.0%})")
        if best is None or ratio > best[0]:
            best = (ratio, model_dir, registered, list(names), label)
        if ratio >= TARGET:
            if len(models) > 1:
                say(f"    (ignoring {len(models) - 1} extra small model(s) - the largest already covers "
                    f"{ratio:.0%} of frames, and only it is used for training)")
            break

    if best is None:
        summary.add("result", "FAILED - no model")
        sys.exit("\nCOLMAP could not reconstruct anything. The footage may be too blurry or too reflective.")

    ratio, model_dir, registered, best_names, label = best
    err = reprojection_error(model_dir)
    err_txt = f", mean reprojection error {err:.2f}px" if err is not None else ""
    say(f"\n==> Best result: {ratio:.0%} of frames registered ({label}){err_txt}")
    summary.add("best", f"{model_dir.relative_to(args.ws)}  {len(registered)}/{len(best_names)} ({ratio:.0%}){err_txt}")
    gaps = find_gaps(best_names, registered)
    if gaps:
        say("    Track breaks (unregistered frames):")       # parsed by the web UI
        for first, last, count in gaps:
            say(f"      {first} -> {last}  ({count} frames)")
            summary.add("track break", f"{first} -> {last} ({count} frames)")

    if ratio < ACCEPT:
        say("\nNot good enough to train on. Likely causes: cuts, fast pans, people blocking the object,\n"
            "or reflective / textureless surfaces. See the recording tips and record again.")
        summary.add("result", f"NOT GOOD ENOUGH (< {ACCEPT:.0%})")
        summary.add("duration", fmt_time(time.time() - p.t0))
        sys.exit(1)

    say("\n==> Undistorting images for splat training")
    state["phase"] = "undistort"
    dense = finalize(c, model_dir, len(registered))
    if ratio < TARGET:
        say(f"\nUsable but incomplete ({ratio:.0%}). Missing frames mean thinner coverage in those areas.")
    summary.add("result", f"OK - train on {dense}")
    summary.add("duration", fmt_time(time.time() - p.t0))
    say(f"\nDone in {fmt_time(time.time() - p.t0)}. Load this folder in Brush:  {dense}")
    say(f"Summary: {summary.path}")


if __name__ == "__main__":
    main()
