"""The whole pipeline in one command: video -> frames -> camera poses -> trained splat.

    splat-studio run IMG_1234.MOV
    splat-studio run IMG_1234.MOV --quality preview
    splat-studio run --train-only workspace/IMG_1234/latest --quality high     # re-train, skip COLMAP
    splat-studio run IMG_1234.MOV -- --sh-degree 1                              # extra Brush options
    splat-studio run capture_2026-10-14_153012.zip       # an iPhone capture: ARKit's poses, no COLMAP

Frames are extracted once per video (frames/<name>) and reused. Each run gets its own
folder workspace/<name>/run_<date_time>/ with dense/ (COLMAP result), splat/ (the .ply
files) and summary.txt. Every run also saves NAME_clean.ply, the splat without background
fog; the original is kept next to it. Paths are relative to the current folder (the data folder).
"""
from __future__ import annotations

import argparse
import datetime
import json
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


STOPPED_EXIT = 3        # exit code of a run stopped before training (--stop-on-gaps)


def gap_problems(ws: Path) -> list[str]:
    """Sides nobody filmed, from the run's coverage.json: around the whole scene, and around each
    chosen object. Only a missing side counts: filming from one height is a warning, never a stop,
    the underside of something standing on a surface is never measured as missing, and a side
    something stands in front of (a wall, another object) is excused (coverage.excuse_blocked)."""
    from .coverage import open_gap, problems
    try:
        cov = json.loads((ws / "coverage.json").read_text())
    except (OSError, ValueError):
        return []
    out = []
    if "gap" in problems(cov):
        out.append(f"{open_gap(cov)['degrees']:.0f} degrees around the object weren't filmed")
    for o in cov.get("objects", []):
        if "gap" in problems(o):
            out.append(f"{open_gap(o)['degrees']:.0f} degrees around {o['name']} weren't filmed")
    return out


def frames_of_run(ws: Path) -> Path | None:
    """The frames folder a run was made from (its summary's `frames:` line), or None."""
    try:
        line = next(l for l in (ws / "summary.txt").read_text().splitlines() if l.startswith("frames:"))
    except (OSError, StopIteration):
        return None
    rel = Path(line.split(":", 1)[1].strip().rsplit(" (", 1)[0])
    for base in (Path.cwd(), ws.parent.parent.parent):         # the run's cwd: the data folder
        d = rel if rel.is_absolute() else base / rel
        if d.is_dir():
            return d
    return None


def is_capture_arg(path: Path | None) -> bool:
    if path is None or not path.exists():
        return False
    from .capture import is_capture
    return is_capture(path)


