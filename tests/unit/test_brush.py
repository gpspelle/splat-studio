import unittest
from pathlib import Path

from tests.helpers import FAKES, TempDirTestCase
from splat_studio import brush
from splat_studio.pipeline import brush_command, split_brush_args

REAL_FLAGS = {l.strip() for l in (FAKES / "brush_flags.txt").read_text().splitlines()
              if l.strip() and not l.startswith("#")}


class BrushParamsTest(unittest.TestCase):
    def test_steps_scale_with_images_and_clamp(self):
        self.assertEqual(brush.choose_brush_params(250, None)[0]["--total-train-iters"], 30000)
        self.assertEqual(brush.choose_brush_params(100, None)[0]["--total-train-iters"], 12000)
        self.assertEqual(brush.choose_brush_params(20, None)[0]["--total-train-iters"], 10000)     # minimum
        self.assertEqual(brush.choose_brush_params(900, None, "high")[0]["--total-train-iters"], 50000)
        self.assertEqual(brush.choose_brush_params(250, None, "preview")[0]["--total-train-iters"], 8000)

    def test_growth_stops_halfway_and_refine_tracks_images(self):
        p, _ = brush.choose_brush_params(250, None)
        self.assertEqual(p["--growth-stop-iter"], 15000)
        self.assertEqual(p["--refine-every"], 250)
        self.assertEqual(brush.choose_brush_params(40, None)[0]["--refine-every"], 100)
        self.assertEqual(brush.choose_brush_params(800, None)[0]["--refine-every"], 300)

    def test_auto_detail_from_points_per_image(self):
        self.assertEqual(brush.auto_detail(100, 100 * 700), "high")
        self.assertEqual(brush.auto_detail(100, 100 * 100), "normal")
        self.assertEqual(brush.auto_detail(100, 100 * 300), "normal")
        self.assertEqual(brush.auto_detail(100, None), "normal")
        p, why = brush.choose_brush_params(100, 100 * 700)
        self.assertEqual(p["--growth-grad-threshold"], brush.DETAIL["high"])
        self.assertIn("high detail", why)

    def test_resolution_never_exceeds_images(self):
        self.assertEqual(brush.choose_brush_params(250, None, "high", image_size=1600)[0]["--max-resolution"], 1600)
        self.assertEqual(brush.choose_brush_params(250, None, "preview", image_size=1600)[0]["--max-resolution"], 1024)

    def test_rejects_unknown_settings(self):
        with self.assertRaises(ValueError):
            brush.choose_brush_params(250, None, "ultra")
        with self.assertRaises(ValueError):
            brush.choose_brush_params(250, None, "standard", "extreme")

    def test_every_option_exists_in_brush(self):
        cmd = brush_command("brush", Path("dense"), brush.choose_brush_params(250, 9000)[0], "high", True, True, [])
        flags = {c for c in map(str, cmd) if c.startswith("--")}
        self.assertTrue(flags <= REAL_FLAGS, flags - REAL_FLAGS)

    def test_brush_command_layout(self):
        cmd = brush_command("brush", Path("d"), {"--sh-degree": 3}, "preview", False, False, ["--sh-degree", "1"])
        self.assertEqual(cmd[:2], ["brush", Path("d")])
        self.assertEqual(cmd[cmd.index("--export-name") + 1], "preview_{iter}.ply")
        self.assertEqual(cmd[-2:], ["--sh-degree", "1"])                    # user overrides come last
        self.assertNotIn("--eval-split-every", cmd)

    def test_split_brush_args(self):
        self.assertEqual(split_brush_args(["a.mov", "--", "--sh-degree", "1"]), (["a.mov"], ["--sh-degree", "1"]))
        self.assertEqual(split_brush_args(["a.mov"]), (["a.mov"], []))

    def test_parse_model_stats(self):
        self.assertEqual(brush.parse_model_stats("Registered images: 240\nPoints: 61000\n"), (240, 61000))
        self.assertEqual(brush.parse_model_stats("nothing"), (None, None))


class FindBrushTest(TempDirTestCase):
    def test_env_override_and_built_checkout(self):
        fake = self.tmp / "my-brush"
        fake.write_text("#!/bin/sh\n")
        from tests.helpers import mock_env
        with mock_env({"SPLAT_STUDIO_BRUSH": str(fake), "PATH": "/usr/bin:/bin"}):
            self.assertEqual(brush.find_brush(), fake)
        with mock_env({"SPLAT_STUDIO_BRUSH": None, "PATH": "/usr/bin:/bin"}):
            self.assertIsNone(brush.find_brush())
            built = self.tmp / "home" / "3d" / "brush" / "target" / "release" / "brush"
            built.parent.mkdir(parents=True)
            built.write_text("")
            self.assertEqual(brush.find_brush(), built.resolve())

    def test_is_brush_repo(self):
        repo = self.tmp / "brush"
        self.assertFalse(brush.is_brush_repo(repo))
        (repo / "apps" / "brush-app").mkdir(parents=True)
        (repo / "Cargo.toml").write_text("")
        self.assertTrue(brush.is_brush_repo(repo))


if __name__ == "__main__":
    unittest.main()
