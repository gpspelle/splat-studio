"""Coverage: where the cameras were around the object, checked before training."""
import unittest
from pathlib import Path
import tempfile

from tests.helpers import FAKES  # noqa: F401  (puts tests/fakes on the path)
from plyfile import write_images_bin
from splat_studio import coverage, guidance, objects


def ring(n=36, keep=lambda k: True):
    with tempfile.TemporaryDirectory() as td:
        ims = objects.read_images_bin(write_images_bin(Path(td) / "images.bin", n=n, radius=3.0, target=(0, -0.25, 0)))
    return [im for k, im in enumerate(ims) if keep(k)]


class MeasureTest(unittest.TestCase):
    def test_all_the_way_round_at_three_heights(self):
        c = coverage.measure(ring())
        self.assertEqual(c["cameras"], 36)
        self.assertLess(c["gap"]["degrees"], 15)
        self.assertGreater(c["height"]["spread"], coverage.HEIGHT_WARN)
        self.assertEqual(coverage.problems(c), [])

    def test_one_side_missing(self):
        c = coverage.measure(ring(keep=lambda k: not 10 <= k < 22))       # 12 of 36 cameras gone: 130 degrees
        self.assertAlmostEqual(c["gap"]["degrees"], 130, delta=2)
        self.assertEqual(coverage.problems(c), ["gap"])
        self.assertEqual({c["gap"]["first"], c["gap"]["last"]}, {"frame_0010.jpg", "frame_0023.jpg"})

    def test_one_height(self):
        c = coverage.measure(ring(keep=lambda k: k % 3 == 1))
        self.assertEqual(coverage.problems(c), ["height"])

    def test_too_few_cameras(self):
        self.assertIsNone(coverage.measure(ring()[:2]))
        self.assertEqual(coverage.problems(None), [])


class AdviceTest(unittest.TestCase):
    def test_gap_names_the_moments_in_the_video(self):
        c = coverage.measure(ring(keep=lambda k: not 10 <= k < 22))
        tips = guidance.coverage_advice(c, fps=2.0)
        self.assertEqual([t["code"] for t in tips], ["run.coverage_gap_at"])
        self.assertEqual(tips[0]["params"]["when"], "0:04 and 0:11")      # frames 10 and 23 at 2 fps
        self.assertEqual(tips[0]["params"]["deg"], "130")
        self.assertEqual(guidance.coverage_advice(c)[0]["code"], "run.coverage_gap")   # no timing known

    def test_good_coverage_and_run_advice(self):
        self.assertEqual([t["code"] for t in guidance.coverage_advice(coverage.measure(ring()))], ["run.coverage_ok"])
        flat = guidance.coverage_advice(coverage.measure(ring(keep=lambda k: k % 3 == 1)))
        job = {"status": "done", "coverage_advice": flat + [{"level": "ok", "code": "x", "params": {}, "text": ""}]}
        self.assertEqual([t["code"] for t in guidance.run_advice(job)], ["run.coverage_height"])   # warnings only


if __name__ == "__main__":
    unittest.main()
