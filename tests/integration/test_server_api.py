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