def import_capture(args) -> tuple[Path | None, Path]:
    """Stages 1 and 2 for an iPhone capture: frames from its video, poses from ARKit (and, with
    --poses refine or colmap, from COLMAP). Returns (run folder, frames folder)."""
    from . import capture
    from .colmap import Summary, WorkspaceError, claim_workspace, point_latest
    src = Path(args.video)
    name = (src.stem if src.is_file() else (capture.find_root(src) or src).name).replace(" ", "_")
    frames = Path("frames") / name
    say(f"==> 1/3 Frames: {frames}")                          # parsed by the web UI
    if args.dry_run:
        say(f"    (dry run: would read the capture {src}, pick about {args.frames} frames and use "
            f"{'COLMAP' if args.poses == 'colmap' else 'ARKit'} camera positions)")
        return None, frames
    if args.re_extract:
        (frames / "capture.json").unlink(missing_ok=True)
    try:
        plan = capture.prepare(src, frames, args.frames, args.max_size, Path("captures"))
    except capture.CaptureError as e:
        sys.exit(f"Couldn't read the capture: {e}")
    if plan["reused"]:
        say(f"    reusing {len(plan['frames'])} frames extracted earlier (use --re-extract to redo)")
    else:
        say(f"    {capture.describe(plan)}")
        say(f"    Kept {len(plan['frames'])} frames")                # parsed by the web UI

    ws = new_run_dir(Path("workspace") / name).resolve()
    if args.poses == "colmap":
        say(f"\n==> 2/3 Camera poses (COLMAP): {ws}")            # parsed by the web UI
        if run(module_cmd("colmap", frames, ws, "--step", 1, "--max-size", args.max_size), False) != 0:
            sys.exit("COLMAP did not produce a usable model - see the messages above. Brush was not started.")
        point_latest(ws)
        append_summary(ws, capture.summary_lines(plan, src, frames)[1:] + [("poses", "COLMAP (ARKit's ignored)")])
        return ws, frames

    label = "ARKit" if args.poses == "arkit" else "ARKit, refined with COLMAP"
    say(f"\n==> 2/3 Camera poses ({label}): {ws}")               # parsed by the web UI
    try:
        claim_workspace(ws)
    except WorkspaceError as e:
        sys.exit(str(e))
    point_latest(ws)
    summary = Summary(ws)
    summary.add("started", f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
    for k, v in capture.summary_lines(plan, src, frames):
        summary.add(k, v)
    started = datetime.datetime.now()
    made = capture.write_arkit_run(plan, frames, ws)
    say(f"    {made['images']} cameras placed from ARKit, {made['arkit_points']} ARKit points"
        + (f" (+{made['points'] - made['arkit_points']} to start from)" if made["points"] > made["arkit_points"] else ""))
    if args.poses == "refine":
        say("\n==> Refining with COLMAP: features, matching, triangulation, polish (lens kept as measured)")
        made = capture.refine_run(plan, ws, args.max_size)
        say(f"    {made['points']} points triangulated")
    took = (datetime.datetime.now() - started).total_seconds()
    summary.add("poses", f"{label}: {made['images']} frames, {made['points']} points in {took:.1f} s")
    summary.add("result", f"OK - train on {ws / 'dense'}")
    return ws, frames


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
    ap.add_argument("video", nargs="?", type=Path,
                    help="video file (.mov / .mp4), or an iPhone capture from Splat Capture (.zip or its folder)")
    ap.add_argument("--train-only", type=Path, metavar="RUN_DIR",
                    help="skip frames and COLMAP; re-train Brush on an existing run folder")
    ap.add_argument("--quality", choices=brushmod.QUALITY, default="standard")
    ap.add_argument("--detail", choices=["auto", *brushmod.DETAIL], default="auto",
                    help="fine detail to grow splats for (default: judged from the reconstruction)")
    ap.add_argument("--frames", type=int, default=250, help="frames to extract (default 250)")
    ap.add_argument("--start", help="trim: start time, e.g. 0:03")
    ap.add_argument("--end", help="trim: end time, e.g. 1:45")
    ap.add_argument("--re-extract", action="store_true", help="extract frames again even if they exist")
    ap.add_argument("--max-size", type=int, default=1600, help="image size for COLMAP (default 1600)")
    ap.add_argument("--poses", choices=["arkit", "refine", "colmap"], default="arkit",
                    help="iPhone captures only: arkit = the phone's camera positions as they are (default); "
                         "refine = plus COLMAP's points and a polish; colmap = ignore them, run COLMAP")
    ap.add_argument("--eval", action="store_true", help="hold out every 10th frame and report PSNR/SSIM")
    ap.add_argument("--no-train", action="store_true", help="stop after COLMAP")
    # Fog removal always runs; --clean is still accepted so older commands keep working.
    ap.add_argument("--clean", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--remove-background", action="store_true",
                    help="also save NAME_object.ply with only the object: no table, floor or room "
                         "(the original is kept)")
    ap.add_argument("--masks", action="store_true",
                    help="train only what was chosen on the 'Choose what to keep' page "
                         "(uses frames/<video>/masks, made by `splat-studio mask`)")
    ap.add_argument("--split-objects", action="store_true",
                    help="with --masks and several objects chosen: also save one splat per object "
                         "(NAME_part-<object>.ply)")
    ap.add_argument("--mask-mode", choices=["transparent", "ignore"], default="transparent",
                    help="transparent: everything not chosen is left out of the splat (default); "
                         "ignore: it is just not used for training")
    ap.add_argument("--stop-on-gaps", action="store_true",
                    help="stop before training when a side of the object (or of a chosen object) wasn't filmed; "
                         "train it later with --train-only RUN_DIR")
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
        frames = frames_of_run(ws)
    elif is_capture_arg(args.video):
        ws, frames = import_capture(args)
        if ws is None:                                 # dry run: nothing more to show
            return
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

        ws = new_run_dir(Path("workspace") / name).resolve()
        say(f"\n==> 2/3 Camera poses (COLMAP): {ws}")          # parsed by the web UI
        if run(module_cmd("colmap", frames, ws, "--step", 1, "--max-size", args.max_size), args.dry_run) != 0:
            sys.exit("COLMAP did not produce a usable model - see the messages above. Brush was not started.")
        if not args.dry_run:
            latest = ws.parent / "latest"
            try:
                if latest.is_symlink() or latest.exists():
                    latest.unlink()
                latest.symlink_to(ws.name)
            except OSError:
                pass

    # ------------------------------------------------ 2a: coverage (where the cameras were, before training)
    if not args.train_only and not args.dry_run:
        from . import coverage
        # with several objects chosen, one ring per object (its own centre, the frames it's visible in)
        cov = coverage.for_run(ws, frames if args.masks and args.split_objects else None)
        if cov:
            (ws / "coverage.json").write_text(json.dumps(cov))
            say(f"    coverage: {coverage.summary_line(cov)}")
            for o in cov.get("objects", []):
                say(f"    coverage of {o['name']}: {coverage.summary_line(o)}")
            say("Coverage: " + json.dumps(cov, separators=(",", ":")))          # parsed by the web UI
            append_summary(ws, [("coverage", coverage.summary_line(cov))]
                           + [(f"coverage {o['name']}", coverage.summary_line(o)) for o in cov.get("objects", [])])
            for who, c in [("", cov)] + [(o["name"] + ": ", o) for o in cov.get("objects", [])]:
                if coverage.blocked_degrees(c):
                    say(f"    note: {who}{coverage.blocked_degrees(c):.0f} degrees couldn't be filmed (something "
                        "stands there), so that side isn't counted as missing")
                for p in coverage.problems(c):
                    say("    warning: " + who + {"gap": "one side was barely filmed (see the coverage ring)",
                                                 "height": "the cameras were all at about the same height"}[p])

    # ------------------------------------------------ 2b: masks (Choose what to keep)
    dense = ws / "dense"
    if args.masks and not args.train_only and not args.dry_run:
        from . import masks as masksmod
        if not masksmod.has_masks(frames):
            sys.exit(f"--masks: no finished selection in {masksmod.masks_dir(frames)} - "
                     "choose what to keep first (splat-studio mask)")
        say("\n==> Preparing the selection for training")
        try:
            n = masksmod.prepare_for_dense(frames, ws, args.mask_mode)
        except (OSError, ValueError, RuntimeError) as e:
            sys.exit(f"Couldn't prepare the masks: {e}")
        say(f"    {n} masks ({args.mask_mode})")
        append_summary(ws, [("masks", f"{n} frames, {args.mask_mode}")])
        from .thumbs import make as make_thumb
        make_thumb(frames, masksmod.masks_dir(frames), ws / "thumbs" / "run.jpg")   # the library shows the object

    # ------------------------------------------------ 2c: stop here if a side wasn't filmed (#34)
    if args.stop_on_gaps and not args.train_only and not args.dry_run:
        missing = gap_problems(ws)
        if missing:
            say("\n==> Stopped before training: " + "; ".join(missing))
            say("    The camera positions are kept. Film the missing side and start a new run, or train anyway:")
            say(f"    splat-studio run --train-only {os.path.relpath(ws)} --quality {args.quality}")
            say("Stopped: coverage")                                  # parsed by the web UI
            append_summary(ws, [("stopped", "before training, a side wasn't filmed: " + "; ".join(missing))])
            sys.exit(STOPPED_EXIT)

    # ------------------------------------------------ 3: Brush
    if args.no_train:
        say(f"\n==> Stopping before training (--no-train). Train later with:\n"
            f"    splat-studio run --train-only {os.path.relpath(ws.parent / 'latest')} --quality {args.quality}")
        return
    say("\n==> 3/3 Training the splat (Brush)")
    if args.dry_run and not dense.exists():
        n_img, n_pts, size = 250, None, args.max_size
        say("    (dry run: assuming 250 images)")
    else:
        n_img, n_pts = brushmod.model_stats(dense / "sparse" / "0")
        n_img = n_img or len(list((dense / "images").glob("*")))
        size = brushmod.image_size(dense)
    params, why = brushmod.choose_brush_params(n_img, n_pts, args.quality, args.detail, size)
    say(f"    {why}")
    from .masks import dense_mask_mode
    if dense_mask_mode(dense) == "transparent" and not args.dry_run:
        params["--alpha-mode"] = "transparent"      # what wasn't chosen is trained to be see-through
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
        say("\n==> Removing background fog (the original is kept)")
        from .splats import PlyError, clean
        try:
            s = clean(plys[-1], run_dir=ws)
            say(f"Kept {s['kept']:,} of {s['total']:,} splats ({s['removed_share']:.0%} removed)")
            say(f"Clean copy: {s['output']}")                     # parsed by the web UI
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
        if args.split_objects and args.masks and frames is not None:
            say("\n==> Splitting the objects (the original is kept)")
            from .objects import split
            from .splats import PlyError
            try:
                from .masks import masks_dir
                from .thumbs import make as make_thumb
                for s in split(plys[-1], ws, frames, log=say):
                    make_thumb(frames, masks_dir(frames) / "objects" / str(s["id"]),
                               ws / "thumbs" / (Path(s["output"]).stem + ".jpg"))   # its own library picture
                    say(f"Part copy ({s['name']}): {s['output']}")      # parsed by the web UI
                    append_summary(ws, [(f"object {s['name']}", f"{s['output']}  ({s['kept']} splats)")])
            except (PlyError, OSError, ValueError, RuntimeError) as e:
                say(f"    couldn't split the objects: {e}")
    else:
        say(f"\nBrush finished but no .ply was found in {out_dir} - check its output above.")


if __name__ == "__main__":
    main()
