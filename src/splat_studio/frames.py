"""Stage 1: extract frames from a walkaround video.

The whole video (or a --start/--end range) is sampled evenly so you get about --target
frames, the right amount for COLMAP on a laptop CPU. iPhone HDR video is detected and
converted to normal colours, and blurry frames are moved aside (needs OpenCV).

    python -m splat_studio.frames IMG_1234.MOV
    python -m splat_studio.frames IMG_1234.MOV --out frames/vase --target 250 --start 0:03 --end 1:45
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .common import to_seconds
from .guidance import save_extraction_info

HDR_TRANSFERS = {"arib-std-b67", "smpte2084"}  # HLG (iPhone / Dolby Vision 8.4) and PQ

# HDR -> normal (SDR) colours, so JPEG frames don't come out grey and washed out.
TONEMAP = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
           "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p")


def video_duration(video: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        sys.exit(f"Could not read the video's duration:\n{r.stderr}")


def color_transfer(video: Path) -> str:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=color_transfer", "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
                       capture_output=True, text=True)
    return r.stdout.strip()


def has_zscale() -> bool:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True)
    return " zscale " in r.stdout


def choose_fps(target: int, length: float, fixed: float | None = None) -> float:
    """Frames per second that yields about `target` frames over `length` seconds."""
    if length <= 0:
        raise ValueError("length must be positive")
    return round(fixed if fixed else target / length, 3)


# "auto" frame count: about 1.75 frames per second of video used (a frame every ~0.6 s of a slow
# walk), so long walkarounds such as a car keep enough overlap; never below the old fixed 250
# (short videos run as before) and never above 600 (COLMAP time grows faster than the count).
AUTO_RATE = 1.75
AUTO_MIN, AUTO_MAX = 250, 600


def auto_target(length: float) -> int:
    """Frames to extract from `length` seconds of video when the count is "auto"."""
    return int(max(AUTO_MIN, min(AUTO_MAX, round(length * AUTO_RATE / 10) * 10)))


def parse_target(value) -> int | str:
    """A frame count: "auto" or a positive whole number (for argparse and the web API)."""
    if str(value).strip().lower() == "auto":
        return "auto"
    n = int(value)
    if n <= 0:
        raise ValueError("the frame count must be positive")
    return n


def frame_name(prefix: str) -> str:
    return f"{prefix}_%04d.jpg"


def extract(video: Path, out_dir: Path, fps: float, start: float, length: float | None,
            prefix: str, tonemap: bool) -> None:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if start > 0:
        cmd += ["-ss", str(start)]
    cmd += ["-i", str(video)]          # ffmpeg applies the iPhone's rotation metadata automatically
    if length:
        cmd += ["-t", str(length)]
    vf = f"fps={fps}" + (f",{TONEMAP}" if tonemap else "")
    cmd += ["-vf", vf, "-qscale:v", "2", str(out_dir / frame_name(prefix))]
    subprocess.run(cmd, check=True)


CANDIDATES = 3     # frames looked at around each chosen moment; the sharpest is kept (#50)


def sharpness(path: Path, cv2) -> float | None:
    """Laplacian variance of the image at half size (fast to read; only compared within one video)."""
    img = cv2.imread(str(path), cv2.IMREAD_REDUCED_GRAYSCALE_2)
    return None if img is None else float(cv2.Laplacian(img, cv2.CV_64F).var())


def pick_sharpest(scores: dict[int, list[tuple[float, object]]], ratio: float) -> dict:
    """For each moment (slot), the sharpest of its candidate frames, and whether even that one is
    blurry: under `ratio` x the median sharpness of all candidates. `scores`: slot -> [(score, item),
    ...] in time order (the first item is the frame at the moment itself). Returns {slot: {"item",
    "score", "blurry": bool, "replaced": bool}}; replaced = the moment's own frame was blurry and a
    sharper neighbour stands in for it."""
    every = sorted(sc for cands in scores.values() for sc, _ in cands)
    median = every[len(every) // 2] if every else 0.0
    limit = median * ratio
    out = {}
    for slot, cands in scores.items():
        best_score, best_item = max(cands, key=lambda c: c[0])
        out[slot] = {"item": best_item, "score": best_score, "blurry": ratio > 0 and best_score < limit,
                     "replaced": ratio > 0 and cands[0][0] < limit <= best_score}
    return out


def extract_sharpest(video: Path, out_dir: Path, fps: float, start: float, length: float | None, prefix: str,
                     tonemap: bool, ratio: float, k: int = CANDIDATES) -> dict:
    """Like `extract`, but looks at `k` frames around each moment and keeps the sharpest, so a
    blurry frame is replaced by a sharp neighbour instead of leaving a gap. Moments where every
    candidate is blurry go to out_dir/_blurry. Candidates are scored and deleted while ffmpeg
    runs, so at most a few spare frames are on disk at once. Needs OpenCV."""
    import cv2
    cand = out_dir / "_candidates"
    shutil.rmtree(cand, ignore_errors=True)
    cand.mkdir(parents=True)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if start > 0:
        cmd += ["-ss", str(start)]
    cmd += ["-i", str(video)]
    if length:
        cmd += ["-t", str(length)]
    vf = f"fps={fps * k}" + (f",{TONEMAP}" if tonemap else "")
    cmd += ["-vf", vf, "-qscale:v", "2", str(cand / "c_%06d.jpg")]
    proc = subprocess.Popen(cmd)
    scores: dict[int, list[tuple[float, Path]]] = {}
    best: dict[int, tuple[float, Path]] = {}
    done: set[str] = set()

    def settle(final: bool) -> None:
        files = sorted(f for f in cand.glob("c_*.jpg") if f.name not in done)
        if not final and files:
            files = files[:-1]                       # the newest may still be being written
        for f in files:
            done.add(f.name)
            n = int(f.stem[2:]) - 1
            slot = n // k
            sc = sharpness(f, cv2)
            if sc is None:
                f.unlink(missing_ok=True)
                continue
            scores.setdefault(slot, []).append((sc, f))
            if slot not in best or sc > best[slot][0]:
                if slot in best:
                    best[slot][1].unlink(missing_ok=True)
                best[slot] = (sc, f)
            else:
                f.unlink(missing_ok=True)

    while proc.poll() is None:
        settle(False)
        time.sleep(0.3)
    if proc.returncode != 0:
        shutil.rmtree(cand, ignore_errors=True)
        raise subprocess.CalledProcessError(proc.returncode, cmd)
    settle(True)
    picks = pick_sharpest({s: [(sc, f) for sc, f in c] for s, c in scores.items()}, ratio)
    blurry_dir = out_dir / "_blurry"
    kept, replaced, dropped = 0, 0, []
    for slot in sorted(picks):
        info = picks[slot]
        name = frame_name(prefix) % (slot + 1)          # numbered by moment, as before: frame N is at start + (N-1)/fps
        if info["blurry"]:
            blurry_dir.mkdir(exist_ok=True)
            shutil.move(str(info["item"]), blurry_dir / name)
            dropped.append(round(start + slot / fps, 1))
        else:
            shutil.move(str(info["item"]), out_dir / name)
            kept += 1
            replaced += info["replaced"]
    shutil.rmtree(cand, ignore_errors=True)
    return {"kept": kept, "replaced": replaced, "dropped": len(dropped), "dropped_at": dropped}


def clock(seconds: float) -> str:
    return f"{int(seconds // 60)}:{int(seconds % 60):02d}"


def blurry_indices(scores: list[float], ratio: float) -> list[int]:
    """Indices of frames whose sharpness is below `ratio` x the median sharpness."""
    if not scores or ratio <= 0:
        return []
    median = sorted(scores)[len(scores) // 2]
    return [i for i, s in enumerate(scores) if s < median * ratio]


def drop_blurry(folder: Path, ratio: float) -> int:
    """Move frames much blurrier than the median into folder/_blurry. Returns how many moved."""
    try:
        import cv2
    except ImportError:
        print("  (OpenCV not installed - skipping the blur filter)")
        return 0
    frames, scores = [], []
    for f in sorted(folder.glob("*.jpg")):
        img = cv2.imread(str(f), cv2.IMREAD_GRAYSCALE)
        if img is not None:
            frames.append(f)
            scores.append(cv2.Laplacian(img, cv2.CV_64F).var())
    bad = blurry_indices(scores, ratio)
    if bad:
        (folder / "_blurry").mkdir(exist_ok=True)
    for i in bad:
        shutil.move(str(frames[i]), folder / "_blurry" / frames[i].name)
    return len(bad)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video", type=Path)
    ap.add_argument("--out", type=Path, help="output folder (default: frames/<video name>)")
    ap.add_argument("--target", type=parse_target, default=250,
                    help="approximate number of frames, or 'auto' to choose from the video's length (default 250)")
    ap.add_argument("--fps", type=float, help="fixed frames per second (overrides --target)")
    ap.add_argument("--start", default="0", help="skip the video before this time, e.g. 0:05")
    ap.add_argument("--end", help="stop at this time, e.g. 2:40 (default: end of video)")
    ap.add_argument("--hdr", choices=["auto", "on", "off"], default="auto",
                    help="convert HDR (e.g. iPhone) video to normal colours (default: auto-detect)")
    ap.add_argument("--candidates", type=int, default=CANDIDATES,
                    help=f"frames looked at around each moment, keeping the sharpest (default {CANDIDATES}; 1 = off)")
    ap.add_argument("--blur-ratio", type=float, default=0.5,
                    help="move frames sharper than less than this fraction of the median to _blurry/ (0 = keep all)")
    args = ap.parse_args(argv)

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"{tool} not found on PATH (macOS: brew install ffmpeg)")
    if not args.video.exists():
        sys.exit(f"video not found: {args.video}")

    name = args.video.stem.replace(" ", "_")
    out_dir = args.out or Path("frames") / name
    if out_dir.exists() and any(out_dir.glob("*.jpg")):
        sys.exit(f"{out_dir} already has frames - delete it or choose another --out")
    out_dir.mkdir(parents=True, exist_ok=True)

    duration = video_duration(args.video)
    start = to_seconds(args.start)
    end = min(to_seconds(args.end), duration) if args.end else duration
    if end <= start:
        sys.exit(f"--end ({end:.1f}s) must be after --start ({start:.1f}s)")
    length = end - start
    target = auto_target(length) if args.target == "auto" else args.target
    if args.target == "auto" and not args.fps:
        print(f"Auto: {target} frames for {clock(length)} of video")
    fps = choose_fps(target, length, args.fps)
    # The two lines below are parsed by the web UI to map frame numbers back to video time.
    print(f"Video: {duration:.1f}s total, using {start:.1f}s -> {end:.1f}s ({length:.1f}s)")
    print(f"Extracting at {fps} fps -> about {int(fps * length)} frames into {out_dir}")
    if fps > 10:
        print("  note: that's a high rate for a short clip - neighbouring frames will be near-duplicates")

    tonemap = False
    if args.hdr != "off":
        hdr = args.hdr == "on" or color_transfer(args.video) in HDR_TRANSFERS
        if hdr and has_zscale():
            print("HDR video detected (typical for iPhone) - converting colours to normal SDR")
            tonemap = True
        elif hdr:
            print("HDR video detected, but this ffmpeg lacks the 'zscale' filter - frames may look washed out.\n"
                  "  Fix: brew reinstall ffmpeg, or record with HDR Video off (Settings > Camera > Record Video)")

    try:
        import cv2  # noqa: F401
        have_cv2 = True
    except ImportError:
        have_cv2 = False
    stats = {"length": round(length, 1), "target": target, "auto": args.target == "auto" and not args.fps}
    if have_cv2 and args.blur_ratio > 0 and args.candidates > 1:
        print(f"Looking at {args.candidates} frames around each moment and keeping the sharpest")
        r = extract_sharpest(args.video, out_dir, fps, start, length if args.end else None, name, tonemap,
                             args.blur_ratio, args.candidates)
        stats.update(r)
        if r["replaced"]:
            print(f"Replaced {r['replaced']} blurry frames with sharper ones next to them")
        if r["dropped"]:
            print(f"Too blurry to use around {', '.join(clock(t) for t in r['dropped_at'][:6])}"
                  + (" and more" if r["dropped"] > 6 else ""))
        print(f"Kept {r['kept']} frames, moved {r['dropped']} blurry ones to {out_dir / '_blurry'}")
    else:
        extract(args.video, out_dir, fps, start, length if args.end else None, name, tonemap)
        total = len(list(out_dir.glob("*.jpg")))
        moved = drop_blurry(out_dir, args.blur_ratio) if args.blur_ratio > 0 else 0
        stats.update(kept=total - moved, replaced=0, dropped=moved, dropped_at=[])
        print(f"Kept {total - moved} frames, moved {moved} blurry ones to {out_dir / '_blurry'}" if args.blur_ratio > 0
              else f"Kept {total} frames")
    # lets the UI map frame numbers back to video time, even when the frames are reused later
    save_extraction_info(out_dir, fps, start, duration, str(args.video), stats)
    print("Frame stats: " + json.dumps(stats, separators=(",", ":")))       # parsed by the web UI


if __name__ == "__main__":
    main()
