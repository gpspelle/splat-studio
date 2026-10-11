"""Backward compatibility: data and interfaces from earlier versions must keep working.

When a test here fails after a change, the change broke something users already rely on.
Update the test only when the break is intentional and documented in CHANGELOG.md.
"""
import json
import os
import time

from tests.helpers import FAKE_BIN, TempDirTestCase, mock_env
from plyfile import write_foggy_scene, write_images_bin, write_ply  # tests/fakes, on sys.path via helpers
from tests.integration.test_server_api import ServerTestCase
from splat_studio.server.jobs import Job, Runner
from splat_studio.server.library import DataDir

# A real summary.txt written by the first version of the COLMAP script (October 2026),
# from the user's own run - kept verbatim.
REAL_OLD_SUMMARY = """started:               2026-10-11 10:31:07
frames:                frames/Video (250 jpg)
settings:              --step 1 --max-size 1600
attempt 1:             sequential matching: 3 model(s), largest 13/250 (5%)
attempt 2:             wider overlap + loop closure, relaxed mapper: 2 model(s), largest 250/250 (100%)
best:                  sparse_try2/1  250/250 (100%), mean reprojection error 0.97px
result:                OK - train on workspace/Video/run_20261011_103107/dense
duration:              7m07s
"""

# Job fields the web UI has relied on since 0.1.0.
JOB_FIELDS_V010 = {"id", "type", "status", "created", "started", "finished", "video", "options", "cmd", "run_dir",
                   "stage", "stage_label", "step_label", "step_pct", "eta", "eta_worst", "attempt", "registered",
                   "train_step", "train_total", "train_eta", "splat", "frames", "blurry", "track_breaks", "error",
                   "returncode", "source", "thumb", "advice"}


class OldDataTest(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.data = DataDir(self.tmp / "data")

    def test_real_run_from_the_first_version_is_listed(self):
        run = self.data.workspace / "Video" / "run_20261011_103107"
        (run / "dense" / "sparse" / "0").mkdir(parents=True)
        (run / "summary.txt").write_text(REAL_OLD_SUMMARY)
        runs = self.data.disk_runs(set())
        self.assertEqual(len(runs), 1)
        r = runs[0]
        self.assertEqual((r["status"], r["source"], r["registered"]), ("done", "cli", "250/250 (100%)"))
        self.assertEqual(len(r["summary"]["attempts"]), 2)
        self.assertTrue(r["summary"]["result"].startswith("OK"))           # the UI offers "Train again"
        self.assertEqual(r["splats"], [])                                   # trained outside the pipeline
        self.assertEqual(time.strftime("%H:%M", time.localtime(r["created"])), "10:31")

    def test_job_records_from_0_1_0_still_load(self):
        old = {"id": "20261011_120000_abcd", "type": "video", "status": "done", "created": 1.0, "started": 1.0,
               "finished": 2.0, "video": "videos/a.mov", "options": {"quality": "preview"}, "cmd": ["x"],
               "run_dir": None, "splat": None}
        (self.data.jobs / old["id"]).mkdir()
        (self.data.jobs / old["id"] / "job.json").write_text(json.dumps(old))
        r = Runner(self.data, start=False)
        v = r.view(r.jobs[old["id"]])
        self.assertTrue(JOB_FIELDS_V010 <= set(v), JOB_FIELDS_V010 - set(v))
        self.assertIsNone(v["clean_splat"])

    def test_brush_window_export_names_are_handled(self):
        run = self.data.workspace / "Video" / "run_1"
        write_ply(run / "splat" / "export_30000.ply", n=50)                # Brush's own default name
        items = self.data.list_splats()
        self.assertEqual(items[0]["title"], "Video · export")
        self.assertFalse(items[0]["cleaned"])


class ApiContractTest(ServerTestCase):
    def test_job_view_keeps_all_fields_and_advice_shape(self):
        status, job = self.json("POST", "/api/jobs", {"type": "setup"})
        self.assertEqual(status, 200)
        self.assertTrue(JOB_FIELDS_V010 <= set(job), JOB_FIELDS_V010 - set(job))

    def test_upload_advice_items_keep_level_and_text(self):
        ply = write_ply(self.tmp / "x.ply", n=10)
        r, _ = self.upload(ply, "/api/import")
        self.assertEqual(r.status, 200)
        from splat_studio import guidance
        for item in guidance.video_advice(None) + guidance.run_advice({"status": "done", "registered": "9/9 (100%)"}):
            self.assertTrue({"level", "text"} <= set(item))                 # 0.1.0 shape
            self.assertTrue({"code", "params"} <= set(item))                # added for translations

    def test_validation_errors_keep_message_and_add_code(self):
        status, body = self.json("POST", "/api/jobs", {"type": "video", "video": "x", "quality": "ultra"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "quality must be preview, standard or high")   # unchanged text
        self.assertEqual(body["code"], "quality")


class CleanJobTest(ServerTestCase):
    def test_remove_fog_from_an_imported_splat(self):
        ply = write_foggy_scene(self.tmp / "room.ply", n_object=3000, n_fog=2000)
        r, data = self.upload(ply, "/api/import")
        path = json.loads(data)["path"]
        before = (self.data.root / path).read_bytes()
        status, job = self.json("POST", "/api/jobs", {"type": "clean", "splat": path})
        self.assertEqual(status, 200, job)
        done = self.wait_done(job["id"])
        self.assertEqual(done["status"], "done", done.get("error"))
        self.assertEqual(done["splat"], "imports/room_clean.ply")
        self.assertEqual(done["cleaned"]["total"], 5000)
        self.assertLess(done["cleaned"]["kept"], 3100)
        self.assertEqual(done["advice"][0]["code"], "run.cleaned")
        self.assertEqual((self.data.root / path).read_bytes(), before)    # original untouched
        _, items = self.json("GET", "/api/splats")
        cleaned = {i["name"]: i["cleaned"] for i in items}
        self.assertEqual(cleaned, {"room.ply": False, "room_clean.ply": True})

    def test_clean_uses_the_runs_cameras(self):
        run = self.data.workspace / "vase" / "run_1"
        write_images_bin(run / "dense" / "sparse" / "0" / "images.bin")
        write_foggy_scene(run / "splat" / "standard_30000.ply", n_object=2000, n_fog=2000)
        status, job = self.json("POST", "/api/jobs", {"type": "clean", "splat": "workspace/vase/run_1/splat/standard_30000.ply"})
        self.assertEqual(job["run_dir"], "workspace/vase/run_1")
        done = self.wait_done(job["id"])
        self.assertEqual(done["status"], "done", done.get("error"))
        self.assertIn("--run", done["cmd"])

    def test_rejects_non_ply(self):
        status, body = self.json("POST", "/api/jobs", {"type": "clean", "splat": "../etc/passwd"})
        self.assertEqual((status, body["code"]), (400, "splat_missing"))
