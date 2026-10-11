# Changelog

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
