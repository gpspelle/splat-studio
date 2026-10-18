import unittest
from pathlib import Path

from tests.helpers import TempDirTestCase
from splat_studio.server.jobs import Job, Runner, build_cmd, parse_line, validate_options
from splat_studio.server.library import DataDir


def job(**kw):
    return Job(Path("/tmp/none"), id="j", type=kw.pop("type", "video"), **kw)


class ParseLineTest(unittest.TestCase):
    def test_stages_and_workspace(self):
        j = job()
        parse_line(j, "==> 1/3 Frames: frames/vase")
        self.assertEqual((j.stage, j.stage_label), (1, "Frames"))
        parse_line(j, "==> 2/3 Camera poses (COLMAP): /data/workspace/vase/run_1", rel=lambda p: "rel:" + p)
        self.assertEqual(j.run_dir, "rel:/data/workspace/vase/run_1")
        self.assertEqual(j.stage_label, "Camera poses")

    def test_frame_stats(self):
        j = job()
        parse_line(j, "Kept 413 frames, moved 7 blurry ones to frames/car/_blurry")
        parse_line(j, 'Frame stats: {"length":240.0,"target":420,"auto":true,"kept":413,"replaced":63,'
                      '"dropped":7,"dropped_at":[72.0,73.5]}')
        self.assertEqual((j.frames, j.blurry), (413, 7))
        self.assertEqual(j.frame_stats["replaced"], 63)
        parse_line(j, "Frame stats: {not json")                    # ignored
        self.assertEqual(j.frame_stats["target"], 420)

    def test_fog_cleaning_and_background_removal_are_told_apart(self):
        j = job()
        for line in ("Done. Splat: /d/workspace/v/run_1/splat/preview_3000.ply",
                     "==> Removing background fog (the original is kept)",
                     "Kept 2,900 of 3,040 splats (5% removed)",
                     "Clean copy: /d/workspace/v/run_1/splat/preview_3000_clean.ply",
                     "==> Removing the background (the original is kept)",
                     "Kept 2,100 of 3,040 splats (31% removed, support surface removed)",
                     "Object copy: /d/workspace/v/run_1/splat/preview_3000_object.ply"):
            parse_line(j, line)
        self.assertEqual(j.cleaned, {"kept": 2900, "total": 3040})
        self.assertEqual(j.isolated, {"kept": 2100, "total": 3040})
        self.assertTrue(j.splat.endswith("preview_3000.ply"))             # the job keeps the original; the UI opens the clean copy
        self.assertTrue(j.object_splat.endswith("preview_3000_object.ply"))
        k = job(type="isolate", options={"splat": "imports/r.ply"})
        parse_line(k, "Kept 10 of 20 splats (50% removed)")
        parse_line(k, "Object copy: imports/r_object.ply")
        self.assertEqual((k.isolated, k.cleaned, k.splat), ({"kept": 10, "total": 20}, None, "imports/r_object.ply"))
        self.assertEqual(build_cmd(k)[-2:], ["isolate", "imports/r.ply"])

    def test_one_splat_per_object(self):
        j = job(video="v.mov", options={"masks": True, "split_objects": True})
        self.assertEqual(build_cmd(j)[-2:], ["--masks", "--split-objects"])
        self.assertNotIn("--split-objects", build_cmd(job(video="v.mov", options={"split_objects": True})))  # needs masks
        for line in ("==> Splitting the objects (the original is kept)",
                     "Part copy (Blue mug): /d/workspace/v/run_1/splat/standard_3000_part-blue-mug.ply",
                     "Part copy (Vase (tall)): /d/workspace/v/run_1/splat/standard_3000_part-vase-tall.ply",
                     "Object copy: /d/workspace/v/run_1/splat/standard_3000_object.ply"):
            parse_line(j, line)
        self.assertEqual([p["name"] for p in j.parts], ["Blue mug", "Vase (tall)"])
        self.assertTrue(j.parts[0]["splat"].endswith("_part-blue-mug.ply"))
        self.assertTrue(j.object_splat.endswith("_object.ply"))           # background removal is told apart
        self.assertEqual(validate_options({"masks": True, "split_objects": True}), {"masks": True, "split_objects": True})

    def test_stopping_before_training_when_a_side_is_missing(self):
        """#34: the run can stop itself after the coverage check; training it later keeps the settings."""
        cmd = build_cmd(job(video="v.mov", options={"stop_on_gaps": True}))
        self.assertIn("--stop-on-gaps", cmd)
        self.assertNotIn("--stop-on-gaps", build_cmd(job(video="v.mov", options={})))      # off unless asked
        retrain = build_cmd(job(type="retrain", run_dir="workspace/v/run_1",
                                options={"stop_on_gaps": True, "masks": True, "split_objects": True}))
        self.assertNotIn("--stop-on-gaps", retrain)                                        # "train anyway"
        self.assertEqual(retrain[-2:], ["--masks", "--split-objects"])
        self.assertEqual(validate_options({"stop_on_gaps": True}), {"stop_on_gaps": True})
        j = job(video="v.mov")
        parse_line(j, "==> Stopped before training: 166 degrees around the object weren't filmed")
        self.assertIsNone(j.stopped)
        parse_line(j, "Stopped: coverage")
        self.assertEqual(j.stopped, "coverage")

    def test_fog_removal_is_not_an_option(self):
        self.assertEqual(validate_options({"clean": True}), {})            # old clients: ignored, it always runs
        self.assertNotIn("--clean", build_cmd(job(video="v.mov", options={"clean": True})))

    def test_colmap_progress_line(self):
        j = job()
        parse_line(j, "==> Attempt 2/3: wider overlap + loop closure, relaxed mapper", now=100.0)
        parse_line(j, "    matching (sequential)      ██████████░░░░░░░░░░░░░░  42%  step left 1m10s | "
                      "total 3m05s | left ~4m00s (worst case ~25m00s)", now=101.0)
        self.assertEqual(j.attempt, "2/3 - wider overlap + loop closure, relaxed mapper")
        self.assertEqual((j.step_label, j.step_pct), ("matching (sequential)", 42))
        # one line is not enough to measure: no number rather than the tool's guess
        self.assertEqual((j.eta, j.eta_worst, j.eta_total, j.eta_note), (None, None, None, "estimating"))

    def test_time_left_is_measured_from_progress(self):
        j = job()
        for i in range(0, 61, 2):                      # 1% every 2 s after a 10 s start-up
            parse_line(j, f"    extracting features        ░░  {i // 2}%  step left 1s | total 1s | left ~1s",
                       now=10.0 + i)
        self.assertEqual(j.eta, "2 min")              # 70% left at 0.5%/s = 140 s
        self.assertEqual(j.eta_note, "first_run")      # the rest of the run hasn't been timed yet

    def test_brush_progress_line_in_real_format(self):
        # Brush's indicatif template: "[{elapsed}] {bar:40} {pos:>7}/{len:7} {msg} ({per_sec}, {eta} remaining)"
        j = job()
        parse_line(j, "[00:01:12] ◍◍◍◍◍◍◍◍◍◍○○○○○○○○○○○○○○○○○○○○○○○○○○○○○○    7421/30000   Steps "
                      "(103.2/s, 3m 39s remaining)")
        self.assertEqual((j.stage, j.train_step, j.train_total, j.step_pct), (3, 7421, 30000, 24))
        self.assertEqual(j.train_eta, "3m 39s")        # Brush's own figure is kept, but not shown as "left"

    def test_results(self):
        j = job()
        parse_line(j, "Kept 230 frames, moved 20 blurry ones to frames/vase/_blurry")
        parse_line(j, "    1 model(s); largest registers 220/230 frames (96%)")
        parse_line(j, "      vase_0085.jpg -> vase_0102.jpg  (18 frames)")
        parse_line(j, "Done. Splat: /data/workspace/vase/run_1/splat/standard_30000.ply")
        self.assertEqual((j.frames, j.blurry), (230, 20))
        self.assertEqual(j.registered, "220/230 frames (96%)")
        self.assertEqual(j.track_breaks, [{"first": "vase_0085.jpg", "last": "vase_0102.jpg", "count": 18}])
        self.assertTrue(j.splat.endswith("standard_30000.ply"))

    def test_errors_are_kept(self):
        j = job()
        parse_line(j, "Not good enough to train on. Likely causes: ...")
        self.assertIn("Not good enough", j.error)
        parse_line(j, "   ")
        self.assertIn("Not good enough", j.error)


