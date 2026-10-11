"""Progress bars and time estimates for the COLMAP stage.

COLMAP's own log counters drive the bar. Estimates account for the number of image pairs
to match, how many feature points the images have, and that reconstruction slows down as
it goes. They start from rough defaults (shown with "~"), switch to this computer's
measured speeds as steps finish, and remember those speeds for the next run.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Callable

from .common import fmt_time

# Seconds per unit of "work" for each kind of step:
#   extract / undistort: per image
#   match / match_exhaustive: per image pair x (features per image / 1000)^2
#   map: per (number of images)^1.5
# Rough defaults for an Apple M1 CPU at 1600 px. Used only until something better is known:
# speeds measured in this run, then speeds saved from earlier runs, then these defaults
# scaled by how fast this run's feature extraction was.
DEFAULT_FEATURES = 8000
PRIOR_RATES = {
    "extract": 0.4,
    "match": 0.04 / (DEFAULT_FEATURES / 1000) ** 2,
    "match_exhaustive": 0.04 / (DEFAULT_FEATURES / 1000) ** 2,
    "map": 0.6 / 250 ** 0.5,
    "undistort": 0.15,
}
MAP_EXPONENT = 1.5


def default_rates_file() -> Path:
    env = os.environ.get("SPLAT_STUDIO_RATES") or os.environ.get("RUN_COLMAP_RATES")
    return Path(env) if env else Path.home() / ".cache" / "splat-studio" / "rates.json"


def bar(frac: float, width: int = 24) -> str:
    frac = max(0.0, min(1.0, frac))
    full = int(frac * width)
    return "█" * full + "░" * (width - full)


RE_BLOCK2 = re.compile(r"(?:Processing|Matching) block \[(\d+)/(\d+),\s*(\d+)/(\d+)\]")
RE_REGISTER = re.compile(r"Registering image #\d+\s*\([^)]*?(\d+)\)")
RE_COUNTER = re.compile(r"\[(\d+)/(\d+)\]")
RE_FEATURES = re.compile(r"Features:\s*(\d+)")


def parse_progress(line: str, kind: str | None, n_images: int) -> float | None:
    """Turn one COLMAP log line into a 0..1 fraction for the current step, or None."""
    m = RE_BLOCK2.search(line)                 # exhaustive matcher: [a/n, b/n]
    if m:
        a, n, b, n2 = map(int, m.groups())
        return ((a - 1) * n2 + b) / (n * n2)
    if kind == "map":
        m = RE_REGISTER.search(line)           # mapper: number of registered images so far
        if m and n_images:
            return int(m.group(1)) / n_images
        return None
    m = RE_COUNTER.search(line)                # extractor / sequential / undistorter: [i/N]
    if m:
        i, n = map(int, m.groups())
        if n > 0:
            return i / n
    return None


class Progress:
    """Tracks the current COLMAP step and draws a one-line progress display."""

    def __init__(self, max_size: int, rates_file: Path | None = None, out=None,
                 clock: Callable[[], float] = time.time):
        self.clock = clock
        self.t0 = clock()
        self.rates_file = rates_file or default_rates_file()
        self.out = out or sys.stdout
        self.key = str(max_size)          # speeds depend on image size, so they're saved per size
        self.measured: dict[str, float] = {}
        self._totals: dict[str, list[float]] = {}
        self.saved = self._load()
        self.features: float | None = None
        self.tty = hasattr(self.out, "isatty") and self.out.isatty()
        self.plan_left: Callable[[], tuple[float, float]] = lambda: (0.0, 0.0)
        self.label, self.kind, self.units = "", None, 0
        self.frac, self.step_t0 = 0.0, 0.0
        self._feature_counts: list[int] = []
        self._last_draw = self._last_plain = 0.0

    # ---- speed bookkeeping
    def _load(self) -> dict:
        try:
            return json.loads(self.rates_file.read_text()).get(self.key, {})
        except (OSError, ValueError):
            return {}

    def _save(self, kind: str, rate: float) -> None:
        base = self.saved.get(kind)
        rate = rate if base is None else 0.5 * base + 0.5 * rate     # blend with earlier runs
        try:
            data = json.loads(self.rates_file.read_text()) if self.rates_file.exists() else {}
        except (OSError, ValueError):
            data = {}
        data.setdefault(self.key, {})[kind] = rate
        try:
            self.rates_file.parent.mkdir(parents=True, exist_ok=True)
            self.rates_file.write_text(json.dumps(data, indent=1))
        except OSError:
            pass

    def work(self, kind: str, units: float) -> float:
        if kind.startswith("match"):
            feats = self.features or DEFAULT_FEATURES
            return units * (feats / 1000) ** 2
        if kind == "map":
            return units ** MAP_EXPONENT
        return units

    def rate(self, kind: str) -> tuple[float, str]:
        """(seconds per unit of work, source) where source is measured / saved / guess."""
        if kind in self.measured:
            return self.measured[kind], "measured"
        if kind in self.saved:
            return self.saved[kind], "saved"
        if kind == "match_exhaustive":
            return self.rate("match")
        prior = PRIOR_RATES.get(kind, 0.0)
        if "extract" in self.measured:
            prior *= self.measured["extract"] / PRIOR_RATES["extract"]
        return prior, "guess"

    def estimate(self, kind: str, units: float) -> float:
        return self.rate(kind)[0] * self.work(kind, units)

    def confident(self) -> bool:
        return all(self.rate(k)[1] != "guess" for k in ("match", "map"))

    # ---- step lifecycle
    def start(self, label: str, kind: str | None, units: float) -> None:
        self.label, self.kind, self.units = label, kind, units
        self.frac, self.step_t0 = 0.0, self.clock()
        self._feature_counts = []
        self.draw(force=True)

    def line(self, text: str) -> None:
        if self.kind == "extract":
            m = RE_FEATURES.search(text)
            if m:
                self._feature_counts.append(int(m.group(1)))

    def update(self, frac: float | None) -> None:
        if frac is not None and frac > self.frac:
            self.frac = min(frac, 1.0)
        self.draw()

    def finish(self) -> None:
        elapsed = self.clock() - self.step_t0
        if self.kind == "extract" and self._feature_counts:
            self.features = sum(self._feature_counts) / len(self._feature_counts)
        if self.kind in PRIOR_RATES and self.units > 0:
            # Sum over every step of this kind so short steps (dominated by COLMAP start-up)
            # don't distort the measured speed.
            t = self._totals.setdefault(self.kind, [0.0, 0.0])
            t[0] += elapsed
            t[1] += self.work(self.kind, self.units)
            if t[0] > 2:
                self.measured[self.kind] = t[0] / t[1]
                self._save(self.kind, self.measured[self.kind])
        self.frac = 1.0
        self.draw(force=True)
        if self.tty:
            self.out.write("\n")
            self.out.flush()

    def step_left(self) -> float | None:
        elapsed = self.clock() - self.step_t0
        if self.frac >= 1.0:
            return 0.0
        expected = self.estimate(self.kind, self.units) if self.kind in PRIOR_RATES and self.units else None
        if self.frac >= 0.03 and elapsed > 3:
            if self.kind == "map":                       # each new image is slower to place
                live = elapsed * (self.frac ** -MAP_EXPONENT - 1)
            else:
                live = elapsed * (1 - self.frac) / self.frac
            if expected is None or self.frac > 0.25:
                return live
            w = self.frac / 0.25                         # early on, blend with the expectation
            return w * live + (1 - w) * max(expected - elapsed, 0)
        if expected is not None:
            return max(expected - elapsed, 0)
        return None

    def render(self) -> str:
        now = self.clock()
        step_left = self.step_left()
        ok_left, worst_left = self.plan_left()
        if step_left is not None:
            ok_left += step_left
            worst_left += step_left
        approx = "" if self.confident() else "~"
        worst = (f" (worst case {approx}{fmt_time(worst_left)})"
                 if worst_left - ok_left > max(10, 0.2 * ok_left) else "")
        # This exact format is parsed by the web UI (server/jobs.py).
        return (f"    {self.label:<26} {bar(self.frac)} {self.frac:4.0%}  "
                f"step left {fmt_time(step_left)} | total {fmt_time(now - self.t0)} | "
                f"left {approx}{fmt_time(ok_left)}{worst}")

    def draw(self, force: bool = False) -> None:
        now = self.clock()
        if not force and now - self._last_draw < 0.5:
            return
        self._last_draw = now
        line = self.render()
        if self.tty:
            self.out.write("\r\033[K" + line)
            self.out.flush()
        elif force or now - self._last_plain > 30:      # logs / pipes: one line every 30 s
            self._last_plain = now
            print(line, file=self.out, flush=True)
