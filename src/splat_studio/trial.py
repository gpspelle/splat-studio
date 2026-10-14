"""The Phase 0 trial: do the iPhone's camera positions (ARKit) train a splat as well as COLMAP's?

From one Splat Capture package, the same frames get camera positions three ways:

  ARKit           the phone's positions and points as recorded (seconds)
  ARKit + points  the phone's positions, COLMAP's points and a polish with the lens fixed
  COLMAP          the phone's positions ignored; COLMAP as for a video

Each is trained with the same Brush settings, holding out every 10th frame and scoring the
splat on those frames as training goes (PSNR and SSIM; higher is better). The report says how
long each took to place the cameras and to train, and how good each splat is.

    splat-studio trial capture_2026-10-14_153012.zip
    splat-studio trial CAPTURE.zip --quality preview          # quicker
    splat-studio trial CAPTURE.zip --only arkit colmap        # skip a variant

Results go to workspace/<name>/trial_<date_time>/ (report.md, results.json, one folder per
variant); runs in the library are left alone.
"""
from __future__ import annotations

import argparse
import datetime
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import brush as brushmod
from . import capture
from .bench import train_scored
from .common import child_env, fmt_time, module_cmd, say

VARIANTS = [("arkit", "ARKit"), ("refine", "ARKit + points"), ("colmap", "COLMAP")]
SAME = 0.5          # dB: about the smallest difference you can see


def place_cameras(key: str, plan: dict, frames: Path, ws: Path, max_size: int) -> dict:
    """Camera positions for one variant into ws (images/, sparse/0, dense/). Raises on failure."""
    if key == "colmap":
        r = subprocess.run([str(c) for c in module_cmd("colmap", frames, ws, "--step", 1, "--max-size", max_size)],
                           env=child_env())
        if r.returncode != 0:
            raise RuntimeError("COLMAP didn't produce a usable model (see colmap.log)")
        return {}
    ws.mkdir(parents=True)
    made = capture.write_arkit_run(plan, frames, ws)
    if key == "refine":
        try:
            made = capture.refine_run(plan, ws, max_size)
        except SystemExit as e:                    # Colmap.run exits on a failed command
            raise RuntimeError(str(e).strip().splitlines()[0] if str(e).strip() else "COLMAP failed") from None
    return made


def verdict(rows: list[dict]) -> str:
    by = {r["key"]: r for r in rows if r.get("psnr") is not None}
    a, c = by.get("arkit"), by.get("colmap")
    if not a or not c:
        return "Not enough results to compare ARKit with COLMAP."
    d = a["psnr"] - c["psnr"]
    saved = c["poses_s"] - a["poses_s"]
    if d >= -SAME:
        return (f"ARKit's camera positions train as well as COLMAP's ({d:+.2f} dB) and save "
                f"{fmt_time(max(0.0, saved))} before training.")
    r = by.get("refine")
    if r and r["psnr"] - c["psnr"] >= -SAME:
        return (f"ARKit alone is {-d:.2f} dB worse than COLMAP, but adding COLMAP's points closes the gap "
                f"({r['psnr'] - c['psnr']:+.2f} dB) in {fmt_time(r['poses_s'])} instead of {fmt_time(c['poses_s'])}.")
    return f"ARKit's camera positions train {-d:.2f} dB worse than COLMAP's: keep COLMAP for now."


def report(name: str, plan: dict, rows: list[dict], params_from: str) -> str:
    lines = [f"# Trial: {name}", "",
             f"{capture.describe(plan)}. Brush settings from the {params_from} run, the same for all; every "
             "10th frame held out and scored. Around 0.5 dB is hard to see.", "",
             "| Camera positions | Frames placed | Points | Placing cameras | Training | PSNR | SSIM | vs COLMAP |",
             "|---|---|---|---|---|---|---|---|"]
    base = next((r for r in rows if r["key"] == "colmap" and r.get("psnr") is not None), None)
    for r in rows:
        if r.get("error"):
            lines.append(f"| {r['label']} | failed: {r['error']} | | | | | | |")
            continue
        vs = f"{r['psnr'] - base['psnr']:+.2f} dB" if base and r is not base and r.get("psnr") is not None else ""
        psnr = f"{r['psnr']:.2f}" if r.get("psnr") is not None else "?"
        ssim = f"{r['ssim']:.3f}" if r.get("ssim") is not None else "?"
        lines.append(f"| {r['label']} | {r.get('images') or '?'}/{len(plan['frames'])} | {r.get('points') or '?'} | "
                     f"{fmt_time(r['poses_s'])} | {fmt_time(r['train_s']) if r.get('train_s') is not None else '?'} | "
                     f"{psnr} | {ssim} | {vs} |")
    placed = {r.get("images") for r in rows if not r.get("error")}
    if len(placed) > 1:
        lines += ["", "Not every variant placed every frame, so the held-out frames differ slightly."]
    lines += ["", f"**{verdict(rows)}**", ""]
    for r in rows:
        if r.get("curve"):
            lines += [f"## {r['label']}: quality during training", "", "| Step | Time | PSNR | SSIM |", "|---|---|---|---|"]
            lines += [f"| {c['step']} | {fmt_time(c['seconds'])} | {c['psnr']:.2f} | {c['ssim']:.3f} |" for c in r["curve"]]
            lines.append("")
    return "\n".join(lines)


