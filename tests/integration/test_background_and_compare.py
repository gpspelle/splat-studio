"""0.3.0 features over HTTP: background removal jobs and the comparison page/export."""
import io
import json
import zipfile
from urllib.parse import urlencode

from tests.helpers import write_ply
from plyfile import write_images_bin, write_room_scene  # tests/fakes (on the path via tests.helpers)
from tests.integration.test_server_api import ServerTestCase


class IsolateJobTest(ServerTestCase):
    def test_remove_background_from_an_imported_splat(self):
        r, data = self.upload(write_room_scene(self.tmp / "room.ply"), "/api/import")
        path = json.loads(data)["path"]
        before = (self.data.root / path).read_bytes()
        status, job = self.json("POST", "/api/jobs", {"type": "isolate", "splat": path})
        self.assertEqual(status, 200, job)
        done = self.wait_done(job["id"])
        self.assertEqual(done["status"], "done", done.get("error"))
        self.assertEqual(done["splat"], "imports/room_object.ply")
        self.assertEqual(done["object_splat"], "imports/room_object.ply")
        self.assertIsNone(done["cleaned"])                       # not mistaken for fog cleaning
        self.assertEqual(done["isolated"]["total"], 18000)
        self.assertLess(done["isolated"]["kept"], 6500)
        self.assertEqual(done["advice"][0]["code"], "run.isolated")
        self.assertEqual((self.data.root / path).read_bytes(), before)    # original untouched
        _, items = self.json("GET", "/api/splats")
        flags = {i["name"]: (i["cleaned"], i["isolated"]) for i in items}
        self.assertEqual(flags, {"room.ply": (False, False), "room_object.ply": (False, True)})

    def test_isolate_uses_the_runs_cameras(self):
        run = self.data.workspace / "vase" / "run_1"
        write_images_bin(run / "dense" / "sparse" / "0" / "images.bin", radius=1.4, target=(0.0, -0.55, 0.0))
        write_room_scene(run / "splat" / "standard_30000.ply")
        status, job = self.json("POST", "/api/jobs", {"type": "isolate",
                                                      "splat": "workspace/vase/run_1/splat/standard_30000.ply"})
        self.assertEqual(job["run_dir"], "workspace/vase/run_1")
        self.assertIn("--run", job["cmd"])
        done = self.wait_done(job["id"])
        self.assertEqual(done["status"], "done", done.get("error"))
        _, items = self.json("GET", "/api/splats")
        titles = {i["name"]: i["title"] for i in items}
        self.assertEqual(titles["standard_30000_object.ply"], "vase · standard")

    def test_rejects_missing_splat(self):
        status, body = self.json("POST", "/api/jobs", {"type": "isolate", "splat": "imports/nope.ply"})
        self.assertEqual((status, body["code"]), (400, "splat_missing"))

    def test_remove_background_option_is_passed_to_the_pipeline(self):
        from splat_studio.server.jobs import validate_options
        self.assertEqual(validate_options({"remove_background": True}), {"remove_background": True})
        self.assertEqual(validate_options({"remove_background": False}), {})


class CompareTest(ServerTestCase):
    def setUp(self):
        super().setUp()
        for name in ("a.ply", "b.ply"):
            self.upload(write_ply(self.tmp / name, n=2000), "/api/import")

    def test_compare_page_is_served(self):
        r, body = self.request("GET", "/compare?a=imports/a.ply&b=imports/b.ply")
        self.assertEqual(r.status, 200)
        self.assertIn(b"compareViewer", body)

    def test_export_comparison_site(self):
        q = urlencode({"a": "imports/a.ply", "b": "imports/b.ply", "ta": "Left", "tb": "Right",
                       "ra": "[1,0,0,0]", "cb": "1.3"})
        r, body = self.request("GET", "/api/export-compare?" + q)
        self.assertEqual(r.status, 200)
        self.assertIn("Left_vs_Right_compare.zip", r.headers["Content-Disposition"])
        z = zipfile.ZipFile(io.BytesIO(body))
        names = set(z.namelist())
        self.assertTrue({"index.html", "i18n.js", "compare-config.json", "model_a.ply", "model_b.ply",
                         "README.txt", "vendor/spark/spark.module.min.js"} <= names)
        self.assertIn(b"compareViewer", z.read("index.html"))
        cfg = json.loads(z.read("compare-config.json"))
        self.assertEqual(cfg["models"][0], {"file": "model_a.ply", "title": "Left", "rotation": [1, 0, 0, 0]})
        self.assertEqual(cfg["models"][1], {"file": "model_b.ply", "title": "Right", "rotation": None, "crop": 1.3})
        self.assertEqual(z.read("model_a.ply"), (self.data.imports / "a.ply").read_bytes())

    def test_export_validation(self):
        r, _ = self.request("GET", "/api/export-compare?a=imports/a.ply&b=imports/missing.ply")
        self.assertEqual(r.status, 404)
        r, _ = self.request("GET", "/api/export-compare?a=imports/a.ply&b=imports/b.ply&ra=[1,2]")
        self.assertEqual(r.status, 400)
        r, _ = self.request("GET", "/api/export-compare?a=imports/a.ply&b=imports/b.ply&ca=50")
        self.assertEqual(r.status, 400)
        r, _ = self.request("GET", "/api/export-compare?a=../../etc/passwd&b=imports/b.ply")
        self.assertEqual(r.status, 404)
