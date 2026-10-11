# Splat Studio

**Turn a phone video into a 3D Gaussian splat on your own Mac, then put it on a website.**

Film an object by walking around it, drop the video into Splat Studio, and get a 3D model
you can orbit, zoom and share. Everything runs locally: no uploads, no accounts, no cloud GPU.

![Splat Studio](docs/images/studio.png)

It wires together three open-source tools and handles the fiddly parts for you:

| Step | Tool | What Splat Studio adds |
|---|---|---|
| Video → frames | ffmpeg | even sampling, iPhone HDR conversion, blurry-frame filtering |
| Frames → camera poses | [COLMAP](https://colmap.github.io/) | retries automatically when tracking breaks, time estimates, one folder per run |
| Poses → splat | [Brush](https://github.com/ArthurBrussee/brush) | settings chosen from your footage, built automatically on first use |
| Splat → website | [Spark](https://sparkjs.dev) + three.js | viewer with auto-upright, one-click export of a self-contained site |

## Contents

- [Requirements](#requirements)
- [Install](#install)
- [Quick start](#quick-start)
- [Recording a good video](#recording-a-good-video)
- [Using the app](#using-the-app)
- [Command line](#command-line)
- [Putting a splat on your website](#putting-a-splat-on-your-website)
- [Troubleshooting](#troubleshooting)
- [Development](#development)

## Requirements

- A Mac with Apple Silicon (M1 or newer) is the main target: Brush trains on its GPU through Metal.
  Linux with an NVIDIA GPU should also work, but you install the tools yourself.
- Python 3.9 or newer (the one that comes with macOS is fine).
- About 5 GB of free disk space for the tools, plus room for your videos.

Everything else (Homebrew, ffmpeg, COLMAP, Rust, Brush) is installed by `./splat-studio setup`.

## Install

```bash
git clone https://github.com/<you>/splat-studio.git
cd splat-studio
./splat-studio setup
```

`setup` checks what's missing and installs it. It may open a macOS window to install the
Xcode command-line tools, and ask for your Mac password to install Homebrew. Then it downloads
and builds Brush, which takes 10–30 minutes, once. Run `./splat-studio check` at any time to
see what's installed.

> Prefer `pip`? `pip install .` gives you a `splat-studio` command. The `./splat-studio`
> script does the same without installing anything.

## Quick start

```bash
./splat-studio
```

Your browser opens Splat Studio at <http://localhost:8765>. Then:

1. Read the **Recording guide** (it opens on your first visit) and film your object.
2. Drop the video onto **New run**. Splat Studio checks it and warns you about anything likely to fail.
3. Click **Start run** and watch the progress: frames → camera poses → training.
4. Click **View splat** to look at the result, and **Export website** to publish it.

Your videos and results are kept in `~/SplatStudio` (change it with `--data-dir`).

## Recording a good video

The quality of the video decides almost everything. In short:

- **One continuous take, three loops** around the object: low, eye level, high. Walk slowly,
  1½–4 minutes in total.
- **Soft, even light.** Lock focus and exposure (press and hold on the object). Stay on the
  1× lens and never zoom.
- **A matte object on a textured surface**, with nothing moving in the scene. Avoid glass,
  chrome, plain white tables and turntables.

<img src="docs/images/upload-checks.png" width="380" align="right" alt="Checks on an uploaded video">

The full guide is in the app and in **[docs/capture-guide.md](docs/capture-guide.md)**.

Splat Studio helps you get it right with fewer attempts:

- **Before processing**, it checks each upload's length, resolution, frame rate, HDR and
  orientation, and tells you what to fix.
- **After a run**, if tracking broke, it tells you **at which seconds of the video** it
  happened, so you know exactly which part to re-film.
- It flags blurry footage, and runs that couldn't be reconstructed, with concrete advice.

<br clear="right">

## Using the app

### Runs

![A run in progress](docs/images/run-progress.png)

Each run shows its stage, the current step, and the time left. Estimates get accurate after
the first run on your Mac, because measured speeds are remembered. Runs are queued and
processed one at a time. **Details** shows each COLMAP attempt, where tracking broke, the Brush
settings used, and the full log.

**Train again** re-trains a finished run with other settings, reusing its camera poses (no
COLMAP). Every splat is kept, so you can compare.

### Settings

| Setting | Options | Notes |
|---|---|---|
| Quality | Preview · Standard · High | Preview is ~4× faster, good for checking a capture. High takes longer and gives finer detail. |
| Detail | Auto · Low · Normal · High | Auto judges it from how much texture the footage has. |
| Measure quality | on/off | Holds back every 10th frame and reports PSNR/SSIM, so runs can be compared with a number. |
| Advanced | frames, trim start/end, extra Brush options | e.g. trim the first seconds while you were pressing record. |

### Viewer

![Viewer](docs/images/viewer.png)

Drag to orbit, scroll to zoom, right-drag to pan. The model is stood upright automatically,
using the "up" direction Brush saves in the file. If it's still tilted, use **Flip**,
**Tilt** or **Roll** and then **Save orientation**. You can also import any `.ply`, `.spz`,
`.splat` or `.ksplat` file into the library.

## Command line

Everything in the app is also available in the terminal:

```bash
./splat-studio run IMG_1234.MOV                        # video -> splat
./splat-studio run IMG_1234.MOV --quality preview      # quick look
./splat-studio run IMG_1234.MOV --start 0:03 --end 1:45
./splat-studio run --train-only workspace/IMG_1234/latest --quality high   # re-train, skip COLMAP
./splat-studio run IMG_1234.MOV -- --sh-degree 1       # anything after -- goes to Brush
./splat-studio run --help
```

Paths are relative to the current folder. Each run lands in `workspace/<video>/run_<date_time>/`:

```
dense/         camera poses + undistorted images (what Brush trains on)
splat/         the trained .ply files
summary.txt    settings, each COLMAP attempt, track breaks, the Brush command, the result
colmap.log     COLMAP's full output
```

You can also run single stages: `./splat-studio frames VIDEO` and `./splat-studio colmap FRAMES_DIR`.

## Putting a splat on your website

**Export website** (in the viewer or the library) downloads a zip:

```
index.html            the viewer
model.ply             your splat
viewer-config.json    title and orientation
vendor/               three.js + Spark (no CDN needed)
```

Upload the folder to any static host (GitHub Pages, Netlify, Cloudflare Pages, S3, your own
server) and it works as is. Opening `index.html` straight from your disk won't work, because
browsers block that; preview it locally with `python3 -m http.server`.

Large splats load slowly on phones. Keep **Quality** at Standard, or compress the `.ply` with
[SuperSplat](https://superspl.at/editor) (crop away the background, export compressed) before exporting.

## Troubleshooting

| Problem | What to do |
|---|---|
| "Not good enough to train on" | The camera track broke too often. Check the advice on the run: it names the seconds where it broke. Re-film more slowly, in better light, on a textured surface. |
| The model has holes or blurry sides | Those angles weren't filmed, or their frames weren't placed. Add the missing loop (usually the high one). |
| Floating specks around the model | Normal for splats. Crop them in [SuperSplat](https://superspl.at/editor). |
| The model is upside down or sideways | **Flip** / **Tilt** / **Roll** in the viewer, then **Save orientation**. |
| A tool is missing | Click the tools badge (top right) or run `./splat-studio check`, then `./splat-studio setup`. |
| `command not found: colmap` after installing | Open a new terminal, or run `./splat-studio setup` again: it adds Homebrew and Rust to the path. |
| Time estimates are rough (`~`) | They're defaults until your Mac has been timed once. From the second run on, they use your Mac's speed. |

## Development

```bash
python3 -m unittest discover -s tests -t .      # all tests, no extra packages needed
```

The tests run the real pipeline code with real ffmpeg, plus stand-ins for COLMAP and Brush
that print the real tools' output formats. The fake Brush rejects any option the real Brush
doesn't have. Browser tests run when Playwright is installed.
See **[CONTRIBUTING.md](CONTRIBUTING.md)** and **[docs/how-it-works.md](docs/how-it-works.md)**.

An experimental Docker setup is in [`docker/`](docker/). It's mostly useful on Linux with an
NVIDIA GPU: on a Mac, Docker can't use the Apple GPU.

## Credits

Built on [COLMAP](https://colmap.github.io/), [Brush](https://github.com/ArthurBrussee/brush),
[Spark](https://sparkjs.dev), [three.js](https://threejs.org) and [ffmpeg](https://ffmpeg.org).
Released under the [MIT license](LICENSE).