class BuildCmdTest(unittest.TestCase):
    def tail(self, cmd):
        return cmd[cmd.index("splat_studio.pipeline") + 1:]

    def test_video_job(self):
        j = job(video="videos/a.mov", options={"quality": "high", "frames": 300, "start": "0:03", "eval": True,
                                               "reextract": True, "brush_args": "--sh-degree 1"})
        self.assertEqual(self.tail(build_cmd(j)),
                         ["videos/a.mov", "--frames", "300", "--start", "0:03", "--re-extract", "--quality", "high",
                          "--detail", "auto", "--eval", "--", "--sh-degree", "1"])

    def test_auto_frames(self):
        j = job(video="videos/a.mov", options={"frames": "auto"})
        self.assertEqual(self.tail(build_cmd(j))[:3], ["videos/a.mov", "--frames", "auto"])
        f = build_cmd(job(type="frames", video="videos/a.mov", options={"frames_dir": "frames/a"}))
        self.assertEqual(f[f.index("--target") + 1], "auto")

    def test_retrain_and_setup(self):
        self.assertEqual(self.tail(build_cmd(job(type="retrain", run_dir="workspace/a/run_1", options={})))[:2],
                         ["--train-only", "workspace/a/run_1"])
        self.assertEqual(self.tail(build_cmd(job(type="setup"))), ["--setup-only"])


