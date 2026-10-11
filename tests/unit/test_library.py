import io
import json
import zipfile

from tests.helpers import TempDirTestCase, write_ply
from splat_studio.server.library import DataDir, export_site, read_summary, splat_title


class LibraryTest(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.data = DataDir(self.tmp / "data")
        self.run = self.data.workspace / "vase" / "run_20260101_120000"
        (self.run / "splat").mkdir(parents=True)

    def test_safe_path_blocks_traversal(self):
        self.assertIsNone(self.data.safe_path("../secret"))
        self.assertIsNone(self.data.safe_path("%2e%2e/secret"))
        self.assertIsNone(self.data.safe_path("/etc/passwd"))
        self.assertIsNone(self.data.safe_path(""))
        self.assertEqual(self.data.safe_path("videos/a.mov"), self.data.root / "videos" / "a.mov")

    def test_rel(self):
        self.assertEqual(self.data.rel(self.run), "workspace/vase/run_20260101_120000")
        self.assertEqual(self.data.rel("videos/a.mov"), "videos/a.mov")

    def test_splat_title(self):
        self.assertEqual(splat_title("workspace/vase/run_1/splat/high_40000.ply"), "vase · high")
        self.assertEqual(splat_title("imports/scan.ply"), "scan.ply")

    def test_read_summary_collects_repeated_keys(self):
        (self.run / "summary.txt").write_text(
            "started:               2026-01-01 12:00:00\nattempt 1:             sequential: 3 model(s)\n"
            "attempt 2:             wider: 1 model(s)\ntrack break:           a -> b (5 frames)\n"
            "result:                OK - train on x\n")
        s = read_summary(self.run)
        self.assertEqual(len(s["attempts"]), 2)
        self.assertEqual(s["track_breaks"], ["a -> b (5 frames)"])
        self.assertTrue(s["result"].startswith("OK"))

    def test_splats_and_thumbnails(self):
        write_ply(self.run / "splat" / "standard_30000.ply", n=200)
        write_ply(self.data.imports / "scan.ply", n=200)
        frames = self.data.frames / "vase"
        frames.mkdir(parents=True)
        (frames / "vase_0001.jpg").write_bytes(b"jpg")
        items = {i["name"]: i for i in self.data.list_splats()}
        self.assertEqual(items["standard_30000.ply"]["title"], "vase · standard")
        self.assertEqual(items["standard_30000.ply"]["thumb"], "frames/vase/vase_0001.jpg")
        self.assertEqual(items["scan.ply"]["source"], "imported")
        self.assertEqual(self.data.latest_splat(self.data.rel(self.run)), self.data.rel(self.run / "splat" / "standard_30000.ply"))

    def test_disk_runs_reports_cli_runs(self):
        (self.run / "summary.txt").write_text("started:               2026-01-01 12:00:00\n"
                                              "best:                  sparse_try1/0  240/250 (96%), err\n"
                                              "result:                OK - train on x\n")
        runs = self.data.disk_runs(known=set())
        self.assertEqual(len(runs), 1)
        self.assertEqual((runs[0]["status"], runs[0]["source"], runs[0]["registered"]), ("done", "cli", "240/250 (96%)"))
        self.assertEqual(self.data.disk_runs(known={self.data.rel(self.run)}), [])
        (self.run / ".lock").write_text("1")
        self.assertEqual(self.data.disk_runs(set())[0]["status"], "running")

    def test_export_site(self):
        ply = write_ply(self.data.imports / "scan.ply", n=200)
        z = zipfile.ZipFile(io.BytesIO(export_site(ply, "My scan", [1, 0, 0, 0])))
        names = set(z.namelist())
        self.assertTrue({"index.html", "model.ply", "viewer-config.json", "README.txt",
                         "vendor/spark/spark.module.min.js", "vendor/three/three.module.min.js"} <= names)
        self.assertEqual(json.loads(z.read("viewer-config.json")),
                         {"file": "model.ply", "title": "My scan", "rotation": [1, 0, 0, 0]})
        self.assertFalse(any(n.endswith(".map") for n in names))
        self.assertEqual(z.read("model.ply"), ply.read_bytes())
