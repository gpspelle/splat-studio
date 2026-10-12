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


def tip(level: str, code: str, text: str, **params) -> dict:
    """One piece of advice. `text` is English; `code` + `params` let the UI translate it."""
    return {"level": level, "code": code, "params": {k: str(v) for k, v in params.items()}, "text": text}


def video_advice(meta: dict | None) -> list[dict]:
    """Checks on an uploaded video. Each item: {level: ok|info|warn, code, params, text}."""
    if not meta:
        return [tip("warn", "video.unreadable", "Couldn't read this video. Is it a normal .mov / .mp4 file?")]
    tips = []
    d, long_side = meta["duration"], max(meta["width"], meta["height"])
    if d < 25:
        tips.append(tip("warn", "video.short_warn", f"Only {fmt_timestamp(d)} long, which usually means too few viewpoints. "
                        "Aim for 1½ to 4 minutes: walk slower and film 3 loops (low, middle, high).", d=fmt_timestamp(d)))
    elif d < 60:
        tips.append(tip("info", "video.short_info", f"{fmt_timestamp(d)} is on the short side. It can work for a small "
                        "object, but 1½ to 4 minutes with 3 loops is more reliable.", d=fmt_timestamp(d)))
    elif d > 600:
        tips.append(tip("info", "video.long", f"{fmt_timestamp(d)} is long. That works, since frames are sampled "
                        "evenly, but trim any parts that don't show the object.", d=fmt_timestamp(d)))
    if long_side and long_side < 1920:
        tips.append(tip("warn", "video.low_res", f"Low resolution ({meta['width']}×{meta['height']}). Record in 4K "
                        "(or at least 1080p) for sharp detail.", w=meta["width"], h=meta["height"]))
    if meta["fps"] and meta["fps"] < 24:
        tips.append(tip("warn", "video.low_fps", f"Low frame rate ({meta['fps']:g} fps) means more motion blur. "
                        "Record at 30 fps.", fps=f"{meta['fps']:g}"))
    if meta["hdr"]:
        tips.append(tip("info", "video.hdr", "HDR video: colours are converted automatically. For the most "
                        "faithful colours, turn off HDR Video (Settings → Camera → Record Video)."))
    if meta["portrait"]:
        tips.append(tip("info", "video.portrait", "Recorded in portrait. Fine for tall objects; for wide ones, "
                        "landscape keeps the whole object in frame more easily."))
    if not any(t["level"] == "warn" for t in tips):
        q = "4K" if long_side >= 3840 else f"{long_side}p" if long_side else ""
        tips.insert(0, tip("ok", "video.good", f"Looks good: {q} {meta['fps']:g} fps, {fmt_timestamp(d)} long.",
                           q=q, fps=f"{meta['fps']:g}", d=fmt_timestamp(d)))
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


def coverage_advice(cov: dict | None, fps: float | None = None, start: float = 0.0) -> list[dict]:
    """Advice from where the cameras were around the object (coverage.measure). With several
    objects, each object's own coverage, named."""
    from .coverage import problems
    if not cov:
        return []
    objs = cov.get("objects") or []
    tips: list[dict] = []
    for c, name in ([(o, o["name"]) for o in objs] or [(cov, None)]):
        found = problems(c)
        params = {"name": name} if name else {}
        pre = f"{name}: " if name else ""
        obj = "_obj" if name else ""
        if "gap" in found:
            g = c["gap"]
            a, z = frame_time(g["first"], fps, start), frame_time(g["last"], fps, start)
            when = f"{fmt_timestamp(min(a, z))} and {fmt_timestamp(max(a, z))}" if a is not None and z is not None else ""
            where = f" (the video passes it between {when})" if when else ""
            deg = f"{g['degrees']:.0f}"
            text = (f"{pre}One side of the object was barely filmed: no camera in a {deg}° arc around it{where}. "
                    "Walk all the way around at the same distance, or that side will look thin or blurry.")
            tips.append(tip("warn", f"run.coverage{obj}_gap_at", text, deg=deg, when=when, **params) if when
                        else tip("warn", f"run.coverage{obj}_gap", text, deg=deg, **params))
        if "height" in found:
            tips.append(tip("warn", f"run.coverage{obj}_height", f"{pre}The cameras were all at about the same height, "
                            "so the top and bottom of the object will be soft. Film three loops: low, eye level, then high.",
                            **params))
    if not any(t["level"] == "warn" for t in tips):
        tips.append(tip("ok", "run.coverage_ok", "The cameras went all the way around the object, at several heights."))
    return tips


