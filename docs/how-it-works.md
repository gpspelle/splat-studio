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

**Time estimates** (`splat_studio/eta.py`). The app never shows a made-up number:
- *This step* is measured from the step's own progress over a recent window (the last 30% of
  the step, at least 20 s), so start-up time and changing speed don't skew it. Mapping adds
  images more slowly as it goes (cost grows as images^1.5). Until a step has run for 8 s and
  moved 2%, the UI says *measuring speed*; a COLMAP step stuck near 100% (the final bundle
  adjustment) says *finishing up* rather than "0s".
- *Training* slows down while the splat grows. Step time is modelled as overhead plus a cost
  per splat, fitted on the splat counts Brush prints, and the count is projected to where
  growth stops (half the steps), following how its growth has been slowing.
- *Whole run* adds the parts still to come, from how long they took on this computer before,
  per frame, per training step (per quality) and per million splats, saved in
  `~/.cache/splat-studio/timings.json`. Until a first full run has been timed, it says so.
- Every UI job writes `timeline.jsonl` (progress with timestamps) next to its log.

**Safety.** Every run has its own folder `workspace/<video>/run_<date_time>/` with a
`summary.txt`, and a lock file stops two runs from ever writing to the same folder.

### Coverage check (`splat_studio/coverage.py`)

Right after COLMAP, before training: "up" is the average of the cameras' own up directions, and
the object is the point the cameras' viewing rays pass closest to (least squares). Each camera
gets an angle around the object and a height angle seen from it. The largest empty arc between
neighbouring cameras over 60°, or a spread under 15° between the 10th and 90th percentile
heights, is a warning; the frames on either side of the gap give the moment in the video. The
run prints `Coverage: {json}` (the UI draws the ring from it) and saves `coverage.json`.

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
  densest connected cluster of opaque splats (a flood fill over a voxel grid, starting from the
  most crowded block), so opaque walls don't pull the region away from the object.
- **inspect** reports the share of splats outside that region, the share that are large and
  faint ("fog"), the share that are nearly invisible everywhere (a sign of inaccurate camera
  positions), and how much bigger the whole scene is than the object.
- **clean** runs after every training run (and on demand for imported splats). It copies the rows of splats inside the region with opacity ≥ 0.02 into
  `NAME_clean.ply`, keeping the header comments (including the up axis). It refuses to
  overwrite its input.
- The viewer frames the camera on the 5th–95th percentile of opaque splats and implements
  **Crop** with Spark's `SplatEdit` (an inverted sphere that makes everything outside it
  transparent).

Tested on a synthetic object-in-a-foggy-room scene with known answers: all object splats kept,
fog removed, and a real browser confirms the crop removes the fog from the frame.

## Background removal (`splats.isolate`)

1. Keep the object region (as above) and splats with opacity ≥ 0.05.
2. Find the support surface: a plane through the opaque splats, near-horizontal with respect
   to Brush's up axis. Candidates are the most crowded height along the up axis plus RANSAC
   planes (for a slightly tilted up axis); the one with most splats within 1.2% of the region
   radius wins, if it holds at least 12% of them. Splats on it, and on the far side from the
   object, are dropped.
3. Voxelise the remaining opaque splats (region radius / 30), flood-fill from the voxel
   nearest their median, grow the blob by one voxel, and keep every splat inside it.

The result, `NAME_object.ply`, keeps the header comments (including the up axis). Tested on a
synthetic vase-on-a-table scene with opaque walls and fog: more than 93% of the vase kept, no
table, wall or fog splats, with and without camera positions.

## Choosing what to keep (`masks.py`, `maskwork.py`, `static/keep.html`)

Beta. The person clicks the object (keep) and things to drop (leave out) on a few frames; a
segmentation model turns that into one mask per frame, and training ignores, or makes
transparent, everything outside it.

1. **Frames.** The page runs the frames stage on its own (`frames` job) so there is something to
   click on; the run later reuses the same `frames/<video>/` folder.
2. **Preview.** Each click posts the frame's points to `/api/keep/preview`. With SAM 2 a single
   long-lived `maskwork serve` process answers (JSON lines), so the model loads once.
