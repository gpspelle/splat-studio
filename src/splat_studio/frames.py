"""Stage 1: extract frames from a walkaround video.

The whole video (or a --start/--end range) is sampled evenly so you get about --target
frames, the right amount for COLMAP on a laptop CPU. iPhone HDR video is detected and
converted to normal colours. With OpenCV, the video is sampled --oversample times more
densely and only the sharpest frame of each group is kept, then blurry frames are moved aside.

    python -m splat_studio.frames IMG_1234.MOV
    python -m splat_studio.frames IMG_1234.MOV --out frames/vase --target 250 --start 0:03 --end 1:45
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
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


def blurry_indices(scores: list[float], ratio: float) -> list[int]:
    """Indices of frames whose sharpness is below `ratio` x the median sharpness."""
    if not scores or ratio <= 0:
        return []
    median = sorted(scores)[len(scores) // 2]
    return [i for i, s in enumerate(scores) if s < median * ratio]


def sharpest_indices(scores: list[float], group: int) -> list[int]:
    """Index of the sharpest frame in each run of `group` consecutive frames."""
    if group <= 1:
        return list(range(len(scores)))
    return [max(range(i, min(i + group, len(scores))), key=scores.__getitem__)
            for i in range(0, len(scores), group)]


def has_cv2() -> bool:
    try:
        import cv2  # noqa: F401
        return True
    except ImportError:
        return False


def sharpness_scores(folder: Path) -> tuple[list[Path], list[float]]:
    """Variance of the Laplacian per frame (higher = sharper), on a 1280px copy for speed
    and so the score doesn't depend on the video's resolution."""
    import cv2
    frames, scores = [], []
    for f in sorted(folder.glob("*.jpg")):
        img = cv2.imread(str(f), cv2.IMREAD_GRAYSCALE)
        if img is None:
            continue
        scale = 1280 / max(img.shape[:2])
        if scale < 1:
            img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        frames.append(f)
        scores.append(cv2.Laplacian(img, cv2.CV_64F).var())
    return frames, scores


def keep_sharpest(folder: Path, group: int) -> int:
    """Delete all but the sharpest frame of every `group` consecutive frames. Returns how many remain."""
    frames, scores = sharpness_scores(folder)
    keep = set(sharpest_indices(scores, group))
    for i, f in enumerate(frames):
        if i not in keep:
            f.unlink()
    return len(keep)


def drop_blurry(folder: Path, ratio: float) -> int:
    """Move frames much blurrier than the median into folder/_blurry. Returns how many moved."""
    if not has_cv2():
        print("  (OpenCV not installed - skipping the blur filter)")
        return 0
    frames, scores = sharpness_scores(folder)
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
    ap.add_argument("--target", type=int, default=250, help="approximate number of frames (default 250)")
    ap.add_argument("--fps", type=float, help="fixed frames per second (overrides --target)")
    ap.add_argument("--start", default="0", help="skip the video before this time, e.g. 0:05")
    ap.add_argument("--end", help="stop at this time, e.g. 2:40 (default: end of video)")
    ap.add_argument("--hdr", choices=["auto", "on", "off"], default="auto",
                    help="convert HDR (e.g. iPhone) video to normal colours (default: auto-detect)")
    ap.add_argument("--blur-ratio", type=float, default=0.5,
                    help="move frames sharper than less than this fraction of the median to _blurry/ (0 = keep all)")
    ap.add_argument("--oversample", type=int, default=3,
                    help="sample N times more frames and keep the sharpest of each N (needs OpenCV; 1 = off)")
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
    fps = choose_fps(args.target, length, args.fps)
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

    group = args.oversample if args.oversample > 1 and has_cv2() else 1
    if group > 1:
        print(f"Sampling {round(fps * group, 3)} fps and keeping the sharpest frame of every {group}")
    extract(args.video, out_dir, round(fps * group, 3), start, length if args.end else None, name, tonemap)
    if group > 1:
        keep_sharpest(out_dir, group)
    # lets the UI map frame numbers back to video time, even when the frames are reused later.
    # Frame numbers count the sampled frames, so this is the sampling rate.
    save_extraction_info(out_dir, round(fps * group, 3), start, duration, str(args.video))
    total = len(list(out_dir.glob("*.jpg")))
    if args.blur_ratio > 0:
        moved = drop_blurry(out_dir, args.blur_ratio)
        print(f"Kept {total - moved} frames, moved {moved} blurry ones to {out_dir / '_blurry'}")
    else:
        print(f"Kept {total} frames")


if __name__ == "__main__":
    main()
