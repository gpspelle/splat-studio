import unittest

import tests  # noqa: F401
from splat_studio import frames
from splat_studio.__main__ import main as cli_main


class FramesLogicTest(unittest.TestCase):
    def test_choose_fps(self):
        self.assertEqual(frames.choose_fps(250, 100), 2.5)
        self.assertEqual(frames.choose_fps(250, 100, fixed=3), 3)
        self.assertEqual(frames.choose_fps(250, 3), 83.333)
        with self.assertRaises(ValueError):
            frames.choose_fps(250, 0)

    def test_auto_frame_count_follows_the_length(self):
        self.assertEqual(frames.auto_target(90), 250)            # short videos: the old fixed count
        self.assertEqual(frames.auto_target(143), 250)
        self.assertEqual(frames.auto_target(240), 420)           # a 4-minute walkaround
        self.assertEqual(frames.auto_target(15 * 60), 600)       # capped
        self.assertEqual(frames.parse_target("auto"), "auto")
        self.assertEqual(frames.parse_target(" AUTO "), "auto")
        self.assertEqual(frames.parse_target("400"), 400)
        for bad in ("0", "-3", "many"):
            with self.assertRaises(ValueError):
                frames.parse_target(bad)

    def test_pick_sharpest_swaps_a_blurry_frame_for_its_neighbour(self):
        scores = {0: [(10.0, "a0"), (11.0, "a1"), (9.0, "a2")],
                  1: [(1.0, "b0"), (12.0, "b1"), (2.0, "b2")],      # own frame blurry, a neighbour sharp
                  2: [(1.0, "c0"), (1.5, "c1"), (2.0, "c2")],       # all blurry
                  3: [(10.0, "d0"), (10.5, "d1"), (9.5, "d2")]}
        picks = frames.pick_sharpest(scores, 0.5)
        self.assertEqual([picks[s]["item"] for s in range(4)], ["a1", "b1", "c2", "d1"])
        self.assertEqual([picks[s]["replaced"] for s in range(4)], [False, True, False, False])
        self.assertEqual([picks[s]["blurry"] for s in range(4)], [False, False, True, False])
        self.assertFalse(any(p["blurry"] for p in frames.pick_sharpest(scores, 0).values()))   # ratio 0: keep all

    def test_blurry_indices(self):
        scores = [100, 110, 20, 95, 30, 105]
        self.assertEqual(frames.blurry_indices(scores, 0.5), [2, 4])
        self.assertEqual(frames.blurry_indices(scores, 0), [])
        self.assertEqual(frames.blurry_indices([], 0.5), [])

    def test_frame_names_sort_in_time_order(self):
        name = frames.frame_name("IMG_1")
        self.assertEqual(name % 7, "IMG_1_0007.jpg")
        self.assertLess(name % 9, name % 10)


class CliTest(unittest.TestCase):
    def test_version_and_unknown_command(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli_main(["--version"])
        self.assertRegex(out.getvalue().strip(), r"^\d+\.\d+\.\d+$")
        with self.assertRaises(SystemExit):
            cli_main(["nonsense"])


if __name__ == "__main__":
    unittest.main()
