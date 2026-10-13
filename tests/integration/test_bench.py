"""`splat-studio bench RUN`: times COLMAP settings and scores Brush over time, on a finished run."""
import json

from tests.helpers import TempDirTestCase, fake_tools_env, make_video, needs_ffmpeg, run_module


@needs_ffmpeg
class BenchTest(TempDirTestCase):
    def setUp(self):
        super().setUp()
        video = make_video(self.tmp / "vase.mov", seconds=4)
        r = run_module("pipeline", [video, "--frames", "24", "--quality", "preview"], cwd=self.tmp, timeout=180)
        self.assertEqual(r.returncode, 0, r.stdout[-2000:] + r.stderr[-2000:])
        self.run_dir = (self.tmp / "workspace" / "vase" / "latest").resolve()
        self.splats_before = sorted(p.name for p in (self.run_dir / "splat").iterdir())

    def bench(self, *args, env=None):
        return run_module("__main__", ["bench", self.run_dir, *args], cwd=self.tmp, env=env, timeout=180)

    def test_colmap_variants_and_brush_curve(self):
        r = self.bench("--quality", "preview", "--evals", "3")
        self.assertEqual(r.returncode, 0, r.stdout[-3000:] + r.stderr[-3000:])
        res = json.loads((self.run_dir / "bench" / "results.json").read_text())
        names = [c["variant"] for c in res["colmap"]]
        self.assertEqual(names, ["current", "smaller features", "smaller + fewer pairs", "global mapper",
                                 "smaller + global"])
        for c in res["colmap"]:
            self.assertNotIn("error", c)
            self.assertEqual(c["registered"], c["images"])
            self.assertAlmostEqual(c["total_s"], c["extract_s"] + c["match_s"] + c["map_s"])
        # the same features are extracted once and reused
        self.assertEqual(res["colmap"][1]["extract_s"], res["colmap"][2]["extract_s"])
        # one Brush run gives the whole curve: quality at regular steps, with the time it took
        (b,) = res["brush"]                         # the frames are small: no lower-resolution run
        steps = [c["step"] for c in b["curve"]]
        self.assertEqual(steps, sorted(steps))
        self.assertEqual(steps[-1], b["steps"])
        self.assertGreaterEqual(len(steps), 3)
        report = (self.run_dir / "bench" / "report.md").read_text()
        self.assertIn("| current |", report)
        self.assertIn("x faster", report)
        self.assertIn("PSNR", report)
        # the run's own splats are untouched; the benchmark's exports live under bench/
        self.assertEqual(sorted(p.name for p in (self.run_dir / "splat").iterdir()), self.splats_before)
        self.assertTrue(list((self.run_dir / "bench" / "brush").rglob("*.ply")))

    def test_older_colmap_skips_the_global_mapper(self):
        r = self.bench("--only", "colmap", env=fake_tools_env(FAKE_COLMAP_GLOBAL="0"))
        self.assertEqual(r.returncode, 0, r.stdout[-3000:] + r.stderr[-3000:])
        self.assertIn("no global_mapper", r.stdout)
        res = json.loads((self.run_dir / "bench" / "results.json").read_text())
        self.assertEqual([c["mapper"] for c in res["colmap"]], ["incremental"] * 3)
        self.assertEqual(res["brush"], [])

    def test_not_a_run_folder(self):
        (self.tmp / "empty").mkdir()
        r = run_module("__main__", ["bench", self.tmp / "empty", "--only", "colmap"], cwd=self.tmp, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("finished run folder", r.stdout + r.stderr)
