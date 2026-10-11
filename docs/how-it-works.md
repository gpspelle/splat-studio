# How it works

```
 video ──► frames ──► camera poses ──► Gaussian splat ──► web viewer
         (ffmpeg)     (COLMAP, CPU)    (Brush, GPU)       (Spark / three.js)
```

## Stage 1: frames (`splat_studio/frames.py`)

- Samples the video evenly to get about 250 frames (`--frames`), so a 1-minute clip and a
  4-minute clip both produce a workable set.
- iPhone HDR (HLG/PQ) is detected and tone-mapped to normal colours, so frames don't come out grey.
- Rotation metadata is applied, so portrait videos come out upright.
- Blurry frames (sharpness well below the median) are moved to `_blurry/` if OpenCV is installed.
- The frame rate and start time are saved in `extraction.json`. That's what lets the UI turn
  "frames 85–102 failed" into "0:42–0:51 in your video".

## Stage 2: camera poses (`splat_studio/colmap.py`)

COLMAP finds the same points in pairs of frames (**matching**), then works out where the
camera stood for each frame (**mapping**). When some frames share too few points with their
neighbours, the "track breaks" and COLMAP produces several disconnected models.

The stage escalates automatically and stops at the first attempt whose largest model
contains ≥ 90% of the frames:

| Attempt | Matching | Mapper |
|---|---|---|
| 1 | sequential: each frame vs its 15 neighbours | standard |
| 2 | 40 neighbours + jumps of 2, 4, 8… frames + loop closure (first 25 vs last 25 frames) | relaxed |
| 3 | exhaustive: every pair (only up to 300 frames, it's O(n²)) | relaxed |
| 4–5 | attempts 1–2 again with every frame, if you started with `--step 2` | |

Matches are kept in COLMAP's database between attempts, so later attempts only pay for new pairs.
Below 60% the run stops, with advice, instead of training on a bad model.

**Time estimates.** COLMAP's own log counters drive the progress bar. The time left is
estimated from the number of image pairs each attempt will match, the feature count per image
(matching cost grows with its square), and a superlinear model for mapping. Estimates start
from defaults for an M1 CPU (marked `~`) and switch to measured speeds of your computer.
Those speeds are saved in `~/.cache/splat-studio/rates.json`, so the second run on a machine
is accurate from the start.

**Safety.** Every run has its own folder `workspace/<video>/run_<date_time>/` with a
`summary.txt`, and a lock file stops two runs from ever writing to the same folder.

## Stage 3: training (`splat_studio/brush.py`, `pipeline.py`)

[Brush](https://github.com/ArthurBrussee/brush) trains the splat on the GPU (Metal on Apple
Silicon). Its settings are picked from the reconstruction. All option names come from Brush's
source and are checked by the test suite.

| Brush option | How it's chosen |
|---|---|
| `--total-train-iters` | ~120 steps per image (standard), clamped to 10k–30k. Preview ~30/image, high ~160/image. |
| `--growth-stop-iter` | half the steps (Brush's own default ratio) |
| `--refine-every` | number of images, 100–300 ("images it takes to cover your scene", per Brush's docs) |
| `--growth-grad-threshold` | from sparse points per image: lots of texture → 0.0018 (more detail), plain → 0.0035 |
| `--max-resolution` | 1024 / 1600 / 1920 by quality, never above the images |
| `--sh-degree` | 2 (preview) or 3: how well reflections and gloss are captured |
| `--max-splats` | 0.5M / 2M / 4M, to keep files reasonable for the web |

These are reasonable starting points, not tuned optima. Use **Measure quality** (`--eval`)
to compare settings with PSNR/SSIM.

Brush writes the scene's estimated up direction into the `.ply` header
(`comment Vertical axis: x y z`). The viewer reads it to stand the model upright.

## Fog: diagnosis and cleanup (`splat_studio/splats.py`)

- Reads standard 3DGS PLY files in chunks with `struct` (no numpy needed): position, opacity
  (stored as a logit) and scale (stored as a log).
- **Object region.** With a run folder, it reads the camera centres and viewing directions from
  COLMAP's `images.bin` and finds the point closest to all viewing rays: what the cameras were
  filming. The kept radius is 0.8 × the median camera distance. Without cameras, it uses the
  median of the opaque splats and 1.5 × their 80th-percentile distance (weaker indoors, where
  walls are opaque too).
- **inspect** reports the share of splats outside that region, the share that are large and
  faint ("fog"), the share that are nearly invisible everywhere (a sign of inaccurate camera
  positions), and how much bigger the whole scene is than the object.
- **clean** copies the rows of splats inside the region with opacity ≥ 0.02 into
  `NAME_clean.ply`, keeping the header comments (including the up axis). It refuses to
  overwrite its input.
- The viewer frames the camera on the 5th–95th percentile of opaque splats and implements
  **Crop** with Spark's `SplatEdit` (an inverted sphere that makes everything outside it
  transparent).

Tested on a synthetic object-in-a-foggy-room scene with known answers: all object splats kept,
fog removed, and a real browser confirms the crop removes the fog from the frame.

## Languages (`static/i18n.js`)

One dictionary per language, shared by the Studio page, the viewer and exported websites.
Server-side advice and errors carry codes and parameters, so they're translated in the browser.

## The web app (`splat_studio/server/`)

- `app.py`: a standard-library HTTP server bound to `127.0.0.1` only.
- `jobs.py`: a queue that runs one pipeline at a time under a pseudo-terminal (so every tool
  prints its live progress bar) and parses that output into status: stage, attempt, percent,
  time left, registered frames, track breaks, and the resulting splat.
- `library.py`: the data folder (videos, runs, splats), and the website export.
- `../guidance.py`: checks on uploaded videos and advice after runs.
- `static/`: the Studio page and the viewer, with three.js and Spark bundled, so nothing is
  loaded from the internet.

## Testing (`tests/`)

- `tests/unit`: pure logic (planning, estimates, parsing, Brush settings, advice, library).
- `tests/integration`: real ffmpeg, plus stand-in `colmap` and `brush` executables
  (`tests/fakes/bin`) that print the real tools' output formats. The fake Brush rejects any
  option the real Brush doesn't have. The integration tests cover the stages, the full CLI
  pipeline, the HTTP API with real jobs, and the UI in a headless browser.

Real COLMAP and Brush are not run in CI: that would need a GPU and a long time per test.
