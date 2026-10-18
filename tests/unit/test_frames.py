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
