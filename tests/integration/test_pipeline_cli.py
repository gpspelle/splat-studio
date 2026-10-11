"""The whole command-line pipeline: video -> frames -> COLMAP (fake) -> Brush (fake) -> .ply"""
from tests.helpers import TempDirTestCase, fake_tools_env, make_video, needs_ffmpeg, run_module


@needs_ffmpeg
class PipelineTest(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.video = make_video(self.tmp / "vase.mov", seconds=6)

    def run_pipeline(self, *args, env=None):
        return run_module("pipeline", list(args), cwd=self.tmp, env=env, timeout=180)

    def test_video_to_splat(self):
        r = self.run_pipeline(self.video, "--frames", "40", "--quality", "preview")
        self.assertEqual(r.returncode, 0, r.stdout[-3000:] + r.stderr[-3000:])
        for marker in ("==> 1/3 Frames", "==> 2/3 Camera poses (COLMAP)", "==> 3/3 Training", "Done. Splat:"):
            self.assertIn(marker, r.stdout)
        run = (self.tmp / "workspace" / "vase" / "latest").resolve()
        splats = list((run / "splat").glob("preview_*.ply"))
        self.assertEqual(len(splats), 1)
        self.assertTrue(splats[0].read_bytes().startswith(b"ply\n"))
        summary = (run / "summary.txt").read_text()
        self.assertIn("brush command:", summary)
        self.assertIn("--total-train-iters", summary)
        self.assertIn("splat:", summary)

    def test_frames_are_reused_and_retrain_skips_colmap(self):
        self.assertEqual(self.run_pipeline(self.video, "--frames", "30", "--quality", "preview").returncode, 0)
        r = self.run_pipeline(self.video, "--quality", "preview")
        self.assertIn("reusing", r.stdout)
        runs = sorted((self.tmp / "workspace" / "vase").glob("run_*"))
        self.assertEqual(len(runs), 2)                                   # each run has its own folder
        r = self.run_pipeline("--train-only", runs[0], "--quality", "high", "--eval")
        self.assertEqual(r.returncode, 0, r.stdout[-2000:])
        self.assertNotIn("2/3 Camera poses", r.stdout)
        self.assertIn("--eval-split-every", r.stdout)
        self.assertTrue(list((runs[0] / "splat").glob("high_*.ply")))
        self.assertTrue(list((runs[0] / "splat").glob("preview_*.ply")))   # earlier splat kept

    def test_extra_brush_options_are_checked_by_brush(self):
        ok = self.run_pipeline(self.video, "--frames", "30", "--quality", "preview", "--", "--sh-degree", "1")
        self.assertEqual(ok.returncode, 0, ok.stdout[-2000:])
        bad = self.run_pipeline(self.video, "--quality", "preview", "--", "--total-steps", "5")
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("unexpected argument '--total-steps'", bad.stdout + bad.stderr)

    def test_failed_reconstruction_never_starts_brush(self):
        env = fake_tools_env(FAKE_COLMAP_REGISTER="0.3")
        r = self.run_pipeline(self.video, "--frames", "30", env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Brush was not started", r.stderr)
        self.assertNotIn("3/3 Training", r.stdout)

    def test_dry_run_and_no_train(self):
        dry = self.run_pipeline(self.video, "--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertFalse((self.tmp / "workspace").exists())
        r = self.run_pipeline(self.video, "--frames", "30", "--no-train")
        self.assertIn("--no-train", r.stdout)
        self.assertFalse(list((self.tmp / "workspace").glob("*/run_*/splat/*.ply")))
