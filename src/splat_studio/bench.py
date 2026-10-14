"""Benchmark: what each setting costs in time, and what it buys in quality, on this computer.

Uses a finished run (its prepared images and its undistorted training folder), so the numbers
are about your own videos and your own hardware:

  COLMAP  feature image size and count, how many neighbouring frames are matched, and the
          incremental vs the global mapper (COLMAP 4+). Each variant runs from scratch in
          RUN/bench/colmap/<variant>/; reported: seconds per step, registered frames, error.
  Brush   one training run that scores held-out frames (every 10th) at regular steps, so a
          single run shows how quality grows with time; then the same at a lower training
          resolution. Exports go to RUN/bench/brush/, never to the run's own splat folder.

    splat-studio bench workspace/<video>/latest            both parts
    splat-studio bench RUN --only colmap                   just COLMAP (minutes)
    splat-studio bench RUN --only brush --quality preview  just Brush, at preview settings

Writes RUN/bench/report.md (a table) and RUN/bench/results.json. Standard library only.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import brush as brushmod
from .common import fmt_time, say

# (name, longest image side for features, max features per image, neighbours matched, mapper)
COLMAP_VARIANTS = [
    ("current", 1600, 8192, 15, "incremental"),
    ("smaller features", 1024, 4096, 15, "incremental"),
    ("smaller + fewer pairs", 1024, 4096, 8, "incremental"),
    ("global mapper", 1600, 8192, 15, "global"),
    ("smaller + global", 1024, 4096, 8, "global"),
]
RE_EVAL = re.compile(r"Eval iter (\d+): PSNR ([\d.]+), ssim ([\d.]+)")
RE_REPROJ = re.compile(r"Mean reprojection error:\s*([\d.]+)")


def colmap_help(cmd: str | None = None) -> str:
    r = subprocess.run(["colmap", *([cmd, "-h"] if cmd else ["help"])], capture_output=True, text=True)
    return r.stdout + r.stderr


def option_names() -> dict:
    """Option spellings differ between COLMAP 3.x and 4.x."""
    h = colmap_help("feature_extractor")
    new = "FeatureExtraction." in h
    return {
        "extract_gpu": "--FeatureExtraction.use_gpu" if new else "--SiftExtraction.use_gpu",
        "match_gpu": "--FeatureMatching.use_gpu" if new else "--SiftMatching.use_gpu",
        "max_size": "--FeatureExtraction.max_image_size" if "FeatureExtraction.max_image_size" in h
        else "--SiftExtraction.max_image_size",
        "max_features": "--SiftExtraction.max_num_features",
    }


def timed(cmd: list, log: Path) -> tuple[float, str]:
    t0 = time.monotonic()
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, errors="replace")
    dt = time.monotonic() - t0
    with open(log, "a") as f:
        f.write(f"\n$ {' '.join(str(c) for c in cmd)}\n{r.stdout}{r.stderr}")
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} {cmd[1]} failed (see {log})")
    return dt, r.stdout + r.stderr


def largest_model(sparse: Path) -> tuple[int, float | None]:
    """(registered images, mean reprojection error) of the biggest model COLMAP wrote."""
    best = (0, None)
    for d in sorted(p for p in sparse.iterdir() if p.is_dir()):
        txt = subprocess.run(["colmap", "model_analyzer", "--path", str(d)], capture_output=True, text=True)
        out = txt.stdout + txt.stderr
        n, _ = brushmod.parse_model_stats(out)
        m = RE_REPROJ.search(out)
        if n and n > best[0]:
            best = (n, float(m.group(1)) if m else None)
    return best


def bench_colmap(run: Path, out: Path, variants=COLMAP_VARIANTS) -> list[dict]:
    images = run / "images"
    if not any(images.glob("*.jpg")):
        raise SystemExit(f"{images} has no prepared images: is this a finished run folder?")
    n = len(list(images.glob("*.jpg")))
    opts = option_names()
    has_global = "global_mapper" in colmap_help()
    log = out / "colmap.log"
    results, cache = [], {}
    for name, size, feats, overlap, mapper in variants:
        if mapper == "global" and not has_global:
            say(f"  {name}: skipped (this COLMAP has no global_mapper; COLMAP 4.0 or newer does)")
            continue
        say(f"  {name}: {size}px, {feats} features, {overlap} neighbours, {mapper} mapper")
        d = out / "colmap" / re.sub(r"\W+", "_", name)
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
        db = d / "database.db"
        row = {"variant": name, "image_size": size, "max_features": feats, "overlap": overlap,
               "mapper": mapper, "images": n}
        try:
            key = (size, feats)
            if key in cache:                                  # same features: reuse the extraction
                shutil.copy(cache[key][0], db)
                row["extract_s"] = cache[key][1]
            else:
                row["extract_s"], _ = timed(["colmap", "feature_extractor", "--database_path", db,
                                             "--image_path", images, "--ImageReader.single_camera", "1",
                                             "--ImageReader.camera_model", "OPENCV", opts["extract_gpu"], "0",
                                             opts["max_size"], size, opts["max_features"], feats], log)
                shutil.copy(db, d / "features.db")
                cache[key] = (d / "features.db", row["extract_s"])
            row["match_s"], _ = timed(["colmap", "sequential_matcher", "--database_path", db,
                                       "--SequentialMatching.overlap", overlap, opts["match_gpu"], "0"], log)
            sparse = d / "sparse"
            sparse.mkdir()
            row["map_s"], _ = timed(["colmap", "global_mapper" if mapper == "global" else "mapper",
                                     "--database_path", db, "--image_path", images, "--output_path", sparse], log)
            row["registered"], row["reprojection_px"] = largest_model(sparse)
            row["total_s"] = row["extract_s"] + row["match_s"] + row["map_s"]
        except RuntimeError as e:
            row["error"] = str(e)
        results.append(row)
        say("    " + (row.get("error") or f"{fmt_time(row['total_s'])}: {row['registered']}/{n} frames"))
    return results


def bench_brush(run: Path, out: Path, brush: Path, quality: str, evals: int, low_res: int) -> list[dict]:
    dense = run / "dense"
    if not (dense / "sparse" / "0").is_dir():
        raise SystemExit(f"{dense} has no training data: is this a finished run folder?")
    n_img, n_pts = brushmod.model_stats(dense / "sparse" / "0")
    n_img = n_img or len(list((dense / "images").glob("*")))
    size = brushmod.image_size(dense)
    params, why = brushmod.choose_brush_params(n_img, n_pts, quality, "auto", size)
    steps = params["--total-train-iters"]
    every = max(500, steps // evals)
    say(f"  {why}; scoring held-out frames every {every} steps")
    runs = [("current", {})]
    if (size or 0) > low_res and params["--max-resolution"] > low_res:
        runs.append((f"train at {low_res}px", {"--max-resolution": low_res}))
    results = []
    for name, override in runs:
        p = {**params, **override, "--eval-split-every": 10, "--eval-every": every,
             "--export-path": str((out / "brush" / re.sub(r"\W+", "_", name)).resolve()),
             "--export-name": "bench_{iter}.ply"}
        cmd = [str(brush), str(dense)] + [str(x) for kv in p.items() for x in kv]
        say(f"  {name}: {steps} steps at up to {p['--max-resolution']}px")
        curve, total, code = train_scored(cmd, out / "brush.log")
        results.append({"variant": name, "steps": steps, "max_resolution": p["--max-resolution"],
                        "total_s": total, "curve": curve,
                        **({"error": f"brush exited with {code}"} if code else {})})
    return results


def train_scored(cmd: list[str], log_path: Path) -> tuple[list[dict], float, int]:
    """Run a Brush command that scores held-out frames; (curve, seconds, exit code). The curve has
    one {step, seconds, psnr, ssim} per evaluation, printed as it comes."""
    curve, t0 = [], time.monotonic()
    with open(log_path, "a") as log:
        log.write(f"\n$ {' '.join(cmd)}\n")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                errors="replace", bufsize=1, env=brushmod.eval_env())
        for line in proc.stdout:
            log.write(line)
            m = RE_EVAL.search(line)
            if m:
                curve.append({"step": int(m.group(1)), "seconds": round(time.monotonic() - t0, 1),
                              "psnr": float(m.group(2)), "ssim": float(m.group(3))})
                say(f"    step {curve[-1]['step']:>6}  {fmt_time(curve[-1]['seconds']):>8}  "
                    f"PSNR {curve[-1]['psnr']:.2f}  SSIM {curve[-1]['ssim']:.3f}")
        proc.wait()
    return curve, round(time.monotonic() - t0, 1), proc.returncode


def report(run: Path, colmap_rows: list[dict], brush_rows: list[dict]) -> str:
    lines = [f"# Benchmark: {run.name}", ""]
    if colmap_rows:
        base = next((r for r in colmap_rows if r["variant"] == "current" and "total_s" in r), None)
        lines += ["## Camera poses (COLMAP)", "",
                  "| Variant | Features | Matching | Mapping | Total | vs current | Frames placed | Error (px) |",
                  "|---|---|---|---|---|---|---|---|"]
        for r in colmap_rows:
            if "error" in r:
                lines.append(f"| {r['variant']} | failed | | | | | | |")
                continue
            vs = f"{base['total_s'] / r['total_s']:.1f}x faster" if base and r is not base else ""
            err = f"{r['reprojection_px']:.2f}" if r.get("reprojection_px") is not None else "?"
            lines.append(f"| {r['variant']} | {fmt_time(r['extract_s'])} | {fmt_time(r['match_s'])} | "
                         f"{fmt_time(r['map_s'])} | {fmt_time(r['total_s'])} | {vs} | "
                         f"{r['registered']}/{r['images']} | {err} |")
        lines.append("")
    for r in brush_rows:
        lines += [f"## Training (Brush): {r['variant']}, up to {r['max_resolution']}px", "",
                  "PSNR and SSIM on held-out frames (every 10th); higher is better. Around 0.5 dB is hard to see.", "",
                  "| Step | Time | PSNR | SSIM |", "|---|---|---|---|"]
        for c in r["curve"]:
            lines.append(f"| {c['step']} | {fmt_time(c['seconds'])} | {c['psnr']:.2f} | {c['ssim']:.3f} |")
        lines.append(f"\nTraining took {fmt_time(r['total_s'])} for {r['steps']} steps.")
        if not r["curve"]:
            lines.append("Brush printed no quality scores, so there is no curve (see bench/brush.log).")
        if r.get("error"):
            lines.append(f"\n{r['error']}")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="splat-studio bench", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run", type=Path, help="a finished run folder, e.g. workspace/vase/latest")
    ap.add_argument("--only", choices=["colmap", "brush"], help="run just one part")
    ap.add_argument("--quality", choices=brushmod.QUALITY, default="standard", help="Brush settings to measure")
    ap.add_argument("--evals", type=int, default=6, help="how many times to score quality during training")
    ap.add_argument("--low-res", type=int, default=1024, help="the lower training resolution to compare")
    ap.add_argument("--brush", help="path to the brush binary")
    a = ap.parse_args(argv)
    run = a.run.resolve()
    if not run.is_dir():
        sys.exit(f"{a.run}: no such run folder")
    if not shutil.which("colmap"):
        sys.exit("colmap not found on PATH (macOS: brew install colmap)")
    out = run / "bench"
    out.mkdir(exist_ok=True)
    colmap_rows, brush_rows = [], []
    if a.only in (None, "colmap"):
        say("==> Camera poses (COLMAP)")
        colmap_rows = bench_colmap(run, out)
    if a.only in (None, "brush"):
        say("==> Training (Brush)")
        brush = Path(a.brush) if a.brush else brushmod.find_brush()
        if not brush:
            sys.exit("Brush isn't built yet: run `splat-studio setup` first")
        brush_rows = bench_brush(run, out, brush, a.quality, a.evals, a.low_res)
    (out / "results.json").write_text(json.dumps({"colmap": colmap_rows, "brush": brush_rows}, indent=1))
    text = report(run, colmap_rows, brush_rows)
    (out / "report.md").write_text(text + "\n")
    say("\n" + text)
    say(f"\nSaved {out / 'report.md'}")


if __name__ == "__main__":
    main()
