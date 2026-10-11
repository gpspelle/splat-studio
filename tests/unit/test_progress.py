import io
import json
import unittest

from tests.helpers import TempDirTestCase
from splat_studio.progress import (DEFAULT_FEATURES, MAP_EXPONENT, PRIOR_RATES, Progress, bar,
                                   parse_progress)


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class ParseProgressTest(unittest.TestCase):
    def test_counter_lines(self):
        self.assertAlmostEqual(parse_progress("Processed file [50/250]", "extract", 250), 0.2)
        self.assertAlmostEqual(parse_progress("Processing image [125/250]", "match", 250), 0.5)
        self.assertAlmostEqual(parse_progress("Undistorting image [10/10]", "undistort", 10), 1.0)

    def test_exhaustive_blocks(self):
        # block [3/5, 1/5] is the 11th of 25
        self.assertAlmostEqual(parse_progress("Processing block [3/5, 1/5]", "match_exhaustive", 0), 11 / 25)
        self.assertAlmostEqual(parse_progress("Matching block [5/5, 5/5]", "match_exhaustive", 0), 1.0)

    def test_mapper_counts_registered_images(self):
        self.assertAlmostEqual(parse_progress("Registering image #42 (25)", "map", 100), 0.25)
        self.assertAlmostEqual(parse_progress("Registering image #9 (num_reg_frames=50)", "map", 100), 0.5)
        self.assertIsNone(parse_progress("Processed file [1/2]", "map", 100))   # counters ignored for map

    def test_unrelated_lines(self):
        self.assertIsNone(parse_progress("Bundle adjustment report", "match", 10))

    def test_bar(self):
        self.assertEqual(bar(0, 4), "░░░░")
        self.assertEqual(bar(0.5, 4), "██░░")
        self.assertEqual(bar(2, 4), "████")


class ProgressEstimateTest(TempDirTestCase):
    def make(self, **kw):
        return Progress(1600, rates_file=self.tmp / "rates.json", out=io.StringIO(), **kw)

    def test_work_units(self):
        p = self.make()
        self.assertEqual(p.work("extract", 10), 10)
        self.assertAlmostEqual(p.work("map", 100), 100 ** MAP_EXPONENT)
        self.assertAlmostEqual(p.work("match", 10), 10 * (DEFAULT_FEATURES / 1000) ** 2)
        p.features = 2000
        self.assertAlmostEqual(p.work("match_exhaustive", 10), 10 * 4)

    def test_guesses_scale_with_measured_extraction(self):
        p = self.make()
        self.assertEqual(p.rate("map"), (PRIOR_RATES["map"], "guess"))
        p.measured["extract"] = PRIOR_RATES["extract"] / 2         # this machine is 2x faster
        self.assertAlmostEqual(p.rate("map")[0], PRIOR_RATES["map"] / 2)
        self.assertFalse(p.confident())

    def test_exhaustive_falls_back_to_sequential_speed(self):
        p = self.make()
        p.measured["match"] = 0.123
        self.assertEqual(p.rate("match_exhaustive"), (0.123, "measured"))

    def test_measures_and_saves_speeds(self):
        clock = FakeClock()
        p = self.make(clock=clock)
        p.start("matching", "match", 1000)
        p.features = 1000
        clock.t += 10
        p.finish()
        self.assertAlmostEqual(p.measured["match"], 10 / 1000)
        saved = json.loads((self.tmp / "rates.json").read_text())
        self.assertAlmostEqual(saved["1600"]["match"], 0.01)
        # the next run starts from the saved speed and blends new measurements 50/50
        p2 = self.make(clock=clock)
        self.assertEqual(p2.rate("match"), (0.01, "saved"))

    def test_short_steps_dont_set_speed_alone(self):
        clock = FakeClock()
        p = self.make(clock=clock)
        p.start("loop", "match", 100)
        clock.t += 0.5
        p.finish()
        self.assertNotIn("match", p.measured)                     # under 2 s total: too noisy

    def test_feature_count_is_read_from_log(self):
        p = self.make()
        p.start("extracting", "extract", 2)
        p.line("Features:        3000")
        p.line("Features:        5000")
        p.finish()
        self.assertEqual(p.features, 4000)

    def test_step_left_uses_live_speed(self):
        clock = FakeClock()
        p = self.make(clock=clock)
        p.start("matching", "match", 100)
        clock.t += 30
        p.update(0.5)
        self.assertAlmostEqual(p.step_left(), 30)                 # halfway after 30 s -> 30 s left
        p.update(1.0)
        self.assertEqual(p.step_left(), 0.0)

    def test_mapper_estimate_is_superlinear(self):
        clock = FakeClock()
        p = self.make(clock=clock)
        p.start("reconstructing", "map", 100)
        clock.t += 10
        p.update(0.5)
        self.assertGreater(p.step_left(), 10)                     # later images are slower to place

    def test_render_format_is_parseable_by_ui(self):
        from splat_studio.server.jobs import RE_COLMAP_BAR
        p = self.make()
        p.plan_left = lambda: (100.0, 900.0)
        p.start("extracting features", "extract", 250)
        m = RE_COLMAP_BAR.search(p.render())
        self.assertIsNotNone(m, p.render())
        self.assertEqual(m.group(1), "extracting features")
        self.assertIsNotNone(m.group(5))                           # worst case shown


if __name__ == "__main__":
    unittest.main()