class ValidateOptionsTest(unittest.TestCase):
    def test_accepts_good_options(self):
        self.assertEqual(validate_options({"quality": "high", "start": "1:05", "eval": True, "frames": 250}),
                         {"quality": "high", "start": "1:05", "eval": True, "frames": 250})
        self.assertEqual(validate_options({"eval": False, "start": ""}), {})
        self.assertEqual(validate_options({"frames": "auto"}), {"frames": "auto"})
        self.assertEqual(validate_options({"frames": "400"}), {"frames": 400})

    def test_rejects_bad_options(self):
        for body in ({"quality": "ultra"}, {"detail": "x"}, {"frames": 5}, {"frames": "lots"}, {"start": "1;rm -rf"},
                     {"brush_args": "--x $(whoami)"}):
            with self.assertRaises(ValueError, msg=str(body)):
                validate_options(body)


class RunnerQueueTest(TempDirTestCase):
    def test_submit_cancel_delete_and_reload(self):
        data = DataDir(self.tmp / "data")
        r = Runner(data, start=False)                       # don't execute anything
        a = r.submit("video", video="videos/a.mov", options={"quality": "preview"})
        b = r.submit("setup")
        self.assertEqual(r._next().id, a.id)               # oldest first
        self.assertTrue(r.cancel(b.id))
        self.assertEqual(r.jobs[b.id].status, "cancelled")
        self.assertFalse(r.delete(a.id))                    # queued jobs can't be deleted
        self.assertTrue(r.delete(b.id))
        self.assertFalse((data.jobs / b.id).exists())
        # a job that was running when the server stopped is marked interrupted on reload
        a.status = "running"
        a.save(force=True)
        r2 = Runner(data, start=False)
        self.assertEqual(r2.jobs[a.id].status, "interrupted")


    def test_splat_in_use(self):
        data = DataDir(self.tmp / "data")
        r = Runner(data, start=False)
        ply = "workspace/vase/run_1/splat/standard_30000.ply"
        self.assertFalse(r.splat_in_use(ply))
        c = r.submit("clean", options={"splat": "imports/scan.ply"})
        self.assertTrue(r.splat_in_use("imports/scan.ply"))         # a queued job reads it
        self.assertFalse(r.splat_in_use("imports/other.ply"))
        t = r.submit("retrain", run_dir="workspace/vase/run_1")
        self.assertTrue(r.splat_in_use(ply))                        # training writes into that run
        r.cancel(c.id), r.cancel(t.id)
        self.assertFalse(r.splat_in_use(ply) or r.splat_in_use("imports/scan.ply"))
        lock = data.workspace / "vase" / "run_1" / ".lock"
        lock.parent.mkdir(parents=True)
        lock.write_text("999999999")                                # stale lock from a crashed run
        self.assertFalse(r.splat_in_use(ply))


if __name__ == "__main__":
    unittest.main()
