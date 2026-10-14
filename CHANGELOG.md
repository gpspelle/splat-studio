# Changelog

## Unreleased

### Added
- **Sides nobody could film don't count as missing** ([#36](https://github.com/gpspelle/splat-studio/issues/36)).
  When COLMAP's 3D points show something standing where you'd have had to film from (the object
  is against a wall, in a corner, next to another car), that side no longer warns or stops the run.
  It shows dashed grey on the ring and grey on the viewer's globe, with a note.
- **Runs stop before training when a side wasn't filmed** ([#34](https://github.com/gpspelle/splat-studio/issues/34)).
  The coverage check right after the camera poses now ends the run there, instead of after a full
  training, unless *Train even if a side wasn't filmed* is ticked on New run. The run keeps its
  camera poses: **Train anyway** trains it with the same settings. Filming from one height stays a
  warning, and an unfilmable underside never stops a run. Terminal: `run --stop-on-gaps`.
- **`splat-studio bench RUN`** ([#32](https://github.com/gpspelle/splat-studio/issues/32)): measures, on
  your own computer and a finished run, how long COLMAP takes with smaller features, fewer matched
  neighbours and the global mapper (COLMAP 4+), and how Brush's quality on held-out frames grows over
  training time, also at a lower training resolution. Writes `bench/report.md` in the run folder.

### Fixed
- **The viewer's coverage no longer appears late with no sign of life** ([#30](https://github.com/gpspelle/splat-studio/issues/30)).
  Older runs are measured again on first view, which took long enough with several objects that the
  request gave up and the panel stayed empty until a later visit. Measuring now happens in the
  background; the panel shows a spinner and *Measuring coverage…* until it's ready, and says so if
  it couldn't be measured.

### Changed
- **The coverage globe is solid.** From the side nobody filmed, the camera dots on the far side no
  longer show through the red.
- **The viewer's coverage is a 3D globe** ([#28](https://github.com/gpspelle/splat-studio/issues/28)).
  The flat ring's marker drifted: it measured from a different centre than the viewer turns around,
  used a different "up", and ignored height. The globe turns with the view (dragging, auto-rotate,
  Flip, Tilt): cameras are dots, the surface is green near them and red where none came within 30°,
  and the side facing you is the side you're looking at. `coverage.json` stores each camera's
  direction from the centre (`dirs`); older runs are measured again when first opened.

### Added
- **Coverage per object, and in the viewer** ([#26](https://github.com/gpspelle/splat-studio/issues/26)).
  With several objects, each gets its own coverage ring, measured around the object's own centre
  (where the rays through its mask centres meet) and counting only the frames where it's visible;
  warnings name the object. The viewer shows a small, toggleable coverage map with a marker for
  the direction you're viewing from, and says when that side was barely filmed. Object splats
  show their object's coverage. `GET /api/coverage?file=`; `coverage.json` now has a basis, and
  older runs are measured on demand.

### Fixed
- **Suggest objects split one object in two, and recomputed on hide/show**
  ([#24](https://github.com/gpspelle/splat-studio/issues/24)). Larger masks now come first, so a
  whole object wins over its parts; Shift-click adds a suggestion to the selected object. Hiding and
  showing suggestions is instant, and they're cached per frame on disk (`masks/suggest/<frame>.json`).
- **Choose what to keep: the last object and its clicks couldn't be removed**
  ([#22](https://github.com/gpspelle/splat-studio/issues/22)). The last object now has a remove
  button too (leaving one empty object to start again). **Undo click** takes back the latest click
  wherever it was, going to its frame, and **Start over** removes every click and object.

## 0.4.0 — 2026-10-12

The first published release. It includes the design system that was numbered 0.4.0 but never released.

### Added
- **Suggested objects** ([#15](https://github.com/gpspelle/splat-studio/issues/15), beta). **Suggest
  objects** on Choose what to keep asks SAM 2's automatic masks for the distinct objects on the
  current frame (up to 8, filtered by size and quality, no duplicates) and outlines them with
  numbers. Clicking one keeps it: as a new object in Several objects mode. `POST /api/keep/suggest`.
- **Batch queue for many videos** ([#14](https://github.com/gpspelle/splat-studio/issues/14)). New run
  accepts several videos at once (drop, choose or add earlier uploads), each listed with its checks
  and a **Remove** button. **Start N runs** queues one run per video with the same settings. With
  several videos, *What to keep* is Everything. **Save these settings as defaults** remembers
  quality, detail, options and advanced settings for new visits.
- **Turntable video** ([#13](https://github.com/gpspelle/splat-studio/issues/13)). The viewer records
  the model turning a full circle from the current view and downloads it as MP4 or GIF (6 or 10 s,
  square or wide). Frames are rendered at a fixed step and sent as JPEGs; ffmpeg encodes them
  (`POST /api/turntable`), saving `NAME_turntable.mp4`/`.gif` next to the splat.
- **Coverage check after camera poses** ([#12](https://github.com/gpspelle/splat-studio/issues/12)).
  Before training, the run measures where the cameras were around the object and shows a
  coverage ring on its card (one dot per camera, the largest gap in red). A gap over 60° or a
  single height gives a warning that names the moment in the video. Also
  `./splat-studio coverage RUN`, and `coverage.json` plus a summary line in each run.
- **Fix one slipped frame without redoing every frame** ([#11](https://github.com/gpspelle/splat-studio/issues/11)).
  After *Apply to all frames*, clicks added on a frame mark it as changed, and **Update N
  changed frame(s)** redoes only the frames around it: for each object clicked there, from the
  previous frame it was clicked on to the next one. Changes survive a reload. `mask --prompts`
  accepts `"changed": [frames]` for the same thing on the command line.
- **Several objects: one splat each (beta).** **What to keep** on New run has **Everything**, **One
  object** and **Several objects**. With several, the Choose what to keep page lists the objects
  (**+ Add object**, up to 8), each with a name and its own colour; clicks go to the selected
  one. SAM 2 follows each object through the video. Training runs once with all of them kept,
  then the splat is split into `NAME_part-<object>.ply` files by projecting it into 48 frames
  and giving each splat to the object whose mask it lands in most often. Finished runs show a
  button per object. New: `run --split-objects`, `split-objects NAME.ply --run RUN`, object
  labels 1–8 in `mask --prompts`, and a `split_objects` option for video jobs.
- **Choose what to keep (beta)** (`/keep`, from **What to keep → Only what I
  choose** on New run). Click the object (**Keep**) and a stand, hand or holder (**Leave out**) on a frame or
  two. Meta's SAM 2 follows that selection through every frame, and training keeps only the
  chosen object: Brush trains what's outside the masks to be transparent. Install it with
  `./splat-studio setup --masking` (its own environment, about 1 GB). New commands and options:
  `./splat-studio mask FRAMES --prompts JSON`, `run --masks [--mask-mode transparent|ignore]`;
  API `GET /api/frames`, `POST /api/keep/preview`, and job types `frames` and `mask`. Runs with
  masks show a **your selection** tag. Runs made before this need COLMAP again to use masks.
- **History page** (`/history`) for finished runs: grouped by day, with search by video name,
  a filter for finished, failed or stopped runs, and **Remove from list**, which deletes only
  the job record and log (`DELETE /api/jobs/<id>`), never splats or run folders.
- **Turn each model on its own in the comparison.** A bar under each model's name turns only
  that model (↺ 15°, ↻ 15°, Flip, Tilt 90°, Reset). **Turn model** is a switch, on for both
  models when the comparison opens: on, dragging in that view turns the model; off, the model
  is locked and dragging doesn't turn it (wheel zoom and right-drag pan still work). Cameras
  stay linked. **Save view** remembers both orientations for the viewer, and exports keep them.
- **Site header and footer** on the Studio page and the recording guide: a logo, navigation
  (Studio, Library, Recording guide, Docs), a GitHub link, and a footer with help links,
  bug reports and contact (GitHub issues), the version, the license and credits. Built by
  `static/site.js`; styles in section 13 of `design.css`. The app also has a favicon.
- The recording guide is now its own page, `/guide`, instead of a pop-up.
- **Delete from the library.** Each library splat has a **Delete** button that asks for
  confirmation, then removes that one file from disk (`DELETE /api/splats?file=…`). The run
  folder, frames and other splats stay. Files a queued or running job still uses are refused
  with error code `splat_busy`.

- **Design system "Instrument"** (`static/design.css`, documented in `docs/design-system.md`):
  colour, type, spacing, shape and motion tokens, plus buttons, segmented controls, checkboxes,
  panels, status, progress, dialogs and the viewer HUD. Used by every page and exported website.
- **Light and dark themes.** Follows the computer's setting, with a Theme menu (System / Light / Dark)
  remembered per browser.
- Bundled fonts: Inter and JetBrains Mono (SIL Open Font License); still nothing loads from the internet.
- Accessibility: WCAG 2.2 AA contrast for all text (checked by `tests/unit/test_design.py`),
  visible keyboard focus, 24px+ targets, keyboard-operable drop zone, reduced-motion support.
- `/api/setup` also returns the app `version` (shown next to the name).

### Changed
- **Library pictures show the object.** Runs trained on a selection use a frame where the
  selection is large and fully in view, cropped around it with the rest blurred and darkened
  (`run/thumbs/run.jpg`), instead of the video's first frame. With several objects, each
  object's splat gets its own picture from its masks. Older runs get theirs the first time the
  library lists them.
- **Choose what to keep prepares the frames by itself** when it opens (using the frame count
  from New run), instead of waiting for a **Prepare frames** click. Opening the page twice
  reuses the frames job that's already running.
- **New run is its own page** (`/new`, **New run** in the menu and on the Studio). The Studio
  (`/`) now shows only the runs and the library; starting a run goes back there. New run keeps
  the video and settings while you go to Choose what to keep and back. **Train again** has its
  own **Remove the background** option.
- **What to keep** is a choice on New run, next to Quality and Detail: **Everything** or **Only
  what I choose**. The Choose what to keep page no longer starts runs: **Use this selection**
  returns to New run, where the rest of the settings are.
- **The Studio's run list stays short.** Runs in progress keep the full card. Finished runs
  are one line (name, result, **View splat**, any error or warning) and open with **Details**.
  Only the finished runs from the last day are kept there (at least 3, at most 5); a link
  leads to the rest on the History page. Run rows are now drawn by `static/runs.js`, shared
  by both pages, with styles in section 14 of `design.css`.
- README screenshots retaken with fog removal always on. A new CI check
  (`scripts/check_docs.py`) fails PRs that change user-facing code without touching the
  README or `docs/`, and `AGENTS.md` / `CLAUDE.md` tell coding agents to keep both current.
- **Fog removal always runs.** Every run now saves `NAME_clean.ply` without the background
  fog, and **View splat** opens it; a **With fog** button opens the original, which is still
  kept. The *Remove background fog* checkbox is gone, and `splat-studio run --clean` is
  accepted but no longer needed. The library's **Remove fog** button only shows on splats
  that don't have a clean copy yet (imports and older runs).
- Rewrote the interface text in English and Portuguese to be shorter and plainer, without
  arrows, dashes and repeated asides. The advice the server writes was updated to match.
- The recording guide no longer opens by itself on the first visit, and the Studio links to
  it from the menu and from runs that need a better capture, instead of from four places.
- **Library grouped by video.** The splat library shows one collapsible group per source video
  (all runs of that video together), newest first, with imported files in their own group.
  Library items have a new `video` field (the video's name, or `null` for imports).

- New look for the Studio page, viewer and comparison page. Element ids, test ids and all
  behaviour are unchanged. Exported websites now also contain `design.css` and `fonts/`.

### Fixed
- **Choose what to keep showed each preview one click late.** The selection tool's "ready"
  line was taken as the answer to the first click, so every later click got the previous
  click's mask, often from another frame, where it looked shifted or rotated. Replies are now
  matched to their click, and clicks made at the same time no longer swap masks.

- **Time left is measured, and covers the whole run.** The old figures only covered part of
  the run (COLMAP alone, or Brush's own guess during training), started from built-in M1
  defaults, averaged speed since the step began, and said "0s" while COLMAP was still
  finishing. Now (`splat_studio/eta.py`):
  - each step's time left is measured over a recent window, so start-up time and slow-downs
    don't skew it; until it can be measured the UI says *measuring speed*, and at the end of a
    COLMAP step that is still working it says *finishing up*;
  - training accounts for Brush slowing down as the splat grows, from the splat counts Brush
    reports;
  - **whole run** adds the remaining parts (camera poses, training, fog removal) from how long
    they took on this computer before (`~/.cache/splat-studio/timings.json`). Before the first
    full run it says so instead of guessing.
- Jobs have new fields `eta_total` and `eta_note`; `eta` now holds this step's measured time
  left ("3 min"), and `eta_worst` is no longer filled. Each job also writes `timeline.jsonl`
  (progress with timestamps) next to its log, to check estimates against.

## 0.3.0

### Added
- **Compare two splats.** Pick two splats in the library with **Compare** and open them side
  by side (stacked on phones). Cameras are linked, relative to each model's size, with a toggle
  to move them separately. **Export website** downloads a self-contained comparison site
  (`/api/export-compare`). New page: `/compare?a=…&b=…`.
- **Background removal.** `splat-studio isolate MODEL.ply` saves `MODEL_object.ply` with only
  the object: it removes the surface it stands on (found along the model's up axis) and keeps
  the connected blob around the object. The original file is never modified.
- In the app: a **Remove the background** option for new runs (off by default), a **Remove
  background** button on library splats, and an **Object only** button on runs that have one.
- `splat-studio run --remove-background`. Jobs have new fields `object_splat` and `isolated`,
  a new job type `isolate`, and library items a new `isolated` flag.

### Changed
- Fog removal without camera positions now finds the object as the densest connected cluster
  of opaque splats, so opaque walls no longer pull the kept region away from the object.

### Fixed
- One unreadable frame no longer ends the COLMAP stage when OpenCV isn't installed (the ffmpeg
  and sips resize paths now leave such a frame as is, like the OpenCV path already did). This
  was also why the integration tests failed on GitHub Actions.

### Compatibility
- Nothing existing was renamed or removed: all 0.2.0 fields, options, outputs and exported
  sites are unchanged, and the new options default to off.

## 0.2.0

### Added
- **Fog removal.** `splat-studio inspect MODEL.ply` explains what's in a splat (background
  share, faint "fog" splats, framing). `splat-studio clean MODEL.ply` saves `MODEL_clean.ply`,
  keeping only the object; the original file is never modified. When the splat comes from a
  Splat Studio run, the object is located from the COLMAP camera positions.
- In the app: a **Remove background fog** option for new runs, a **Remove fog** button on
  every library splat, and a **Without fog** button on runs that have a clean copy.
- **Crop** control in the viewer, also saved with **Save view** and in exported websites.
- **Portuguese (Brazil)** translation of the whole app, viewer, run advice and errors, with a
  language switcher (remembered per browser; defaults to the browser's language).
- `splat-studio run --clean`.

### Changed
- The viewer frames the camera on the object (its opaque splats) instead of on every splat,
  so models with a foggy background no longer open with the camera inside the fog.
- Advice items and API errors now also carry a `code` (and advice `params`) for translation.
  Existing fields (`level`, `text`, `error`) are unchanged.

### Fixed
- Splats whose header says `Vertical axis: y` (Brush writes this when it has no estimated up
  direction) were shown upside down.
- Compressed PLY files now get a clear "not supported" message.
- Two runs started within the same second no longer collide on the same folder.

### Compatibility
- Runs, job records and exported websites from 0.1.0 keep working
  (see `tests/integration/test_compat.py`). Exported sites without crop are byte-for-byte
  the same config as before.

## 0.1.0
- First release: video → frames → COLMAP → Brush, local web UI, viewer, website export,
  recording guidance, setup, tests.
