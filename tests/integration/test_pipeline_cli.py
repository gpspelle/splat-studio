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
        splats = [p for p in (run / "splat").glob("preview_*.ply") if not p.stem.endswith("_clean")]
        self.assertEqual(len(splats), 1)
        self.assertTrue(splats[0].read_bytes().startswith(b"ply\n"))
        self.assertNotIn("Auto:", r.stdout)                              # --frames 40 was given
        summary = (run / "summary.txt").read_text()
        self.assertIn("brush command:", summary)
        self.assertIn("--total-train-iters", summary)
        self.assertIn("splat:", summary)

    def test_frame_count_is_auto_by_default(self):
        r = self.run_pipeline(self.video, "--quality", "preview", "--no-train")
        self.assertEqual(r.returncode, 0, r.stdout[-3000:] + r.stderr[-3000:])
        self.assertIn("Auto: 250 frames for 0:06 of video", r.stdout)

    def test_global_mapper_first_incremental_as_fallback(self):
        r = self.run_pipeline(self.video, "--frames", "30", "--quality", "preview", "--no-train")
        self.assertEqual(r.returncode, 0, r.stdout[-3000:] + r.stderr[-3000:])
        self.assertIn("with COLMAP's global mapper", r.stdout)
        run = (self.tmp / "workspace" / "vase" / "latest").resolve()
        log, summary = (run / "colmap.log").read_text(), (run / "summary.txt").read_text()
        self.assertIn("$ colmap global_mapper", log)
        self.assertNotIn("$ colmap mapper", log)
        self.assertRegex(summary, r"mapper:\s+global")
        self.assertRegex(summary, r"attempt 1:\s+sequential matching, global mapper: ")
        # the global mapper loses the track: the relaxed fallback uses the incremental mapper
        env = fake_tools_env(FAKE_COLMAP_GLOBAL_REGISTER="0.5")
        r = self.run_pipeline(self.video, "--quality", "preview", "--no-train", env=env)
        self.assertEqual(r.returncode, 0, r.stdout[-3000:] + r.stderr[-3000:])
        run = (self.tmp / "workspace" / "vase" / "latest").resolve()
        summary = (run / "summary.txt").read_text()
        self.assertRegex(summary, r"attempt 1:\s+sequential matching, global mapper: .* \(50%\)")
        self.assertRegex(summary, r"attempt 2:\s+wider overlap \+ loop closure, relaxed mapper: .* \(100%\)")
        self.assertIn("$ colmap mapper", (run / "colmap.log").read_text())

    def test_incremental_mapper_when_asked_or_on_older_colmap(self):
        for args, env in [(["--mapper", "incremental"], None), ([], fake_tools_env(FAKE_COLMAP_GLOBAL="0"))]:
            r = self.run_pipeline(self.video, "--frames", "30", "--quality", "preview", "--no-train", *args, env=env)
            self.assertEqual(r.returncode, 0, r.stdout[-3000:] + r.stderr[-3000:])
            run = (self.tmp / "workspace" / "vase" / "latest").resolve()
            self.assertNotIn("global_mapper", (run / "colmap.log").read_text())
            self.assertRegex((run / "summary.txt").read_text(), r"mapper:\s+incremental")

    def test_every_run_adds_a_clean_copy_and_keeps_the_original(self):
        r = self.run_pipeline(self.video, "--frames", "30", "--quality", "preview")
        self.assertEqual(r.returncode, 0, r.stdout[-2000:] + r.stderr[-2000:])
        run = (self.tmp / "workspace" / "vase" / "latest").resolve()
        names = sorted(p.name for p in (run / "splat").glob("*.ply"))
        self.assertEqual(names, ["preview_3000_clean.ply", "preview_3000.ply"][::-1] if names[0].endswith("0.ply")
                         else names)
        self.assertTrue((run / "splat" / "preview_3000.ply").exists())
        self.assertTrue((run / "splat" / "preview_3000_clean.ply").exists())
        self.assertIn("Clean copy:", r.stdout)
        self.assertIn("clean splat:", (run / "summary.txt").read_text())

    def test_old_clean_flag_is_still_accepted(self):
        r = self.run_pipeline(self.video, "--frames", "30", "--quality", "preview", "--clean")
        self.assertEqual(r.returncode, 0, r.stdout[-2000:] + r.stderr[-2000:])
        run = (self.tmp / "workspace" / "vase" / "latest").resolve()
        self.assertEqual(sorted(p.name for p in (run / "splat").glob("*.ply")),
                         ["preview_3000.ply", "preview_3000_clean.ply"])
        self.assertNotIn("Object copy", r.stdout)

    def test_remove_background_option_adds_an_object_copy(self):
        r = self.run_pipeline(self.video, "--frames", "30", "--quality", "preview", "--remove-background")
        self.assertEqual(r.returncode, 0, r.stdout[-2000:] + r.stderr[-2000:])
        run = (self.tmp / "workspace" / "vase" / "latest").resolve()
        names = sorted(p.name for p in (run / "splat").glob("*.ply"))
        self.assertEqual(names, ["preview_3000.ply", "preview_3000_clean.ply", "preview_3000_object.ply"])
        self.assertIn("Clean copy:", r.stdout)
        self.assertIn("Object copy:", r.stdout)
        self.assertIn("support surface removed", r.stdout)
        self.assertIn("object splat:", (run / "summary.txt").read_text())

    def test_person_masks_made_by_themselves(self):
        env = fake_tools_env(SPLAT_STUDIO_PERSON_BACKEND="disc")
        r = self.run_pipeline(self.video, "--frames", "30", "--quality", "preview", "--person", env=env)
        self.assertEqual(r.returncode, 0, r.stdout[-2000:] + r.stderr[-2000:])
        self.assertIn("Masks:", r.stdout)
        run = (self.tmp / "workspace" / "vase" / "latest").resolve()
        self.assertTrue(any((run / "dense" / "masks").glob("*.png")))
        self.assertIn("masks:", (run / "summary.txt").read_text())
        self.assertIn("--alpha-mode", (run / "summary.txt").read_text())
        # the next run reuses them
        r = self.run_pipeline(self.video, "--frames", "30", "--quality", "preview", "--person", "--no-train", env=env)
        self.assertEqual(r.returncode, 0, r.stdout[-2000:] + r.stderr[-2000:])
        self.assertNotIn("Choosing what to keep", r.stdout)

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

    def test_stop_on_gaps_stops_before_training_and_train_only_finishes_it(self):
        """#34: a side nobody filmed stops the run before Brush; the poses stay and --train-only trains them."""
        env = fake_tools_env(FAKE_COLMAP_POSES="gap")
        r = self.run_pipeline(self.video, "--frames", "30", "--quality", "preview", "--stop-on-gaps", env=env)
        self.assertEqual(r.returncode, 3, r.stdout[-2000:] + r.stderr[-2000:])
        self.assertIn("Stopped before training:", r.stdout)
        self.assertIn("degrees around the object weren't filmed", r.stdout)
        self.assertIn("Stopped: coverage", r.stdout)
        self.assertNotIn("3/3 Training", r.stdout)
        run = (self.tmp / "workspace" / "vase" / "latest").resolve()
        self.assertFalse(list((run / "splat").glob("*.ply")) if (run / "splat").exists() else [])
        self.assertIn("stopped:", (run / "summary.txt").read_text())
        self.assertIn(f"--train-only", r.stdout)
        t = self.run_pipeline("--train-only", run, "--quality", "preview", env=env)
        self.assertEqual(t.returncode, 0, t.stdout[-2000:] + t.stderr[-2000:])
        self.assertTrue(list((run / "splat").glob("preview_*.ply")))
        # filmed all the way round: the same option trains as usual; without it, gaps never stop a run
        full = self.run_pipeline(self.video, "--quality", "preview", "--stop-on-gaps",
                                 env=fake_tools_env(FAKE_COLMAP_POSES="full"))
        self.assertEqual(full.returncode, 0, full.stdout[-2000:])
        self.assertIn("3/3 Training", full.stdout)
        gap = self.run_pipeline(self.video, "--quality", "preview", env=env)
        self.assertEqual(gap.returncode, 0, gap.stdout[-2000:])
        self.assertIn("barely filmed", gap.stdout)

    def test_a_side_blocked_by_a_wall_never_stops_the_run(self):
        """#36: the same unfilmed side, with a wall standing there: nobody could have filmed it."""
        r = self.run_pipeline(self.video, "--frames", "30", "--quality", "preview", "--stop-on-gaps",
                              env=fake_tools_env(FAKE_COLMAP_POSES="wall"))
        self.assertEqual(r.returncode, 0, r.stdout[-2000:] + r.stderr[-2000:])
        self.assertIn("couldn't be filmed (something stands there)", r.stdout)
        self.assertNotIn("barely filmed", r.stdout)
        self.assertNotIn("Stopped", r.stdout)
        self.assertIn("3/3 Training", r.stdout)
        run = (self.tmp / "workspace" / "vase" / "latest").resolve()
        import json
        cov = json.loads((run / "coverage.json").read_text())
        self.assertTrue(cov["blocked"])
        self.assertLess(cov["gap_open"]["degrees"], 60)
        self.assertGreater(cov["gap"]["degrees"], 120)

    def test_dry_run_and_no_train(self):
        dry = self.run_pipeline(self.video, "--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertFalse((self.tmp / "workspace").exists())
        r = self.run_pipeline(self.video, "--frames", "30", "--no-train")
        self.assertIn("--no-train", r.stdout)
        self.assertFalse(list((self.tmp / "workspace").glob("*/run_*/splat/*.ply")))
