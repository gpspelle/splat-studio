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
        self.assertTrue(j.splat.endswith("preview_3000.ply"))             # View splat stays the original
        self.assertTrue(j.object_splat.endswith("preview_3000_object.ply"))
        k = job(type="isolate", options={"splat": "imports/r.ply"})
        parse_line(k, "Kept 10 of 20 splats (50% removed)")
        parse_line(k, "Object copy: imports/r_object.ply")
        self.assertEqual((k.isolated, k.cleaned, k.splat), ({"kept": 10, "total": 20}, None, "imports/r_object.ply"))
        self.assertEqual(build_cmd(k)[-2:], ["isolate", "imports/r.ply"])

    def test_colmap_progress_line(self):
        j = job()
        parse_line(j, "==> Attempt 2/3: wider overlap + loop closure, relaxed mapper")
        parse_line(j, "    matching (sequential)      ██████████░░░░░░░░░░░░░░  42%  step left 1m10s | "
                      "total 3m05s | left ~4m00s (worst case ~25m00s)")
        self.assertEqual(j.attempt, "2/3 - wider overlap + loop closure, relaxed mapper")
        self.assertEqual((j.step_label, j.step_pct, j.eta, j.eta_worst),
                         ("matching (sequential)", 42, "~4m00s", "~25m00s"))

    def test_brush_progress_line_in_real_format(self):
        # Brush's indicatif template: "[{elapsed}] {bar:40} {pos:>7}/{len:7} {msg} ({per_sec}, {eta} remaining)"
        j = job()
        parse_line(j, "[00:01:12] ◍◍◍◍◍◍◍◍◍◍○○○○○○○○○○○○○○○○○○○○○○○○○○○○○○    7421/30000   Steps "
                      "(103.2/s, 3m 39s remaining)")
        self.assertEqual((j.stage, j.train_step, j.train_total, j.step_pct, j.eta), (3, 7421, 30000, 24, "3m 39s"))

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

    def test_retrain_and_setup(self):
        self.assertEqual(self.tail(build_cmd(job(type="retrain", run_dir="workspace/a/run_1", options={})))[:2],
                         ["--train-only", "workspace/a/run_1"])
        self.assertEqual(self.tail(build_cmd(job(type="setup"))), ["--setup-only"])


class ValidateOptionsTest(unittest.TestCase):
    def test_accepts_good_options(self):
        self.assertEqual(validate_options({"quality": "high", "start": "1:05", "eval": True, "frames": 250}),
                         {"quality": "high", "start": "1:05", "eval": True, "frames": 250})
        self.assertEqual(validate_options({"eval": False, "start": ""}), {})

    def test_rejects_bad_options(self):
        for body in ({"quality": "ultra"}, {"detail": "x"}, {"frames": 5}, {"start": "1;rm -rf"},
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