def registered_ratio(text: str | None) -> float | None:
    """'230/250 frames (92%)' or '230/250 (92%)' -> 0.92."""
    m = re.search(r"(\d+)/(\d+)", text or "")
    return int(m.group(1)) / int(m.group(2)) if m and int(m.group(2)) else None


def run_advice(job: dict) -> list[dict]:
    """Lessons from a run's outcome, for recording the next one. Items: {level, code, params, text}."""
    tips = []
    if job.get("cleaned"):
        c = job["cleaned"]
        tips.append(tip("ok", "run.cleaned", f"Fog removed: kept {c['kept']:,} of {c['total']:,} splats. "
                        "The original is kept next to it.", kept=f"{c['kept']:,}", total=f"{c['total']:,}"))
    if job.get("isolated"):
        c = job["isolated"]
        tips.append(tip("ok", "run.isolated", f"Background removed: kept {c['kept']:,} of {c['total']:,} splats. "
                        "The original is kept next to it.", kept=f"{c['kept']:,}", total=f"{c['total']:,}"))
    tips += [t for t in job.get("coverage_advice") or [] if t["level"] != "ok"]
    status, error = job.get("status"), (job.get("error") or "")
    ratio = registered_ratio(job.get("registered"))
    breaks = job.get("track_breaks") or []
    frames, blurry = job.get("frames") or 0, job.get("blurry") or 0

    if blurry and frames and blurry / (frames + blurry) > 0.15:
        tips.append(tip("warn", "run.blurry", f"{blurry} of {frames + blurry} frames were blurry and set aside. "
                        "Move more slowly, add light, and lock focus (press and hold on the object).",
                        blurry=blurry, total=frames + blurry))
    if breaks:
        when = ", ".join(b["when"] for b in breaks[:4] if b.get("when"))
        where = f" around {when} in the video" if when else ""
        level = "warn" if (ratio or 0) < 0.9 else "info"
        text = (f"The camera track broke{where}. Look at those moments: usually a fast turn, "
                "motion blur, something passing in front, or the object leaving the frame.")
        tips.append(tip(level, "run.track_break_at", text, when=when) if when else tip(level, "run.track_break", text))
    if "Not good enough" in error or (ratio is not None and ratio < 0.6):
        tips.append(tip("warn", "run.too_few", "Too few frames could be placed to train on. Re-record in one "
                        "continuous take, walking slowly, with the whole object in frame. Avoid shiny, transparent "
                        "or plain textureless objects and backgrounds."))
    elif ratio is not None and ratio < 0.9:
        tips.append(tip("info", "run.partial", f"{ratio:.0%} of frames were placed. The splat is usable, but areas with "
                        "missing frames will look thin. Filming those moments again, more smoothly, fills them in.", pct=f"{ratio:.0%}"))
    elif status == "done" and ratio is not None and ratio >= 0.97 and not breaks:
        tips.append(tip("ok", "run.excellent", "Every frame was placed."))
    if "could not reconstruct anything" in error:
        tips.append(tip("warn", "run.nothing", "COLMAP found nothing to match. The video may be very blurry, too "
                        "dark, or show a plain/reflective surface. Put the object on a textured surface "
                        "(newspaper, patterned cloth) in soft, even light."))
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
