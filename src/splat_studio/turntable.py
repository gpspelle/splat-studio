"""Turntable videos: the viewer renders the model turning a full circle, one JPEG per frame,
and sends them here as one stream; ffmpeg turns them into an MP4 or a GIF.

The frames are rendered at a fixed step per frame, so the video is exactly as long as asked,
however fast the computer renders. Standard library plus ffmpeg.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

FORMATS = {"mp4": ".mp4", "gif": ".gif"}
GIF_WIDTH = 480


def encode(frames: Path, out: Path, fmt: str, fps: int) -> Path:
    """frames: concatenated JPEGs (an MJPEG stream). Writes out atomically; raises RuntimeError."""
    if fmt not in FORMATS:
        raise ValueError("format must be mp4 or gif")
    fps = max(5, min(60, int(fps)))
    src = ["ffmpeg", "-loglevel", "error", "-y", "-f", "image2pipe", "-framerate", str(fps), "-c:v", "mjpeg",
           "-i", str(frames)]
    tmp = out.with_name(out.stem + ".part" + out.suffix)
    if fmt == "gif":
        graph = (f"scale='min({GIF_WIDTH},iw)':-2:flags=lanczos,split[a][b];"
                 "[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4")
        tries = [[*src, "-vf", graph, "-loop", "0", str(tmp)]]
    else:
        common = ["-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
        tries = [[*src, *common, "-c:v", "libx264", "-crf", "20", "-preset", "medium", str(tmp)],
                 [*src, *common, "-c:v", "mpeg4", "-q:v", "3", str(tmp)]]       # ffmpeg built without x264
    err = ""
    for cmd in tries:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
            tmp.replace(out)
            return out
        err = r.stderr[-800:]
    tmp.unlink(missing_ok=True)
    raise RuntimeError("ffmpeg couldn't make the video: " + err)


def output_path(splat: Path, fmt: str) -> Path:
    return Path(splat).with_name(Path(splat).stem + "_turntable" + FORMATS[fmt])