def new_trial_dir(parent: Path) -> Path:
    base = parent / f"trial_{datetime.datetime.now():%Y%m%d_%H%M%S}"
    d, i = base, 2
    while d.exists():
        d = base.with_name(f"{base.name}_{i}")
        i += 1
    return d


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="splat-studio trial", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("capture", type=Path, help="a Splat Capture package (.zip or its folder)")
    ap.add_argument("--only", nargs="+", choices=[k for k, _ in VARIANTS], help="run only these variants")
    ap.add_argument("--frames", type=int, default=250, help="frames to use (default 250)")
    ap.add_argument("--max-size", type=int, default=1600, help="image size (default 1600)")
    ap.add_argument("--quality", choices=brushmod.QUALITY, default="standard", help="Brush settings")
    ap.add_argument("--evals", type=int, default=6, help="how many times to score quality during training")
    ap.add_argument("--brush", help="path to the brush binary")
    a = ap.parse_args(argv)

    if not capture.is_capture(a.capture):
        sys.exit(f"{a.capture}: not a Splat Capture package (a .zip or folder with frames.jsonl)")
    for tool in ("colmap", "ffmpeg"):
        if not shutil.which(tool):
            sys.exit(f"{tool} not found on PATH: run `splat-studio setup` first")
    brush = Path(a.brush) if a.brush else brushmod.find_brush()
    if not brush:
        sys.exit("Brush isn't built yet: run `splat-studio setup` first")

    src = a.capture
    name = (src.stem if src.is_file() else (capture.find_root(src) or src).name).replace(" ", "_")
    frames = Path("frames") / name
    say(f"==> Frames: {frames}")
    try:
        plan = capture.prepare(src, frames, a.frames, a.max_size, Path("captures"))
    except capture.CaptureError as e:
        sys.exit(f"Couldn't read the capture: {e}")
    say(f"    {capture.describe(plan)}")
    out = new_trial_dir(Path("workspace") / name).resolve()
    out.mkdir(parents=True)

    keys = [k for k, _ in VARIANTS if not a.only or k in a.only]
    rows = []
    for key in keys:
        label = dict(VARIANTS)[key]
        say(f"\n==> Camera positions: {label}")
        ws = out / key
        row = {"key": key, "label": label}
        t0 = time.monotonic()
        try:
            place_cameras(key, plan, frames, ws, a.max_size)
            row["poses_s"] = round(time.monotonic() - t0, 1)
            row["images"], row["points"] = brushmod.model_stats(ws / "dense" / "sparse" / "0")
            say(f"    {row['images']} frames placed, {row['points']} points in {fmt_time(row['poses_s'])}")
        except (RuntimeError, OSError, capture.CaptureError) as e:
            row["error"] = str(e)
            say(f"    failed: {e}")
        rows.append(row)

    ok = [r for r in rows if not r.get("error")]
    if not ok:
        sys.exit("No variant produced camera positions; see the messages above.")
    ref = next((r for r in ok if r["key"] == "colmap"), ok[0])
    dense = out / ref["key"] / "dense"
    params, why = brushmod.choose_brush_params(ref.get("images") or len(plan["frames"]), ref.get("points"),
                                               a.quality, "auto", brushmod.image_size(dense))
    steps = params["--total-train-iters"]
    every = max(500, steps // max(1, a.evals))
    say(f"\n==> Training each with the same settings ({why}); scoring held-out frames every {every} steps")
    for r in ok:
        say(f"\n==> Training: {r['label']}")
        ws = out / r["key"]
        p = {**params, "--eval-split-every": 10, "--eval-every": every,
             "--export-path": str((ws / "splat").resolve()), "--export-name": f"{name}_{r['key']}_{{iter}}.ply"}
        cmd = [str(brush), str(ws / "dense")] + [str(x) for kv in p.items() for x in kv]
        curve, total, code = train_scored(cmd, ws / "brush.log")
        r["train_s"], r["curve"] = total, curve
        if code:
            r["error"] = f"Brush exited with {code}"
        if curve:
            r["psnr"], r["ssim"] = curve[-1]["psnr"], curve[-1]["ssim"]

    text = report(name, plan, rows, ref["label"])
    (out / "results.json").write_text(json.dumps({"capture": str(src), "frames": len(plan["frames"]),
                                                  "brush": {k: v for k, v in params.items()},
                                                  "variants": rows}, indent=1))
    (out / "report.md").write_text(text + "\n")
    say("\n" + text)
    say(f"\nSaved {out / 'report.md'}")


if __name__ == "__main__":
    main()
