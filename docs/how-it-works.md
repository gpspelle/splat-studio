# How it works

```
 video ──► frames ──► camera poses ──► Gaussian splat ──► web viewer
         (ffmpeg)     (COLMAP, CPU)    (Brush, GPU)       (Spark / three.js)
```

## Stage 1: frames (`splat_studio/frames.py`)

- Samples the video evenly. The count is "auto" by default (`--frames auto`, `frames.auto_target`):
  about 1.75 frames per second of the part used (after trimming), at least 250 and at most 600, so
  a 1-minute clip gets 250 and a 4-minute car walkaround 420, a frame every ~0.6 s, which keeps
  enough overlap between neighbours. Training time barely changes (at standard quality Brush's
  steps are capped from 250 images on); COLMAP's time grows a little faster than the count, hence
  the cap. A number (`--frames 400`, or **Frames** under New run → Advanced) overrides it.
- iPhone HDR (HLG/PQ) is detected and tone-mapped to normal colours, so frames don't come out grey.
- Rotation metadata is applied, so portrait videos come out upright.
- **The sharpest frame at each moment** (#50, needs OpenCV): ffmpeg extracts 3 candidates per
  moment (3× the rate), each is scored (variance of the Laplacian at half size) while ffmpeg runs
  and the others are deleted at once, so only a few spare frames are ever on disk. The sharpest
  candidate is saved under the moment's own number (frame N is still at start + (N−1)/fps). A
  moment whose best candidate is still under half the median sharpness of all candidates is moved
  to `_blurry/`. Blur comes from moving fast, usually at corners, so swapping a blurry frame for a
  sharp neighbour keeps the overlap exactly where it's most needed, instead of leaving a gap.
  `--candidates 1` (or no OpenCV) extracts one frame per moment and drops blurry ones as before.
- `extraction.json` keeps `stats` (length, target, auto, kept, replaced, dropped, dropped_at), and
  the stage prints them as `Frame stats: {…}` (again when frames are reused) for the run line.
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
| 1 | sequential: each frame vs its 15 neighbours | global (COLMAP 4+), else standard |
| 2 | 40 neighbours + jumps of 2, 4, 8… frames + loop closure (first 25 vs last 25 frames) | relaxed |
| 3 | exhaustive: every pair (only up to 300 frames, it's O(n²)) | relaxed |
| 4–5 | attempts 1–2 again with every frame, if you started with `--step 2` | |

Matches are kept in COLMAP's database between attempts, so later attempts only pay for new pairs.

The **global mapper** (`colmap global_mapper`, COLMAP 4.0+, GLOMAP's method) estimates every
camera's rotation and position at once, then refines everything together, instead of adding
cameras one at a time with a bundle adjustment after each. On a 250-frame walkaround (M1) it took
44 s instead of 2m03s for the same frames placed and the same reprojection error (#44). It reports
stages rather than images, so its progress bar follows the stages (rotation averaging, tracks,
positioning, bundle adjustment rounds, retriangulation), and its speed is learned separately
(`map_global` in the rates file). `--mapper incremental` (on `run` and `colmap`) turns it off; the
summary has a `mapper:` line and marks attempts that used it.
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
heights, is a warning; the frames on either side of the gap give the moment in the video.
With `run --stop-on-gaps` (the New run page passes it unless *Train even if a side wasn't filmed*
is ticked), a gap over 60° around the scene, or around any chosen object, stops the run after the
masks are prepared and before Brush: it prints `Stopped: coverage`, adds a `stopped:` line to
`summary.txt` and exits with code 3; the web UI shows the run as *stopped* with **Train anyway**,
which queues a `retrain` job (`--train-only`, the run's own settings, and `--masks --split-objects`
when it had several objects; `frames_of_run` finds their frames from the summary). Height is never
a reason to stop, and only angles around the object are measured, so an unfilmable underside
never stops a run.

Sides nobody could film are excused (`excuse_blocked`). COLMAP's `points3D.bin` (next to the
cameras) is split into 10° directions around "up"; a direction is *blocked* when it has no camera
within 10° and at least max(12, points/1000) points stand 0.6–1.3× the median camera distance away
(where a person filming would be), within one camera distance above or below the centre, with a
10th–90th percentile height span of at least 0.25× that distance. Flat floors and table tops never
qualify, nor does a wall farther away than people stood. "How far people stood" is per
direction (`local_distance`, interpolated between the cameras on either side, from each camera's
`flat` distance), because around a long object the ends are further from the centre than the
sides. The object never blocks itself: in a top-view grid (cells 0.04× the camera distance) the
cells whose points span some height are "standing"; the standing cells connected to the one
nearest the centre (within half the camera distance, growing across one-cell holes) are the
object, however long, and only standing cells apart from it can block. When no object outline is
found, blocking points must be at least 0.6× the camera distance away instead. Neighbouring blocked directions become
`blocked` arcs; `gap_open` is the largest gap once they count as covered, and `problems()` (the
warning and the stop) uses it. The raw `gap` is kept. The run card draws blocked arcs dashed grey,
and the viewer colours globe points with no camera nearby grey instead of red when their direction
is blocked (note: "Nobody could film this side"). The
run prints `Coverage: {json}` (the UI draws the ring from it) and saves `coverage.json`. The basis
(centre, up and two axes) and each camera's direction from the centre (`dirs`, model coordinates)
are saved with it. The viewer draws a small globe in a second WebGL canvas: a solid icosphere (so the cameras and
colours on the far side stay hidden) whose
vertices are coloured by the angle to the nearest camera direction (green up to 12°, amber to 30°,
red beyond), the cameras as points, all rotated by the model's orientation and seen from the main
camera's direction. The note uses the same angle for your current view direction.

Runs saved before `dirs` existed are measured again on first view. `GET /api/coverage` does that
in a background thread (one per run) and waits up to 1.5 s; if it isn't done it answers
`{"status": "computing"}`, and the viewer shows a spinner and asks again every 1.5 s until the
coverage arrives (or `"status": "failed"`, shown as a short note). The viewer asks as soon as it
knows the file, alongside loading the splat.

With several objects, each object's centre is where the rays through its mask centres meet
(least squares over ~90 sampled frames), and only the frames where its mask covers at least 0.2%
of the frame count, so occlusion by another object shows up as a gap. All rings share one basis.

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

**Suggested objects** (`masks.suggest`, `maskwork.pick_suggestions`). The preview process builds
SAM 2's automatic mask generator on first use (16 points per side). Masks between 0.4% and 50%
of the frame are taken largest first (a whole object before its parts, which often score higher),
and any mask that overlaps one already kept by more than half of the smaller is dropped, up to 8.
The result is cached per frame (`masks/suggest/<frame>.json`), so showing them again is free. Each comes back with a point inside it
(its centre, or the nearest mask pixel), which becomes the keep click when the suggestion is
clicked. The stand-in backend returns three fixed suggestions for tests.

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

## Turntable videos (`static/viewer.html`, `turntable.py`)

The viewer stops its render loop, sizes the canvas to the output (1080x1080 or 1920x1080, smaller
for GIFs), warms up for a few frames, then for each of fps x seconds steps puts the camera at the
next angle around the target, renders twice so the splat order settles, and keeps a JPEG. The
JPEGs go to `POST /api/turntable` as one stream; ffmpeg reads it as MJPEG and writes H.264 MP4
(or MPEG-4 if x264 is missing) or a palette-optimised GIF at 480 px. The view is restored after.

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
- `static/`: the Studio page (`index.html`: runs and library), New run (`new.html`: one or
  more videos and their settings, kept in `sessionStorage` while you visit Choose what to keep;
  defaults in `localStorage`; a batch posts one video job per video), and the viewer, with three.js and Spark bundled, so nothing is
  loaded from the internet.

## Testing (`tests/`)

- `tests/unit`: pure logic (planning, estimates, parsing, Brush settings, advice, library).
- `tests/integration`: real ffmpeg, plus stand-in `colmap` and `brush` executables
  (`tests/fakes/bin`) that print the real tools' output formats. The fake Brush rejects any
  option the real Brush doesn't have. The integration tests cover the stages, the full CLI
  pipeline, the HTTP API with real jobs, and the UI in a headless browser.

Real COLMAP and Brush are not run in CI: that would need a GPU and a long time per test.

## iPhone captures (`splat_studio/capture.py`, `trial.py`)

[Splat Capture](https://github.com/gpspelle/splat-capture) saves `capture_<date>.zip` with
`video.mov` (the sensor's landscape image, no rotation tag), `frames.jsonl` (per video frame: a
camera-to-world pose, row-major in ARKit's axes, `K = [fx, fy, cx, cy]` and the tracking state),
`points.json` (ARKit's feature points with colours) and `meta.json`. The format is specified in
that repository's `docs/capture-format.md`.

`run CAPTURE.zip` replaces stages 1 and 2:

1. The zip is extracted once to `captures/<name>/`.
2. About `--frames` frames are chosen evenly from those with `tracking: normal` (all frames when
   fewer than 30% are). ffmpeg picks them by frame number (`select=eq(n,…)`), scales them to
   `--max-size` and turns them upright into `frames/<name>/`; `frames/<name>/capture.json` keeps
   the plan, so a second run reuses the frames.
3. Each pose becomes a COLMAP camera: flip the camera's y and z (ARKit looks along −z with y up),
   then apply the same quarter turns as the image (new x = −old y, new y = old x; `fx`/`fy` swap,
   `cx' = h − cy`, `cy' = cx`). The number of turns is the one that puts world up (ARKit's +y) at
   the top of most frames. One PINHOLE camera with the median intrinsics.
4. `images/`, `sparse/0` (`cameras.bin`, `images.bin` without 2D points, `points3D.bin` from ARKit's
   points, topped up to 200 with a ball of grey points if there are fewer) and `dense/` (the same,
   no undistortion needed) are written, so coverage, masks and Brush work unchanged.

`--poses refine` then runs COLMAP feature extraction (PINHOLE with ARKit's lens), sequential
matching, `point_triangulator` with the poses fixed (the model is rewritten with the database's
image ids, read with `sqlite3`) and `bundle_adjuster` with the lens fixed. `--poses colmap` runs
stage 2 as for a video on the extracted frames.

`trial CAPTURE.zip` does all three into `workspace/<name>/trial_<date_time>/{arkit,refine,colmap}`
(not `run_*`, so the library ignores them), trains each with the same Brush settings (chosen from
the COLMAP result) plus `--eval-split-every 10`, and writes `results.json` and `report.md`: time to
place the cameras, training time, PSNR/SSIM on the held-out frames, and a one-line verdict
(within 0.5 dB counts as the same).

## Benchmark

`splat-studio bench RUN` (`splat_studio/bench.py`) measures settings on a finished run, on the
computer it runs on, without changing the run's results:

- **COLMAP**: five variants run from scratch on the run's prepared images (`RUN/images`), each in
  `RUN/bench/colmap/<variant>/`: the incremental mapper at full settings (1600 px, 8192 SIFT
  features, 15 neighbours), smaller features (1024 px, 4096), fewer pairs (8 neighbours), and the
  global mapper, the default since #44 (`colmap global_mapper`, COLMAP 4.0+; skipped on older
  versions). The others are compared with the incremental row. Variants with the same
  features copy the extracted database instead of extracting again. Each reports seconds for
  extraction, matching and mapping, frames placed in the largest model and its mean reprojection
  error (`colmap model_analyzer`).
- **Brush**: the run's own settings (`choose_brush_params`) plus `--eval-split-every 10` and
  `--eval-every steps/6`, exporting to `RUN/bench/brush/`. Brush prints
  `Eval iter N: PSNR x, ssim y` for the held-out frames; the time each line arrives gives a
  quality-over-time curve from a single training. Brush prints these through its logger, which
  hides info lines unless `RUST_LOG` allows them, so Brush runs that score frames get
  `RUST_LOG=brush_cli=info` (`brush.eval_env`). A second training at `--max-resolution 1024`
  runs when the images are larger than that.

Results go to `RUN/bench/results.json` and a Markdown table in `RUN/bench/report.md`.

