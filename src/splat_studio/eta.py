"""Time-left estimates for a whole run: frames, camera poses (COLMAP), training (Brush), cleanup.

Rules, learned the hard way:
  * Never show a made-up number. Until a step has run long enough to measure, or this computer
    has done that step before, the estimate is "estimating" (None), not a default guess.
  * Measure speed over a recent window, not since the step started: start-up time and changing
    speed (COLMAP's mapper slows down, Brush slows down while the splat grows) would skew it.
  * Training gets slower as the splat grows. Brush reports the splat count; step time is
    modelled as overhead + cost per splat, and the splat count is projected to where growth stops.
  * "Total left" covers every remaining part of the run, using how long those parts took on this
    computer before (timings.json). Without that history it says so instead of guessing.

The RunClock is fed progress observations with timestamps and answers: step_left, total_left
(seconds or None) and a note ("estimating", "finishing", "first_run", "more_attempts").
"""
from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path

MIN_STEP_ELAPSED = 8.0      # seconds of a step before its own speed is trusted
MIN_PROGRESS = 0.02
WINDOW_MIN = 20.0           # seconds; the speed window is max(WINDOW_MIN, WINDOW_SHARE * elapsed)
WINDOW_SHARE = 0.3
STALL = 30.0                # no progress for this long near the end of a step = "finishing"
MAP_EXPONENT = 1.5          # each image the mapper adds costs more than the last


def default_timings_file() -> Path:
    env = os.environ.get("SPLAT_STUDIO_TIMINGS")
    if env:
        return Path(env)
    rates = os.environ.get("SPLAT_STUDIO_RATES") or os.environ.get("RUN_COLMAP_RATES")
    if rates:                                   # keep tests (which redirect the rates file) hermetic
        return Path(rates).with_name("timings.json")
    return Path.home() / ".cache" / "splat-studio" / "timings.json"


# ---------------------------------------------------------------- history of this computer

class History:
    """Per-unit durations of finished steps on this computer, smoothed over runs."""

    def __init__(self, path: Path | None = None):
        self.path = path or default_timings_file()
        try:
            self.data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.data = {}

    def get(self, key: str) -> float | None:
        v = self.data.get(key)
        return float(v) if isinstance(v, (int, float)) and v > 0 else None

    def record(self, key: str, value: float) -> None:
        if not (value > 0 and math.isfinite(value)):
            return
        old = self.get(key)
        self.data[key] = value if old is None else 0.5 * old + 0.5 * value
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".part")
            tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True))
            tmp.replace(self.path)
        except OSError:
            pass


# COLMAP steps as the progress bar names them -> (history key, how work scales with frames)
COLMAP_STEPS = {
    "extracting features": ("colmap.extract", 1.0),
    "matching (sequential)": ("colmap.match_sequential", 1.0),
    "matching (loop closure)": ("colmap.match_loop", 0.0),
    "matching (exhaustive)": ("colmap.match_exhaustive", 2.0),
    "reconstructing poses": ("colmap.map", MAP_EXPONENT),
    "undistorting images": ("colmap.undistort", 1.0),
}
FIRST_ATTEMPT = ("extracting features", "matching (sequential)", "reconstructing poses", "undistorting images")


# ---------------------------------------------------------------- one step's live speed

