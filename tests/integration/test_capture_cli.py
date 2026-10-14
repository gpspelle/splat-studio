"""`splat-studio run CAPTURE.zip` and `splat-studio trial CAPTURE.zip` with an iPhone capture (fake tools)."""
import json

from splat_studio import capture
from splat_studio.objects import read_images_bin
from splat_studio.pipeline import frames_of_run
from tests.helpers import TempDirTestCase, fake_tools_env, make_capture, needs_ffmpeg, run_module


@needs_ffmpeg
class RunCaptureTest(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.zip = make_capture(self.tmp / "Downloads", name="capture_2026-10-14_153012", frames=90, limited=6)

    def run_capture(self, *extra, env=None):
        r = run_module("pipeline", [self.zip, "--frames", "30", "--quality", "preview", *extra], cwd=self.tmp,
                       env=env, timeout=240)
        self.assertEqual(r.returncode, 0, r.stdout[-3000:] + r.stderr[-3000:])
        return r, (self.tmp / "workspace" / "capture_2026-10-14_153012" / "latest").resolve()

    def test_arkit_poses_train_without_colmap(self):
        r, ws = self.run_capture()
        self.assertIn("==> 2/3 Camera poses (ARKit): ", r.stdout)
        self.assertNotIn("Attempt 1", r.stdout)                         # COLMAP never ran
        self.assertFalse((ws / "colmap.log").exists())
        self.assertIn("30 cameras placed from ARKit", r.stdout)
        self.assertEqual(len(read_images_bin(ws / "dense" / "sparse" / "0" / "images.bin")), 30)
        self.assertTrue((ws / "coverage.json").exists())                # coverage works on ARKit poses
        self.assertIn("Done. Splat: ", r.stdout)
        self.assertTrue(list((ws / "splat").glob("*.ply")))
        summary = (ws / "summary.txt").read_text()
        self.assertIn("iPhone15,2", summary)
        self.assertIn("6 frames where tracking was limited", summary)
        self.assertRegex(summary, r"poses:\s+ARKit: 30 frames")
        self.assertEqual(frames_of_run(ws).resolve(), (self.tmp / "frames" / "capture_2026-10-14_153012").resolve())
        self.assertTrue((self.tmp / "captures" / "capture_2026-10-14_153012").is_dir())
        # a second run reuses the frames
        r2, _ = self.run_capture()
        self.assertIn("reusing 30 frames", r2.stdout)

    def test_refine_triangulates_with_colmap_ids(self):
        r, ws = self.run_capture("--poses", "refine")
        self.assertIn("Camera poses (ARKit, refined with COLMAP)", r.stdout)
        self.assertIn("points triangulated", r.stdout)
        self.assertEqual(capture.model_counts(ws / "dense" / "sparse" / "0"), (30, 900))  # the fake's points
        self.assertRegex((ws / "summary.txt").read_text(), r"poses:\s+ARKit, refined with COLMAP: 30 frames, 900 points")

    def test_colmap_ignores_the_phone_poses(self):
        r, ws = self.run_capture("--poses", "colmap")
        self.assertIn("==> 2/3 Camera poses (COLMAP): ", r.stdout)
        self.assertIn("Attempt 1", r.stdout)
        self.assertIn("COLMAP (ARKit's ignored)", (ws / "summary.txt").read_text())
        self.assertIn("Done. Splat: ", r.stdout)

    def test_name_and_phone_check_reach_the_summary(self):
        self.zip = make_capture(self.tmp / "Downloads", name="vase-window_2026-10-14_160000", frames=60,
                                meta={"name": "vase window", "cameraLocked": True,
                                      "quality": {"unsteady": 0.05, "blurry": 0.1, "gap": 20.0, "heights": 31.0}})
        r = run_module("pipeline", [self.zip, "--frames", "20", "--quality", "preview"], cwd=self.tmp, timeout=240)
        self.assertEqual(r.returncode, 0, r.stdout[-3000:] + r.stderr[-3000:])
        self.assertIn('"vase window": 20 frames', r.stdout)
        summary = (self.tmp / "workspace" / "vase-window_2026-10-14_160000" / "latest" / "summary.txt").read_text()
        self.assertIn('"vase window"', summary)
        self.assertIn("focus and exposure locked", summary)
        self.assertRegex(summary, r"phone check:\s+340 degrees around, heights over 31 degrees, 5% unsteady, 10% blurry")

    def test_dry_run_reads_nothing(self):
        r = run_module("pipeline", [self.zip, "--dry-run"], cwd=self.tmp, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("would read the capture", r.stdout)
        self.assertFalse((self.tmp / "captures").exists())


@needs_ffmpeg
class TrialTest(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.zip = make_capture(self.tmp, name="capture_vase", frames=60, limited=3)

    def trial(self, *extra, env=None):
        return run_module("__main__", ["trial", self.zip, "--frames", "20", "--quality", "preview", "--evals", "3",
                                       *extra], cwd=self.tmp, env=env, timeout=300)

    def test_three_ways_compared_on_the_same_frames(self):
        r = self.trial()
        self.assertEqual(r.returncode, 0, r.stdout[-3000:] + r.stderr[-3000:])
        (out,) = (self.tmp / "workspace" / "capture_vase").glob("trial_*")
        res = json.loads((out / "results.json").read_text())
        self.assertEqual([v["key"] for v in res["variants"]], ["arkit", "refine", "colmap"])
        for v in res["variants"]:
            self.assertNotIn("error", v)
            self.assertEqual(v["images"], 20)
            self.assertIsNotNone(v["psnr"])
            self.assertGreaterEqual(len(v["curve"]), 3)
            self.assertTrue(list((out / v["key"] / "splat").glob("*.ply")))
        report = (out / "report.md").read_text()
        for label in ("| ARKit |", "| ARKit + points |", "| COLMAP |"):
            self.assertIn(label, report)
        self.assertIn("train as well as COLMAP's", report)           # the fake scores them the same
        self.assertIn("+0.00 dB", report)
        # trials aren't runs: the library's latest run is untouched
        self.assertFalse((self.tmp / "workspace" / "capture_vase" / "latest").exists())

    def test_a_failed_variant_is_reported_and_the_rest_still_compared(self):
        r = self.trial(env=fake_tools_env(FAKE_COLMAP_FAIL="point_triangulator"))
        self.assertEqual(r.returncode, 0, r.stdout[-3000:] + r.stderr[-3000:])
        (out,) = (self.tmp / "workspace" / "capture_vase").glob("trial_*")
        res = {v["key"]: v for v in json.loads((out / "results.json").read_text())["variants"]}
        self.assertIn("point_triangulator", res["refine"]["error"])
        self.assertIsNotNone(res["arkit"]["psnr"])
        self.assertIsNotNone(res["colmap"]["psnr"])
        self.assertIn("| ARKit + points | failed", (out / "report.md").read_text())

    def test_only_some_variants(self):
        r = self.trial("--only", "arkit")
        self.assertEqual(r.returncode, 0, r.stdout[-3000:] + r.stderr[-3000:])
        (out,) = (self.tmp / "workspace" / "capture_vase").glob("trial_*")
        self.assertEqual([v["key"] for v in json.loads((out / "results.json").read_text())["variants"]], ["arkit"])
        self.assertIn("Not enough results to compare", (out / "report.md").read_text())

    def test_not_a_capture(self):
        (self.tmp / "clip.mov").write_bytes(b"not a video")
        r = run_module("__main__", ["trial", self.tmp / "clip.mov"], cwd=self.tmp, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not a Splat Capture package", r.stdout + r.stderr)