3. **Propagation.** `splat-studio mask` writes `frames/<video>/masks/<frame>.png` (white = keep)
   plus `prompts.json` and `done.json`. SAM 2 runs in its own environment
   (`~/.splat-studio/sam2/venv`, Python 3.10+ with PyTorch) as `maskwork propagate`: chunks of
   64 frames, forwards from the first clicked frame and then backwards, each chunk starting
   from the clicks on its frames or the mask carried over from the previous chunk's edge
   frame, which bounds memory on long videos. `PYTORCH_ENABLE_MPS_FALLBACK=1` lets it use the
   Mac's GPU. The app itself still uses only the standard library: masks are written as PNGs
   by hand, and image sizes are read from JPEG headers.
4. **Into the training images.** COLMAP resizes the frames (`ws/images`) and undistorts them
   (`dense/images`), so each mask goes through the same steps (`masks.prepare_for_dense`): ffmpeg
   resizes it with nearest-neighbour scaling, then `colmap image_undistorter` runs on the masks
   with the same distorted model, recorded in `dense/source_model.txt`. The result is
   thresholded back to black and white in `dense/masks/<image stem>.png`, where Brush finds it.
5. **Training.** In `transparent` mode (the default) Brush gets `--alpha-mode transparent`: the
   mask becomes the image's alpha and what's outside is trained to be see-through, so a stand
   disappears instead of being guessed at. `ignore` mode only leaves those pixels out of the loss.

**Several objects.** Clicks carry a label: 0 leaves out, 1–8 is the object. Each object is
propagated on its own (its clicks as keep points; on the same frames, the other objects' clicks
and the leave-out clicks as remove points) into `masks/objects/<id>/`, and their union is
`masks/<frame>.png`, so training keeps all of them at once. `objects.py` then splits the
trained splat: visible splats are grouped on a 120-across voxel grid, each voxel centre is
projected into 48 evenly spread frames with the distorted COLMAP model (OPENCV and the other
standard camera models), and the voxel goes to the object whose mask holds it in at least half
of the frames where it's in view (at least 3). Masks are read at 320 px wide through ffmpeg,
so the split stays standard-library Python; 500,000 splats take about 15 seconds.

**Fixing a slipped frame** (`masks.update_masks`). After an Apply, frames whose clicks changed
are redone on their own: for each object clicked on a changed frame (every object, for a frame
with only leave-out clicks), from the previous frame that object was clicked on to the next one,
both included so SAM 2 starts from real clicks. Overlapping ranges merge, the union is rebuilt
only for those frames, and a new or removed object needs a full Apply.

**Library pictures** (`thumbs.py`). From ~24 masks spread over the video, the one with the
largest selection that doesn't touch the frame's edge wins; its frame is cropped around the
selection at the tiles' 16:10 shape, and ffmpeg's `maskedmerge` keeps the selection sharp over a
blurred, darkened copy of the frame. Made during the run (`run/thumbs/run.jpg`, and
`<splat>.jpg` per object), or once in the background for runs made before.

Tests use a stand-in backend (`SPLAT_STUDIO_MASK_BACKEND=disc`: a disc around the keep clicks)
and a stubbed SAM 2 for the chunking logic.

## Comparing (`static/compare.html`)

Two panes, each with its own WebGL renderer, Spark renderer, camera and orbit controls. The
pane you last touched leads: every frame its camera offset and pan, divided by its model's
size, are applied to the other pane, multiplied by that model's size. Unlinked, each pane
moves on its own.

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
- `static/`: the Studio page (`index.html`: runs and library), New run (`new.html`: the video
  and its settings, kept in `sessionStorage` while you visit Choose what to keep), and the viewer, with three.js and Spark bundled, so nothing is
  loaded from the internet.

## Testing (`tests/`)

- `tests/unit`: pure logic (planning, estimates, parsing, Brush settings, advice, library).
- `tests/integration`: real ffmpeg, plus stand-in `colmap` and `brush` executables
  (`tests/fakes/bin`) that print the real tools' output formats. The fake Brush rejects any
  option the real Brush doesn't have. The integration tests cover the stages, the full CLI
  pipeline, the HTTP API with real jobs, and the UI in a headless browser.

Real COLMAP and Brush are not run in CI: that would need a GPU and a long time per test.
