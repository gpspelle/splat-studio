"""Recording advice: checks on an uploaded video, and lessons from a finished run.

The aim is fewer re-shoots: catch obvious problems before processing, and when a run
struggles, point at the exact moments of the video that caused it.
"""
from __future__ import annotations

import json
import re
import subprocess
from fractions import Fraction
from pathlib import Path

from .common import fmt_timestamp

HDR_TRANSFERS = {"arib-std-b67", "smpte2084"}


# ---------------------------------------------------------------- uploaded video

def probe_video(path: Path) -> dict | None:
    """Basic facts about a video via ffprobe, or None if it can't be read."""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format",
                            str(path)], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return parse_probe(json.loads(r.stdout or "{}"))


def parse_probe(data: dict) -> dict | None:
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if not video:
        return None
    rotation = 0
    for sd in video.get("side_data_list", []) or []:
        if "rotation" in sd:
            rotation = int(float(sd["rotation"]))
    if not rotation and "rotate" in (video.get("tags") or {}):
        rotation = int(float(video["tags"]["rotate"]))
    w, h = int(video.get("width") or 0), int(video.get("height") or 0)
    if abs(rotation) % 180 == 90:
        w, h = h, w                                   # shown rotated: portrait phone video
    try:
        fps = float(Fraction(video.get("avg_frame_rate") or "0/1")) if video.get("avg_frame_rate") != "0/0" else 0.0
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0)
    return {"width": w, "height": h, "fps": round(fps, 2), "duration": duration,
            "codec": video.get("codec_name"), "hdr": video.get("color_transfer") in HDR_TRANSFERS,
            "portrait": h > w}


def video_advice(meta: dict | None) -> list[dict]:
    """Checks on an uploaded video. Each item: {level: ok|info|warn, text}."""
    if not meta:
        return [{"level": "warn", "text": "Couldn't read this video. Is it a normal .mov / .mp4 file?"}]
    tips = []
    d, long_side = meta["duration"], max(meta["width"], meta["height"])
    if d < 25:
        tips.append({"level": "warn", "text": f"Only {fmt_timestamp(d)} long. That's usually too few viewpoints - "
                     "walk slower and do 3 loops (low, middle, high): aim for 1½-4 minutes."})
    elif d < 60:
        tips.append({"level": "info", "text": f"{fmt_timestamp(d)} is on the short side. It can work for a small "
                     "object; 1½-4 minutes with 3 loops is more reliable."})
    elif d > 600:
        tips.append({"level": "info", "text": f"{fmt_timestamp(d)} is long. That's fine - frames are spread "
                     "evenly - but trim any parts that don't show the object."})
    if long_side and long_side < 1920:
        tips.append({"level": "warn", "text": f"Low resolution ({meta['width']}×{meta['height']}). Record in 4K "
                     "(or at least 1080p) for sharp detail."})
    if meta["fps"] and meta["fps"] < 24:
        tips.append({"level": "warn", "text": f"Low frame rate ({meta['fps']:g} fps) means more motion blur. "
                     "Record at 30 fps."})
    if meta["hdr"]:
        tips.append({"level": "info", "text": "HDR video: colours are converted automatically. For the most "
                     "faithful colours, turn off HDR Video (Settings → Camera → Record Video)."})
    if meta["portrait"]:
        tips.append({"level": "info", "text": "Recorded in portrait. Fine for tall objects; for wide ones, "
                     "landscape keeps the whole object in frame more easily."})
    if not any(t["level"] == "warn" for t in tips):
        q = "4K" if long_side >= 3840 else f"{long_side}p" if long_side else ""
        tips.insert(0, {"level": "ok", "text": f"Looks good: {q} {meta['fps']:g} fps, {fmt_timestamp(d)} long."})
    return tips


# ---------------------------------------------------------------- finished run

RE_FRAME_INDEX = re.compile(r"_(\d+)\.jpg$")


def frame_time(name: str, fps: float | None, start: float = 0.0) -> float | None:
    """Video time (s) of an extracted frame like 'IMG_0042_0123.jpg', given extraction fps."""
    m = RE_FRAME_INDEX.search(name)
    if not m or not fps:
        return None
    return start + (int(m.group(1)) - 1) / fps


def track_break_times(breaks: list[dict], fps: float | None, start: float = 0.0) -> list[dict]:
    out = []
    for b in breaks:
        a, z = frame_time(b["first"], fps, start), frame_time(b["last"], fps, start)
        out.append({**b, "from_s": a, "to_s": z,
                    "when": f"{fmt_timestamp(a)}–{fmt_timestamp(z)}" if a is not None and z is not None else None})
    return out


def registered_ratio(text: str | None) -> float | None:
    """'230/250 frames (92%)' or '230/250 (92%)' -> 0.92."""
    m = re.search(r"(\d+)/(\d+)", text or "")
    return int(m.group(1)) / int(m.group(2)) if m and int(m.group(2)) else None


def run_advice(job: dict) -> list[dict]:
    """Lessons from a run's outcome, for recording the next one. Items: {level, text}."""
    tips = []
    status, error = job.get("status"), (job.get("error") or "")
    ratio = registered_ratio(job.get("registered"))
    breaks = job.get("track_breaks") or []
    frames, blurry = job.get("frames") or 0, job.get("blurry") or 0

    if blurry and frames and blurry / (frames + blurry) > 0.15:
        tips.append({"level": "warn", "text": f"{blurry} of {frames + blurry} frames were blurry and set aside. "
                     "Move more slowly, add light, and lock focus (press and hold on the object)."})
    if breaks:
        when = [b["when"] for b in breaks if b.get("when")]
        where = f" around {', '.join(when[:4])} in the video" if when else ""
        tips.append({"level": "warn" if (ratio or 0) < 0.9 else "info",
                     "text": f"The camera track broke{where}. Look at those moments: usually a fast turn, "
                             "motion blur, something passing in front, or the object leaving the frame."})
    if "Not good enough" in error or (ratio is not None and ratio < 0.6):
        tips.append({"level": "warn", "text": "Too few frames could be placed to train on. Re-record in one "
                     "continuous take, walking slowly, with the whole object in frame. Avoid shiny, transparent "
                     "or plain textureless objects and backgrounds."})
    elif ratio is not None and ratio < 0.9:
        tips.append({"level": "info", "text": f"{ratio:.0%} of frames were placed - usable, but the missing parts "
                     "will look thinner. A smoother re-take of those moments would fill them in."})
    elif status == "done" and ratio is not None and ratio >= 0.97 and not breaks:
        tips.append({"level": "ok", "text": "Excellent capture: every frame was placed."})
    if "could not reconstruct anything" in error:
        tips.append({"level": "warn", "text": "COLMAP found nothing to match. The video may be very blurry, too "
                     "dark, or show a plain/reflective surface. Put the object on a textured surface "
                     "(newspaper, patterned cloth) in soft, even light."})
    return tips


# ---------------------------------------------------------------- extraction facts saved with frames

def save_extraction_info(frames_dir: Path, fps: float, start: float, duration: float, video: str) -> None:
    (frames_dir / "extraction.json").write_text(json.dumps(
        {"fps": fps, "start": start, "duration": duration, "video": video}, indent=1))


def load_extraction_info(frames_dir: Path) -> dict:
    try:
        return json.loads((frames_dir / "extraction.json").read_text())
    except (OSError, ValueError):
        return {}
