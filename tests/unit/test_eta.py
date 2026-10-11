"""Time-left estimates against simulated runs whose true durations are known.

The simulations reproduce what makes estimates go wrong on real runs: start-up time before the
first progress line, COLMAP's mapper slowing down as it adds images, a long final bundle
adjustment at 100%, and Brush getting slower while the splat grows."""
import random
import unittest

from tests.helpers import TempDirTestCase
from splat_studio import eta


def extract_events(n=250, startup=6.0, per=0.4, seed=1):
    """(t, frac) of feature extraction."""
    rnd, t, out = random.Random(seed), startup, []
    for i in range(1, n + 1):
        t += per * rnd.uniform(0.7, 1.3)
        out.append((t, i / n))
    return out, t


def mapper_events(n=250, base=0.15, seed=2, final_ba=45.0):
    """Registering image k costs ~ base * sqrt(k) (+ a bundle adjustment every 30 images)."""
    rnd, t, out = random.Random(seed), 2.0, []
    for k in range(1, n + 1):
        t += base * k ** 0.5 * rnd.uniform(0.8, 1.2)
        if k % 30 == 0:
            t += 0.08 * k                             # periodic global bundle adjustment
        out.append((t, k / n))
    return out, t + final_ba


def brush_events(total=30000, growth_stop=15000, startup=40.0, n0=60_000, n_max=1_500_000,
                 overhead=0.012, per_million=0.030, refine_every=200, seed=3):
    """(t, step, splats) of Brush training: step time = overhead + per-splat cost."""
    rnd, t, out, n = random.Random(seed), startup, [], n0
    for s in range(1, total + 1):
        if s % refine_every == 0 and s <= growth_stop:
            n = n + (n_max - n) * 0.035               # grows quickly, then levels off
        t += (overhead + per_million * n / 1e6) * rnd.uniform(0.95, 1.05)
        if s % 50 == 0:
            out.append((t, s, int(n)))
    return out, t


class WindowTest(unittest.TestCase):
    def test_says_estimating_before_it_can_measure(self):
        w = eta.Window(0.0)
        self.assertIsNone(w.left(1.0))
        w.add(2.0, 0.001)
        self.assertIsNone(w.left(3.0))

    def test_constant_speed_step(self):
        ev, end = extract_events()
        w = eta.Window(0.0)
        for t, f in ev:
            w.add(t, f)
            if f in (0.2, 0.5, 0.8):
                truth = end - t
                self.assertAlmostEqual(w.left(t), truth, delta=0.15 * truth + 3, msg=f)

    def test_startup_time_does_not_skew_the_estimate(self):
        # 60 s of loading before the first frame: an average since the start would say ~2x too long
        ev, end = extract_events(startup=60.0)
        w = eta.Window(0.0)
        for t, f in ev:
            w.add(t, f)
        t, f = ev[len(ev) // 2]
        w2 = eta.Window(0.0)
        for tt, ff in ev[: len(ev) // 2 + 1]:
            w2.add(tt, ff)
        self.assertAlmostEqual(w2.left(t), end - t, delta=0.15 * (end - t))


class RunClockTest(TempDirTestCase):
    def clock(self, **kw):
        return eta.RunClock(history=eta.History(self.tmp / "timings.json"), **kw)

    def test_mapper_slowing_down_and_final_adjustment(self):
        ev, end = mapper_events()
        c = self.clock()
        c.start_phase("reconstructing poses", 0.0)
        errors, finishing = [], False
        for t, f in ev:
            c.progress(t, f)
            if 0.25 <= f < 0.9 and int(f * 250) % 25 == 0:
                left = c.step_left(t)[0]
                last_reg = ev[-1][0]
                errors.append(abs(left - (last_reg - t)) / (last_reg - t))
        self.assertLess(max(errors), 0.35, errors)
        # stuck at 100% during the final bundle adjustment: "finishing", not "0s left"
        left, note = c.step_left(ev[-1][0] + eta.STALL + 5)
        self.assertEqual((left, note), (None, "finishing"))

    def test_training_that_slows_down_as_the_splat_grows(self):
        ev, end = brush_events()
        c = self.clock(kind="retrain", clean_after=False)
        c.line(0.0, "$ brush dense --total-train-iters 30000 --growth-stop-iter 15000 --max-splats 2000000")
        c.start_training(0.0)
        ours, linear = [], []
        for t, s, n in ev:
            c.line(t, f"Current splat count {n}")
            c.brush_step(t, s, 30000)
            if s in (3000, 6000, 9000, 12000, 20000, 27000):
                truth = end - t
                left = c.step_left(t)[0]
                ours.append(abs(left - truth) / truth)
                linear.append(abs(c.train.steps.left(t, 30000) - truth) / truth)
        self.assertLess(max(ours), 0.3, ours)
        self.assertGreater(max(linear), 0.4, linear)      # what Brush's own estimate does

    def test_no_made_up_total_on_a_new_computer(self):
        c = self.clock()
        c.start_phase("extracting features", 0.0)
        for t, f in extract_events()[0][:100]:
            c.progress(t, f)
        e = c.estimate(45.0)
        self.assertIsNotNone(e["step_left"])
        self.assertIsNone(e["total_left"])
        self.assertEqual(e["note"], "first_run")

    def test_total_from_this_computers_history(self):
        h = eta.History(self.tmp / "timings.json")
        # a finished run teaches per-frame and per-step speeds
        c = eta.RunClock(history=h, frames_hint=200)
        c.line(0, "$ brush x --total-train-iters 24000 --growth-stop-iter 12000")
        t = 0.0
        for phase, dur in (("frames", 40), ("extracting features", 80), ("matching (sequential)", 120),
                           ("reconstructing poses", 200), ("undistorting images", 30)):
            c.start_phase(phase, t)
            c.progress(t + dur, 1.0)
            t += dur
        c.start_training(t)
        c.brush_step(t + 30, 1, 24000)
        c.brush_step(t + 30 + 1200, 24000, 24000)
        t += 1230
        c.start_phase("clean", t)
        c.end_phase(t + 50)
        # the next run, same size (200 frames: the pipeline picks 24000 steps): total known from the start
        c2 = eta.RunClock(history=eta.History(self.tmp / "timings.json"), frames_hint=200)
        c2.start_phase("frames", 0.0)
        e = c2.estimate(0.0)
        self.assertAlmostEqual(e["total_left"], 40 + 80 + 120 + 200 + 30 + 1230 + 50, delta=5)
        self.assertEqual(e["note"], "more_attempts")
        # during training, only training and cleanup remain
        c2.line(0, "$ brush x --total-train-iters 24000")
        c2.start_training(1000.0)
        e = c2.estimate(1000.0)
        self.assertAlmostEqual(e["total_left"], 1230 + 50, delta=5)


class FormatTest(unittest.TestCase):
    def test_rounding(self):
        self.assertEqual([eta.fmt_left(x) for x in (None, 3, 42, 95, 1830, 4800)],
                         [None, "5s", "45s", "2 min", "31 min", "1 h 20 min"])


if __name__ == "__main__":
    unittest.main()
