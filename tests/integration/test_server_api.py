"""The web server's HTTP API, with real jobs running the pipeline on stand-in tools."""
import http.client
import io
import json
import os
import threading
import time
import zipfile
from urllib.parse import quote, urlencode

from tests.helpers import FAKE_BIN, ROOT, TempDirTestCase, make_video, mock_env, needs_ffmpeg, write_ply
from splat_studio.server.app import create_server


class ServerTestCase(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.envctx = mock_env({"PATH": f"{FAKE_BIN}{os.pathsep}{os.environ['PATH']}",
                                "SPLAT_IN_CONTAINER": "1", "FAKE_BRUSH_DELAY": "0.02",
                                "SPLAT_STUDIO_QUIET": "1"})
        self.envctx.__enter__()
        self.srv, self.runner, self.data = create_server(self.tmp / "data", port=0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.runner.stop_all()
        self.srv.shutdown()
        self.srv.server_close()
        self.envctx.__exit__(None, None, None)
        super().tearDown()

    def request(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r, data

    def json(self, method, path, obj=None):
        body = json.dumps(obj).encode() if obj is not None else None
        r, data = self.request(method, path, body, {"Content-Type": "application/json"} if body else {})
        return r.status, json.loads(data or b"null")

    def upload(self, path, endpoint="/api/upload", name=None):
        return self.request("POST", endpoint, path.read_bytes(), {"X-Filename": quote(name or path.name)})

    def wait_done(self, jid, timeout=180):
        end = time.time() + timeout
        while time.time() < end:
            _, jobs = self.json("GET", "/api/jobs")
            j = next(x for x in jobs if x["id"] == jid)
            if j["status"] not in ("queued", "running"):
                return j
            time.sleep(0.3)
        self.fail(f"job {jid} didn't finish")


class StaticAndSecurityTest(ServerTestCase):
    def test_pages_and_vendor_files(self):
        for path in ("/", "/viewer", "/vendor/spark/spark.module.min.js", "/vendor/three/three.module.min.js"):
            r, _ = self.request("GET", path)
            self.assertEqual(r.status, 200, path)
        r, _ = self.request("GET", "/vendor/spark/spark.module.min.js")
        self.assertEqual(r.getheader("Content-Type"), "text/javascript")

    def test_no_access_outside_data_folder(self):
        (self.tmp / "secret.txt").write_text("nope")
        for path in ("/files/../secret.txt", "/files/%2e%2e/secret.txt", "/../../etc/passwd",
                     "/api/export?file=../secret.txt"):
            r, data = self.request("GET", path)
            self.assertEqual(r.status, 404, path)
            self.assertNotIn(b"nope", data)

    def test_server_only_listens_locally(self):
        self.assertEqual(self.srv.server_address[0], "127.0.0.1")

    def test_rejects_bad_requests(self):
        r, _ = self.request("POST", "/api/upload", b"x", {"X-Filename": "evil.sh"})
        self.assertEqual(r.status, 400)
        status, body = self.json("POST", "/api/jobs", {"type": "video", "video": "videos/missing.mov"})
        self.assertEqual(status, 400)
        status, body = self.json("POST", "/api/jobs", {"type": "video", "video": "x", "quality": "ultra"})
        self.assertEqual((status, body["error"]), (400, "quality must be preview, standard or high"))
        status, _ = self.json("POST", "/api/jobs", {"type": "retrain", "run_dir": "workspace/none"})
        self.assertEqual(status, 400)

    def test_setup_status(self):
        status, body = self.json("GET", "/api/setup")
        self.assertEqual(status, 200)
        names = {i["name"]: i for i in body["items"]}
        self.assertTrue(names["COLMAP"]["ok"])          # the fake on PATH
        self.assertEqual(body["data_dir"], str(self.data.root))


class LibraryApiTest(ServerTestCase):
    def test_import_view_range_and_export(self):
        ply = write_ply(self.tmp / "scan.ply", n=500)
        r, data = self.upload(ply, "/api/import")
        self.assertEqual(r.status, 200)
        path = json.loads(data)["path"]
        self.assertEqual(path, "imports/scan.ply")
        r, _ = self.upload(ply, "/api/import")                       # same name -> kept, renamed
        self.assertEqual(json.loads(_)["path"], "imports/scan_2.ply")
        _, splats = self.json("GET", "/api/splats")
        self.assertEqual({s["name"] for s in splats}, {"scan.ply", "scan_2.ply"})
        r, head = self.request("GET", "/files/" + path, headers={"Range": "bytes=0-2"})
        self.assertEqual((r.status, head), (206, b"ply"))
        r, data = self.request("GET", "/api/export?" + urlencode({"file": path, "title": "Scan",
                                                                   "rotation": "[0,0,0,1]"}))
        self.assertEqual(r.status, 200)
        z = zipfile.ZipFile(io.BytesIO(data))
        self.assertEqual(json.loads(z.read("viewer-config.json"))["rotation"], [0, 0, 0, 1])
        r, _ = self.request("GET", "/api/export?" + urlencode({"file": path, "rotation": "[1,2]"}))
        self.assertEqual(r.status, 400)


    def test_delete_splat(self):
        path = json.loads(self.upload(write_ply(self.tmp / "scan.ply", n=200), "/api/import")[1])["path"]
        self.assertEqual(self.json("DELETE", "/api/splats?" + urlencode({"file": "../secret.ply"}))[0], 404)
        self.assertEqual(self.json("DELETE", "/api/splats?" + urlencode({"file": "videos/a.mov"}))[0], 404)
        status, res = self.json("DELETE", "/api/splats?" + urlencode({"file": path}))
        self.assertEqual((status, res), (200, {"ok": True}))
        _, splats = self.json("GET", "/api/splats")
        self.assertNotIn(path, [s["path"] for s in splats])
        self.assertEqual(self.json("DELETE", "/api/splats?" + urlencode({"file": path}))[0], 404)
        run = self.data.workspace / "vase" / "run_1"                   # a command-line run still training
        ply = write_ply(run / "splat" / "standard_30000.ply", n=200)
        (run / ".lock").write_text(str(os.getpid()))
        status, res = self.json("DELETE", "/api/splats?" + urlencode({"file": self.data.rel(ply)}))
        self.assertEqual((status, res["code"]), (409, "splat_busy"))
        self.assertTrue(ply.exists())

@needs_ffmpeg
class JobsApiTest(ServerTestCase):
    def test_upload_run_and_retrain(self):
        video = make_video(self.tmp / "Vase Clip.mov", seconds=6)
        r, data = self.upload(video)
        self.assertEqual(r.status, 200)
        up = json.loads(data)
        self.assertEqual(up["video"], "videos/Vase_Clip.mov")       # safe file name
        self.assertTrue(any(a["level"] == "warn" for a in up["advice"]))   # 6 s, 360p -> warnings
        _, vids = self.json("GET", "/api/videos")
        self.assertEqual(vids[0]["advice"], up["advice"])            # cached with the video

        status, job = self.json("POST", "/api/jobs", {"type": "video", "video": up["video"], "quality": "preview",
                                                      "frames": 40})
        self.assertEqual(status, 200, job)
        done = self.wait_done(job["id"])
        self.assertEqual(done["status"], "done", done.get("error"))
        self.assertEqual(done["stage"], 3)
        self.assertEqual(done["train_step"], done["train_total"])    # Brush progress was parsed
        self.assertEqual(done["registered"], f"{done['frames']}/{done['frames']} frames (100%)")
        self.assertTrue(done["splat"].startswith("workspace/Vase_Clip/run_"))
        self.assertEqual(done["advice"][0]["level"], "ok")
        self.assertTrue(done["thumb"].endswith(".jpg"))
        _, log = self.json("GET", f"/api/jobs/{job['id']}/log")
        self.assertIn("==> 3/3 Training", log["log"])

        status, rt = self.json("POST", "/api/jobs", {"type": "retrain", "run_dir": done["run_dir"], "quality": "high"})
        self.assertEqual(status, 200)
        rt_done = self.wait_done(rt["id"])
        self.assertEqual(rt_done["status"], "done", rt_done.get("error"))
        self.assertIn("/splat/high_", rt_done["splat"])
        _, jobs = self.json("GET", "/api/jobs")
        self.assertEqual(len(jobs), 2)                                # the re-train doesn't duplicate the run

    def test_broken_track_gives_timestamped_advice(self):
        os.environ["FAKE_COLMAP_REGISTER"] = "0.8"
        try:
            video = make_video(self.tmp / "shaky.mov", seconds=10)
            up = json.loads(self.upload(video)[1])
            _, job = self.json("POST", "/api/jobs", {"type": "video", "video": up["video"], "quality": "preview",
                                                     "frames": 40})
            done = self.wait_done(job["id"])
        finally:
            del os.environ["FAKE_COLMAP_REGISTER"]
        self.assertEqual(done["status"], "done", done.get("error"))
        self.assertTrue(done["track_breaks"])
        self.assertRegex(done["track_breaks"][0]["when"], r"^\d:\d\d–\d:\d\d$")
        text = " ".join(a["text"] for a in done["advice"])
        self.assertIn(done["track_breaks"][0]["when"], text)

    def test_cancel_queued_and_delete(self):
        video = make_video(self.tmp / "a.mov", seconds=4)
        up = json.loads(self.upload(video)[1])
        _, first = self.json("POST", "/api/jobs", {"type": "video", "video": up["video"], "quality": "preview"})
        _, second = self.json("POST", "/api/jobs", {"type": "video", "video": up["video"], "quality": "preview"})
        _, res = self.json("POST", f"/api/jobs/{second['id']}/cancel")
        self.assertTrue(res["ok"])
        self.wait_done(first["id"])
        _, jobs = self.json("GET", "/api/jobs")
        self.assertEqual(next(j for j in jobs if j["id"] == second["id"])["status"], "cancelled")
        _, res = self.json("DELETE", f"/api/jobs/{second['id']}")
        self.assertTrue(res["ok"])
        _, jobs = self.json("GET", "/api/jobs")
        self.assertNotIn(second["id"], [j["id"] for j in jobs])


class TimeLeftTest(ServerTestCase):
    """Time-left estimates during a real (stand-in) run, and what the next run learns from it."""

    def job(self, jid):
        _, jobs = self.json("GET", "/api/jobs")
        return next(j for j in jobs if j["id"] == jid)

    def test_estimates_during_training_and_history_for_the_next_run(self):
        import os
        from splat_studio.eta import History, default_timings_file
        os.environ["FAKE_BRUSH_LINES"], os.environ["FAKE_BRUSH_DELAY"] = "40", "0.4"
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("FAKE_BRUSH_LINES", "FAKE_BRUSH_DELAY")])
        video = make_video(self.tmp / "vase.mov", seconds=6)
        _, data = self.upload(video)
        _, job = self.json("POST", "/api/jobs", {"type": "video", "video": json.loads(data)["video"],
                                                 "quality": "preview", "frames": 40})
        seen = None
        end = time.time() + 180
        while time.time() < end:
            j = self.job(job["id"])
            if j["status"] != "running" and j["status"] != "queued":
                break
            if j.get("train_total") and j["train_step"] >= 0.7 * j["train_total"]:
                seen = seen or j
            time.sleep(0.3)
        self.assertEqual(j["status"], "done", j.get("error"))
        self.assertIsNotNone(seen, "never saw training past 70%")
        self.assertRegex(seen["eta"] or "", r"^\d+s$")                     # a few seconds, measured
        self.assertIsNone(seen["eta_worst"])
        self.assertIsNone(j["eta"])                                        # finished: nothing left
        h = History(default_timings_file())
        self.assertIsNotNone(h.get("train.per_step.preview"))
        self.assertIsNotNone(h.get("colmap.extract"))
        timeline = (self.data.jobs / job["id"] / "timeline.jsonl").read_text().splitlines()
        self.assertGreater(len(timeline), 5)
        self.assertEqual(json.loads(timeline[-1])["stage"], 3)
        # the next run on this computer knows the total from the start
        _, rt = self.json("POST", "/api/jobs", {"type": "retrain", "run_dir": j["run_dir"], "quality": "preview"})
        end = time.time() + 60
        while time.time() < end:
            r = self.job(rt["id"])
            if r["status"] == "running" and r.get("eta_total"):
                break
            time.sleep(0.2)
        self.assertTrue(r.get("eta_total"), r)
        self.wait_done(rt["id"])


@needs_ffmpeg
class ChooseWhatToKeepTest(ServerTestCase):
    """Frames -> clicks -> masks for every frame -> a run that trains only the chosen object
    (with the stand-in 'disc' segmentation instead of SAM 2)."""

    def setUp(self):
        super().setUp()
        self.maskenv = mock_env({"SPLAT_STUDIO_MASK_BACKEND": "disc"})
        self.maskenv.__enter__()

    def tearDown(self):
        self.maskenv.__exit__(None, None, None)
        super().tearDown()

    def test_select_mask_and_train(self):
        r, _ = self.request("GET", "/keep")
        self.assertEqual(r.status, 200)
        up = json.loads(self.upload(make_video(self.tmp / "vase.mov", seconds=4))[1])
        q = urlencode({"video": up["video"]})
        _, info = self.json("GET", f"/api/frames?{q}")
        self.assertEqual((info["frames"], info["masks_done"], info["backend"]), ([], False, "disc"))

        _, job = self.json("POST", "/api/jobs", {"type": "frames", "video": up["video"], "frames": 24})
        _, again = self.json("POST", "/api/jobs", {"type": "frames", "video": up["video"], "frames": 24})
        self.assertEqual(again["id"], job["id"])                     # page opened twice: one frames job
        self.assertEqual(self.wait_done(job["id"])["status"], "done")
        _, info = self.json("GET", f"/api/frames?{q}")
        self.assertGreaterEqual(len(info["frames"]), 20)
        first = info["frames"][0]

        status, prev = self.json("POST", "/api/keep/preview", {"video": up["video"], "frame": first,
                                                                "points": [[0.5, 0.5, 1], [0.9, 0.1, 0]]})
        self.assertEqual(status, 200, prev)
        r, png = self.request("GET", "/files/" + prev["mask"])
        self.assertEqual((r.status, png[:4]), (200, b"\x89PNG"))
        status, err = self.json("POST", "/api/keep/preview", {"video": up["video"], "frame": first,
                                                               "points": [[0.9, 0.1, 0]]})
        self.assertEqual((status, err["code"]), (400, "no_keep_point"))
        status, err = self.json("POST", "/api/jobs", {"type": "mask", "video": up["video"], "prompts": {}})
        self.assertEqual((status, err["code"]), (400, "no_keep_point"))

        _, job = self.json("POST", "/api/jobs", {"type": "mask", "video": up["video"],
                                                 "prompts": {first: [[0.5, 0.5, 1], [0.9, 0.1, 0]]}})
        done = self.wait_done(job["id"])
        self.assertEqual(done["status"], "done", done.get("error"))
        self.assertTrue(done["masks"].endswith("frames/vase/masks"), done["masks"])
        _, info = self.json("GET", f"/api/frames?{q}")
        self.assertTrue(info["masks_done"])
        self.assertEqual(info["prompts"], {first: [[0.5, 0.5, 1], [0.9, 0.1, 0]]})

        # a slipped frame: one more click there redoes only the frames around it (#11)
        later, last = info["frames"][10], info["frames"][15]
        _, job = self.json("POST", "/api/jobs", {"type": "mask", "video": up["video"], "changed": [later, "nope.jpg"],
                                                 "prompts": {first: [[0.5, 0.5, 1], [0.9, 0.1, 0]], later: [[0.4, 0.5, 1]],
                                                             last: [[0.5, 0.5, 1]]}})
        self.assertEqual(self.wait_done(job["id"])["status"], "done")
        _, log = self.json("GET", f"/api/jobs/{job['id']}/log")
        self.assertIn(f"1 changed frame(s), 16 of {len(info['frames'])} frames to redo", log["log"])   # frames 0-15
        _, info = self.json("GET", f"/api/frames?{q}")
        self.assertTrue(info["masks_done"])

        _, job = self.json("POST", "/api/jobs", {"type": "video", "video": up["video"], "quality": "preview",
                                                 "frames": 24, "masks": True})
        done = self.wait_done(job["id"])
        self.assertEqual(done["status"], "done", done.get("error"))
        run = self.data.root / done["run_dir"]
        dense = run / "dense"
        self.assertEqual(len(list((dense / "masks").glob("*.png"))), len(list((dense / "images").glob("*.jpg"))))
        summary = (run / "summary.txt").read_text()
        self.assertIn("--alpha-mode transparent", summary)
        # the library shows the chosen object, not the first frame
        self.assertTrue((run / "thumbs" / "run.jpg").exists())
        self.assertTrue(done["thumb"].endswith("/thumbs/run.jpg"), done["thumb"])
        _, lib = self.json("GET", "/api/splats")
        self.assertTrue(all(x["thumb"].endswith("/thumbs/run.jpg") for x in lib if x["video"] == "vase"))
        self.assertIn("masks:", summary)

    def test_runs_without_masks_are_unchanged(self):
        up = json.loads(self.upload(make_video(self.tmp / "cup.mov", seconds=4))[1])
        _, job = self.json("POST", "/api/jobs", {"type": "video", "video": up["video"], "quality": "preview",
                                                 "frames": 24})
        done = self.wait_done(job["id"])
        self.assertEqual(done["status"], "done", done.get("error"))
        run = self.data.root / done["run_dir"]
        self.assertFalse((run / "dense" / "masks").exists())
        self.assertNotIn("--alpha-mode", (run / "summary.txt").read_text())


@needs_ffmpeg
class SeveralObjectsTest(ServerTestCase):
    """Two objects chosen in one video: masks for each, both kept for training, and the run tries
    to split them (the stand-in COLMAP has no real camera poses, so it reports why it couldn't)."""

    def test_two_objects(self):
        with mock_env({"SPLAT_STUDIO_MASK_BACKEND": "disc"}):
            up = json.loads(self.upload(make_video(self.tmp / "table.mov", seconds=4))[1])
            _, job = self.json("POST", "/api/jobs", {"type": "frames", "video": up["video"], "frames": 24})
            self.assertEqual(self.wait_done(job["id"])["status"], "done")
            q = urlencode({"video": up["video"]})
            first = self.json("GET", f"/api/frames?{q}")[1]["frames"][0]
            pts = [[0.25, 0.5, 1], [0.75, 0.5, 2], [0.5, 0.95, 0]]
            status, prev = self.json("POST", "/api/keep/preview", {"video": up["video"], "frame": first, "points": pts})
            self.assertEqual(status, 200, prev)
            self.assertEqual(sorted(prev["masks"]), ["1", "2"])
            _, job = self.json("POST", "/api/jobs", {"type": "mask", "video": up["video"], "prompts": {first: pts},
                                                     "objects": [{"id": 1, "name": "Mug"}, {"id": 2, "name": "Vase"},
                                                                 {"id": 5, "name": "never clicked"}]})
            self.assertEqual(self.wait_done(job["id"])["status"], "done")
            _, info = self.json("GET", f"/api/frames?{q}")
            self.assertEqual(info["objects"], [{"id": 1, "name": "Mug"}, {"id": 2, "name": "Vase"}])
            frames = self.data.root / info["frames_dir"]
            self.assertEqual(len(list((frames / "masks" / "objects" / "2").glob("*.png"))), len(info["frames"]))

            _, job = self.json("POST", "/api/jobs", {"type": "video", "video": up["video"], "quality": "preview",
                                                     "frames": 24, "masks": True, "split_objects": True})
            done = self.wait_done(job["id"])
        self.assertEqual(done["status"], "done", done.get("error"))
        self.assertTrue(done["options"]["split_objects"])
        self.assertTrue((self.data.root / done["run_dir"] / "dense" / "masks").is_dir())
        _, log = self.json("GET", f"/api/jobs/{job['id']}/log")
        self.assertIn("==> Splitting the objects", log["log"])
        self.assertIn("couldn't split the objects", log["log"])      # stand-in COLMAP: no camera poses