class Window:
    """(time, work done) samples of one step; work is monotone in [0, total]."""

    def __init__(self, t0: float):
        self.t0 = t0
        self.samples: list[tuple[float, float]] = []
        self.last_change = t0

    def add(self, t: float, x: float) -> None:
        if self.samples and x <= self.samples[-1][1]:
            return
        self.samples.append((t, x))
        self.last_change = t
        if len(self.samples) > 4000:                 # keep memory bounded on very long steps
            self.samples = self.samples[::2]

    @property
    def x(self) -> float:
        return self.samples[-1][1] if self.samples else 0.0

    def rate(self, now: float) -> float | None:
        """Work per second over the recent window, or None if not measurable yet."""
        if not self.samples:
            return None
        # with few samples, the step's start is the only reference (includes start-up time)
        pts = self.samples if len(self.samples) >= 3 else [(self.t0, 0.0)] + self.samples
        elapsed = now - self.t0
        horizon = max(WINDOW_MIN, WINDOW_SHARE * elapsed)
        t_last, x_last = pts[-1]
        first = next(((t, x) for t, x in pts if t >= t_last - horizon), pts[0])
        if first == pts[-1]:
            first = pts[-2]
        # a step that has gone quiet is slowing down: count the silence as part of the window
        gaps = [b[0] - a[0] for a, b in zip(pts[-10:], pts[-9:])]
        typical = sorted(gaps)[len(gaps) // 2] if gaps else 0.0
        end = now if now - t_last > max(3 * typical, 5.0) else t_last
        dt, dx = end - first[0], x_last - first[1]
        if dt < 4.0 or dx <= 0:
            return None
        return dx / dt

    def left(self, now: float, total: float = 1.0) -> float | None:
        if now - self.t0 < MIN_STEP_ELAPSED or self.x < MIN_PROGRESS * total:
            return None
        r = self.rate(now)
        if not r:
            return None
        quiet = now - self.samples[-1][0]
        return max(0.0, (total - self.x) / r - min(quiet, 5.0))


# ---------------------------------------------------------------- training

class Training:
    """Brush training: step samples, splat counts, and a growth-aware time model."""

    def __init__(self, t0: float, total: int, growth_stop: int | None, max_splats: int | None):
        self.t0, self.total = t0, total
        self.growth_stop = growth_stop or total // 2
        self.max_splats = max_splats
        self.steps = Window(t0)
        self.splats: list[tuple[int, int]] = []       # (step, splat count)
        self.first_step_t: float | None = None

    def step(self, t: float, s: int) -> None:
        if s > 0 and self.first_step_t is None:
            self.first_step_t = t
        self.steps.add(t, s)

    def splat_count(self, s: int, n: int) -> None:
        if not self.splats or n != self.splats[-1][1]:
            self.splats.append((s or int(self.steps.x), n))

    def _projected_splats(self, s_from: int) -> list[tuple[int, float]]:
        """Splat count at future steps. Growth usually slows down, so the increments between
        refinements are extrapolated with their recent decay, until growth stops (or the cap)."""
        pts = self.splats[-8:]
        cur_step, cur = pts[-1]
        inc = [(b[1] - a[1]) / max(1, b[0] - a[0]) for a, b in zip(pts, pts[1:])]   # splats per step
        inc = [max(0.0, v) for v in inc]
        decay = 1.0
        ratios = [b / a for a, b in zip(inc, inc[1:]) if a > 0]
        if ratios:
            decay = min(1.0, max(0.3, sorted(ratios)[len(ratios) // 2]))
        span = max(1, (pts[-1][0] - pts[0][0]) // max(1, len(pts) - 1))           # steps per refinement
        slope = inc[-1] if inc else 0.0
        out, n, s_mark, k = [], float(cur), cur_step, 0
        step_size = max(1, (self.total - s_from) // 200)
        for st in range(s_from, self.total, step_size):
            while s_mark + span <= min(st, self.growth_stop):                       # refinements until st
                k += 1
                n += slope * (decay ** k) * span
                s_mark += span
            if self.max_splats:
                n = min(n, self.max_splats)
            out.append((st, n))
        return out

    def _cost_model(self, tau_now: float, n_now: float) -> tuple[float, float]:
        """Step time = overhead + per_splat * splats, fitted on the intervals between
        refinements when the splat count has varied enough, else 30% overhead assumed."""
        pts = []
        samples = self.steps.samples
        for (s0, n0), (s1, _) in zip(self.splats, self.splats[1:]):
            seg = [(t, x) for t, x in samples if s0 <= x <= s1]
            if len(seg) >= 2 and seg[-1][1] > seg[0][1]:
                pts.append((n0, (seg[-1][0] - seg[0][0]) / (seg[-1][1] - seg[0][1])))
        pts = pts[-12:]
        if len(pts) >= 4:
            ns = [p[0] for p in pts]
            if max(ns) > 1.3 * min(ns):
                mx, my = sum(ns) / len(ns), sum(p[1] for p in pts) / len(pts)
                var = sum((x - mx) ** 2 for x in ns)
                b = sum((x - mx) * (p[1] - my) for x, p in zip(ns, pts)) / var
                a = my - b * mx
                if b > 0 and a >= 0:
                    scale = tau_now / (a + b * n_now)        # anchor on the current measured speed
                    return a * scale, b * scale
        return 0.3 * tau_now, 0.7 * tau_now / n_now

    def left(self, now: float) -> float | None:
        s = int(self.steps.x)
        if s <= 0:
            return None
        live = self.steps.left(now, self.total)        # constant-speed extrapolation
        if live is None:
            return None
        if s >= self.growth_stop or len(self.splats) < 2 or self.splats[-1][1] <= 0:
            return live
        rate = self.steps.rate(now) or 0.0
        if rate <= 0:
            return live
        n_now = self.splats[-1][1]
        overhead, per_splat = self._cost_model(1.0 / rate, n_now)
        proj = self._projected_splats(s)
        total = 0.0
        for i, (st, n) in enumerate(proj):
            nxt = proj[i + 1][0] if i + 1 < len(proj) else self.total
            total += (nxt - st) * (overhead + per_splat * n)
        return max(live, total)


# ---------------------------------------------------------------- the whole run

RE_BRUSH_CMD_STEPS = re.compile(r"--total-train-iters\s+(\d+)")
RE_BRUSH_CMD_GROWTH = re.compile(r"--growth-stop-iter\s+(\d+)")
RE_BRUSH_CMD_MAX = re.compile(r"--max-splats\s+(\d+)")
RE_SPLAT_COUNT = re.compile(r"(?:Current splat count|Refine iter (\d+),)\s*(\d+)")


class RunClock:
    """Follows one job's output and estimates the time left for the step and for the run."""

    def __init__(self, kind: str = "video", history: History | None = None, frames_hint: int = 250,
                 clean_after: bool = True, isolate_after: bool = False, quality: str = "standard"):
        self.kind, self.quality = kind, quality
        self.h = history or History()
        self.frames = frames_hint
        self.clean_after, self.isolate_after = clean_after, isolate_after
        self.phase: str | None = None         # "frames" | colmap step label | "train" | "clean" | "isolate"
        self.phase_t0 = 0.0
        self.win: Window | None = None
        self.train: Training | None = None
        self.train_steps: int | None = None
        self.growth_stop: int | None = None
        self.max_splats: int | None = None
        self.attempt = 1
        self.done_colmap_steps: set[str] = set()


    # ---- feeding
    def start_phase(self, name: str, t: float, force: bool = False) -> None:
        if name == self.phase and not force:
            return
        self.end_phase(t)
        self.phase, self.phase_t0 = name, t
        self.win = Window(t)

    def end_phase(self, t: float) -> None:
        """Record how long the finished phase took, per unit, for next time."""
        p, dur = self.phase, t - self.phase_t0
        if p is None or dur <= 0:
            return
        if p in COLMAP_STEPS and self.win and self.win.x >= 0.999 and self.attempt == 1:
            key, power = COLMAP_STEPS[p]
            self.h.record(key, dur / max(1, self.frames) ** power)
            self.done_colmap_steps.add(p)
        elif p == "frames" and self.frames:
            self.h.record("frames", dur / self.frames)
        elif p == "train" and self.train and self.train.first_step_t and \
                self.train.steps.x >= self.train.total * 0.999:
            self.h.record(f"train.per_step.{self.quality}", (t - self.train.first_step_t) / self.train.total)
            self.h.record("train.startup", self.train.first_step_t - self.phase_t0)
        elif p in ("clean", "isolate"):
            n = self.final_splats()
            if n:
                self.h.record(f"{p}.per_msplat", dur / (n / 1e6))
            else:
                self.h.record(p, dur)
        self.phase = None

    def final_splats(self) -> int | None:
        return self.train.splats[-1][1] if self.train and self.train.splats else None

    def _cleanup_expected(self, p: str) -> float | None:
        per, n = self.h.get(f"{p}.per_msplat"), self.final_splats()
        if per and n:
            return per * n / 1e6
        if per and self.max_splats:                  # before training: assume the cap is reached
            return per * self.max_splats / 1e6
        return self.h.get(p)

    def progress(self, t: float, frac: float) -> None:
        if self.win is not None:
            x = frac ** MAP_EXPONENT if self.phase == "reconstructing poses" else frac
            self.win.add(t, x)

    def line(self, t: float, line: str) -> None:
        """Lines with facts the job parser doesn't track (Brush command, splat counts)."""
        if "brush" in line and "--total-train-iters" in line:
            m = RE_BRUSH_CMD_STEPS.search(line)
            self.train_steps = int(m.group(1)) if m else self.train_steps
            m = RE_BRUSH_CMD_GROWTH.search(line)
            self.growth_stop = int(m.group(1)) if m else None
            m = RE_BRUSH_CMD_MAX.search(line)
            self.max_splats = int(m.group(1)) if m else None
        m = RE_SPLAT_COUNT.search(line)
        if m and self.train:
            self.train.splat_count(int(m.group(1) or 0), int(m.group(2)))

    def set_attempt(self, t: float, n: int) -> None:
        if n != self.attempt:
            self.attempt = n
            if self.phase:
                self.start_phase(self.phase, t, force=True)

    def start_training(self, t: float) -> None:
        self.start_phase("train", t)
        self.train = Training(t, self.train_steps or 0, self.growth_stop, self.max_splats)

    def brush_step(self, t: float, step: int, total: int) -> None:
        if self.phase != "train" or not self.train:
            self.start_training(t)
        self.train.total = total
        self.train.step(t, step)

    # ---- estimates
    def _colmap_step_expected(self, label: str) -> float | None:
        key, power = COLMAP_STEPS.get(label, (None, 1))
        per = self.h.get(key) if key else None
        return per * max(1, self.frames) ** power if per else None

    def step_left(self, now: float) -> tuple[float | None, str | None]:
        p = self.phase
        if p is None:
            return None, "estimating"
        if p == "train" and self.train:
            if self.train.steps.x <= 0:                      # loading the dataset
                start = self.h.get("train.startup")
                rest = self._train_expected()
                return ((max(0.0, start - (now - self.phase_t0)) + rest) if start and rest else None,
                        None if start and rest else "estimating")
            left = self.train.left(now)
            if left is None:
                per = self.h.get(f"train.per_step.{self.quality}")
                if per:
                    return per * (self.train.total - self.train.steps.x), None
                return None, "estimating"
            return left, None
        if p in ("clean", "isolate"):
            exp = self._cleanup_expected(p)
            return (max(0.0, exp - (now - self.phase_t0)), None) if exp else (None, "estimating")
        w = self.win
        if w is not None and w.x >= 0.95 and now - w.last_change > STALL:
            return None, "finishing"
        live = w.left(now) if w is not None else None
        if live is not None:
            return live, None
        exp = self._colmap_step_expected(p) if p in COLMAP_STEPS else (
            self.h.get("frames") * self.frames if p == "frames" and self.h.get("frames") else None)
        if exp is not None:
            return max(0.0, exp - (now - self.phase_t0)), None
        return None, "estimating"

    def _planned_steps(self) -> int | None:
        if self.train_steps:
            return self.train_steps
        if self.train and self.train.total:
            return self.train.total
        try:                                        # what the pipeline will choose for this many frames
            from .brush import choose_brush_params
            return choose_brush_params(self.frames, None, self.quality)[0]["--total-train-iters"]
        except (ValueError, KeyError):
            return None

    def _train_expected(self) -> float | None:
        per = self.h.get(f"train.per_step.{self.quality}")
        steps = self._planned_steps()
        if not per or not steps:
            return None
        return per * steps

    def _future(self) -> tuple[float | None, str | None]:
        """Time for the phases after the current one, from history. None if unknown."""
        p, total, note = self.phase, 0.0, None
        if self.kind in ("clean", "isolate"):
            return 0.0, None
        order = (["frames"] + list(FIRST_ATTEMPT) if self.kind == "video" else []) + ["train"]
        if self.clean_after:
            order.append("clean")
        if self.isolate_after:
            order.append("isolate")
        if p in COLMAP_STEPS and p not in FIRST_ATTEMPT:          # a later COLMAP attempt
            idx = order.index("reconstructing poses")
        elif p in order:
            idx = order.index(p)
        else:
            idx = -1 if p is None else len(order)
        for q in order[idx + 1:]:
            if q == "frames":
                per = self.h.get("frames")
                v = per * self.frames if per else None
            elif q in COLMAP_STEPS:
                v = self._colmap_step_expected(q)
            elif q == "train":
                start, rest = self.h.get("train.startup") or 0.0, self._train_expected()
                v = start + rest if rest else None
            else:
                v = self._cleanup_expected(q)
            if v is None:
                return None, "first_run"
            total += v
        if self.kind == "video" and (p is None or p == "frames" or p in COLMAP_STEPS):
            note = "more_attempts"                    # COLMAP may need another attempt
        return total, note

    def estimate(self, now: float) -> dict:
        step, step_note = self.step_left(now)
        future, future_note = self._future()
        total = step + future if step is not None and future is not None else None
        return {"step_left": step, "total_left": total, "note": step_note or future_note}


def fmt_left(seconds: float | None) -> str | None:
    """Rounded the way people read a countdown: 40s, 3 min, 1 h 20 min."""
    if seconds is None:
        return None
    s = max(0, int(seconds))
    if s < 60:
        return f"{max(5, (s + 4) // 5 * 5)}s"
    m = (s + 30) // 60
    if m < 60:
        return f"{m} min"
    return f"{m // 60} h {m % 60:02d} min"
