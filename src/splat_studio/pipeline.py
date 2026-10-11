"""The whole pipeline in one command: video -> frames -> camera poses -> trained splat.

    splat-studio run IMG_1234.MOV
    splat-studio run IMG_1234.MOV --quality preview
    splat-studio run --train-only workspace/IMG_1234/latest --quality high     # re-train, skip COLMAP
    splat-studio run IMG_1234.MOV -- --sh-degree 1                              # extra Brush options

Frames are extracted once per video (frames/<name>) and reused. Each run gets its own
folder workspace/<name>/run_<date_time>/ with dense/ (COLMAP result), splat/ (the .ply
files) and summary.txt. Paths are relative to the current folder (the data folder).
"""
from __future__ import annotations

import argparse
import datetime
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from . import brush as brushmod
from .colmap import new_run_dir
from .common import child_env, module_cmd, say
from .setup_deps import ensure_dependencies


def run(cmd: list, dry: bool) -> int:
    say("\n$ " + " ".join(shlex.quote(str(c)) for c in cmd))
    if dry:
        return 0
    return subprocess.run([str(c) for c in cmd], env=child_env()).returncode


def append_summary(ws: Path, lines: list[tuple[str, str]]) -> None:
    with open(ws / "summary.txt", "a") as f:
        for k, v in lines:
            f.write(f"{k + ':':<22} {v}\n")


def split_brush_args(argv: list[str]) -> tuple[list[str], list[str]]:
    """Everything after a bare `--` goes to Brush unchanged."""
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], argv[i + 1:]
    return argv, []


def brush_command(brush: Path | str, dense: Path, params: dict, quality: str, evaluate: bool,
                  viewer: bool, extra: list[str]) -> list:
    params = dict(params)
    params["--export-path"] = "./splat/"          # relative to the dataset's parent = the run folder
    params["--export-name"] = f"{quality}_{{iter}}.ply"
    if evaluate:
        params["--eval-split-every"] = 10
    cmd: list = [brush, dense]
    for k, v in params.items():
        cmd += [k, v]
    if viewer:
        cmd.append("--with-viewer")
    return cmd + list(extra)                       # user overrides come last and win


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="splat-studio run", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video", nargs="?", type=Path, help="video file (.mov / .mp4)")
    ap.add_argument("--train-only", type=Path, metavar="RUN_DIR",
                    help="skip frames and COLMAP; re-train Brush on an existing run folder")
    ap.add_argument("--quality", choices=brushmod.QUALITY, default="standard")
    ap.add_argument("--detail", choices=["auto", *brushmod.DETAIL], default="auto",
                    help="fine detail to grow splats for (default: judged from the reconstruction)")
    ap.add_argument("--frames", type=int, default=250, help="frames to extract (default 250)")
    ap.add_argument("--start", help="trim: start time, e.g. 0:03")
    ap.add_argument("--end", help="trim: end time, e.g. 1:45")
    ap.add_argument("--re-extract", action="store_true", help="extract frames again even if they exist")
    ap.add_argument("--max-size", type=int,
                    help="image size for COLMAP and training (default: 1600, or 1920 at high quality)")
    ap.add_argument("--eval", action="store_true", help="hold out every 10th frame and report PSNR/SSIM")
    ap.add_argument("--no-train", action="store_true", help="stop after COLMAP")
    ap.add_argument("--clean", action="store_true",
                    help="also save NAME_clean.ply without background fog (the original is kept)")
    ap.add_argument("--remove-background", action="store_true",
                    help="also save NAME_object.ply with only the object: no table, floor or room "
                         "(the original is kept)")
    ap.add_argument("--viewer", action="store_true", help="open the Brush window while training")
    ap.add_argument("--brush", help="path to the brush binary")
    ap.add_argument("--dry-run", action="store_true", help="print the commands without running them")
    ap.add_argument("--setup-only", action="store_true", help="only install dependencies and build Brush")
    ap.add_argument("--skip-setup", action="store_true", help="don't check / install dependencies")
    return ap


