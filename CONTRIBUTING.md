# Contributing

## Setup

No dependencies are needed to run the app or the tests, only Python 3.9+ and ffmpeg.

```
git clone https://github.com/gpspelle/splat-studio.git
cd splat-studio
./splat-studio --help
```

Optional extras: `pip install opencv-python-headless` (blur filter) and
`pip install playwright && python -m playwright install chromium` (browser tests).

## Tests

```
python3 -m unittest discover -s tests -t .            # everything (~1 min)
python3 -m unittest discover -s tests/unit -t .       # unit tests only (< 1 s)
python3 -m unittest tests.integration.test_web_ui     # browser tests (needs Playwright)
```

pytest works too (`pytest`), if you have it.

The integration tests use stand-in `colmap` and `brush` programs from `tests/fakes/bin`.
They print the same output formats as the real tools, so the progress parsing is tested for
real. The fake Brush only accepts options listed in `tests/fakes/brush_flags.txt`, which is
extracted from Brush's source. When upgrading Brush, regenerate that list and bump
`BRUSH_REF` in `src/splat_studio/brush.py` and `docker/Dockerfile`.

Useful knobs for the fakes: `FAKE_COLMAP_REGISTER=0.8` (simulate a broken track),
`FAKE_COLMAP_DELAY`, `FAKE_BRUSH_DELAY`.

## Layout

```
src/splat_studio/
  frames.py        stage 1: video -> frames
  colmap.py        stage 2: self-correcting COLMAP
  progress.py      progress bars and time estimates for stage 2
  brush.py         stage 3: Brush settings, finding/building Brush
  pipeline.py      all stages in one command (splat-studio run)
  setup_deps.py    dependency check/install (macOS)
  guidance.py      recording advice
  server/          web UI: app.py (HTTP), jobs.py (queue), library.py (files)
  static/          Studio page, viewer, bundled three.js + Spark
tests/unit, tests/integration, tests/fakes
docs/              recording guide, how it works, screenshots
scripts/readme_screenshots.py   regenerates docs/images
docker/            experimental container
```

## Conventions

- Standard library only at runtime. Vendored front-end libraries are pinned files in `static/vendor`.
- Lines that the UI parses (`==> 2/3 Camera poses (COLMAP): …`, the progress bar, `Done. Splat: …`)
  are marked with a comment where they are printed. Change them together with `server/jobs.py`
  and the tests.
- Screenshots: `python3 scripts/readme_screenshots.py` (needs Playwright).