def main(argv: list[str] | None = None) -> None:
    argv, brush_extra = split_brush_args(list(sys.argv[1:] if argv is None else argv))
    ap = build_parser()
    args = ap.parse_args(argv)
    if not args.video and not args.train_only and not args.setup_only:
        ap.error("give a video, or --train-only RUN_DIR, or --setup-only")

    # Set everything up first, so a long COLMAP run is never followed by a missing tool.
    if not args.skip_setup and not os.environ.get("SPLAT_IN_CONTAINER"):
        ensure_dependencies(args.dry_run)
    brush = None if args.no_train and not args.setup_only else brushmod.ensure_brush(args.brush, args.dry_run)
    if args.setup_only:
        say(f"\nSetup complete. Brush: {brush}")
        return

    # ------------------------------------------------ 1 + 2: frames and COLMAP
    if args.train_only:
        ws = args.train_only.resolve()
        if not (ws / "dense" / "sparse" / "0").exists():
            sys.exit(f"{ws} has no dense/sparse/0 - run COLMAP for it first")
    else:
        if not args.video.exists():
            sys.exit(f"video not found: {args.video}")
        name = args.video.stem.replace(" ", "_")
        frames = Path("frames") / name
        say(f"==> 1/3 Frames: {frames}")
        have = len(list(frames.glob("*.jpg"))) if frames.exists() else 0
        if have and not args.re_extract:
            say(f"    reusing {have} frames extracted earlier (use --re-extract to redo)")
        else:
            if frames.exists() and args.re_extract and not args.dry_run:
                shutil.rmtree(frames)
            cmd = module_cmd("frames", args.video, "--out", frames, "--target", args.frames)
            if args.start:
                cmd += ["--start", args.start]
            if args.end:
                cmd += ["--end", args.end]
            if run(cmd, args.dry_run) != 0:
                sys.exit("Frame extraction failed.")

        # The undistorted COLMAP images are what Brush trains on, so they must be at least as
        # large as the quality's training resolution, or "high" silently trains at 1600px.
        max_size = args.max_size or max(1600, brushmod.QUALITY[args.quality]["res"])
        ws = new_run_dir(Path("workspace") / name).resolve()
        say(f"\n==> 2/3 Camera poses (COLMAP): {ws}")          # parsed by the web UI
        if run(module_cmd("colmap", frames, ws, "--step", 1, "--max-size", max_size), args.dry_run) != 0:
            sys.exit("COLMAP did not produce a usable model - see the messages above. Brush was not started.")
        if not args.dry_run:
            latest = ws.parent / "latest"
            try:
                if latest.is_symlink() or latest.exists():
                    latest.unlink()
                latest.symlink_to(ws.name)
            except OSError:
                pass

    # ------------------------------------------------ 3: Brush
    dense = ws / "dense"
    if args.no_train:
        say(f"\n==> Stopping before training (--no-train). Train later with:\n"
            f"    splat-studio run --train-only {os.path.relpath(ws.parent / 'latest')} --quality {args.quality}")
        return
    say("\n==> 3/3 Training the splat (Brush)")
    if args.dry_run and not dense.exists():
        n_img, n_pts, size = 250, None, args.max_size or max(1600, brushmod.QUALITY[args.quality]["res"])
        say("    (dry run: assuming 250 images)")
    else:
        n_img, n_pts = brushmod.model_stats(dense / "sparse" / "0")
        n_img = n_img or len(list((dense / "images").glob("*")))
        size = brushmod.image_size(dense)
    params, why = brushmod.choose_brush_params(n_img, n_pts, args.quality, args.detail, size)
    say(f"    {why}")
    cmd = brush_command(brush, dense, params, args.quality, args.eval, args.viewer, brush_extra)
    if not args.dry_run:
        append_summary(ws, [("brush settings", why), ("brush command", " ".join(shlex.quote(str(c)) for c in cmd))])
    started = datetime.datetime.now()
    if run(cmd, args.dry_run) != 0:
        sys.exit("Brush failed - see its output above.")
    if args.dry_run:
        return
    out_dir = ws / "splat"
    plys = sorted(out_dir.glob("*.ply"), key=lambda p: p.stat().st_mtime)
    took = str(datetime.datetime.now() - started).split(".")[0]
    if plys:
        append_summary(ws, [("splat", f"{plys[-1]}  (trained in {took})")])
        say(f"\nDone. Splat: {plys[-1]}")                         # parsed by the web UI
        if args.clean:
            say("\n==> Removing background fog (the original is kept)")
            from .splats import PlyError, clean
            try:
                s = clean(plys[-1], run_dir=ws)
                say(f"Kept {s['kept']:,} of {s['total']:,} splats ({s['removed_share']:.0%} removed)")
                say(f"Clean copy: {s['output']}")                 # parsed by the web UI
                append_summary(ws, [("clean splat", f"{s['output']}  (kept {s['kept']} of {s['total']})")])
            except (PlyError, OSError, ValueError) as e:
                say(f"    couldn't clean the splat: {e}")
        if args.remove_background:
            say("\n==> Removing the background (the original is kept)")
            from .splats import PlyError, isolate
            try:
                s = isolate(plys[-1], run_dir=ws)
                say(f"Kept {s['kept']:,} of {s['total']:,} splats ({s['removed_share']:.0%} removed"
                    f"{', support surface removed' if s['plane'] else ''})")
                say(f"Object copy: {s['output']}")                # parsed by the web UI
                append_summary(ws, [("object splat", f"{s['output']}  (kept {s['kept']} of {s['total']})")])
            except (PlyError, OSError, ValueError) as e:
                say(f"    couldn't remove the background: {e}")
    else:
        say(f"\nBrush finished but no .ply was found in {out_dir} - check its output above.")


if __name__ == "__main__":
    main()
