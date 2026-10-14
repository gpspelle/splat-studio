import io
import os
import subprocess
import sys
import unittest

from tests.helpers import TempDirTestCase
from splat_studio import colmap
from splat_studio.progress import Progress


class PairsTest(unittest.TestCase):
    def test_sequential_pairs(self):
        self.assertEqual(colmap.sequential_pairs(4, 1, False), {(0, 1), (1, 2), (2, 3)})
        self.assertEqual(len(colmap.sequential_pairs(10, 15, False)), 45)   # overlap > n: all pairs
        # quadratic adds jumps of 1, 2, 4, 8...
        self.assertIn((0, 8), colmap.sequential_pairs(20, 4, True))
        self.assertNotIn((0, 8), colmap.sequential_pairs(20, 4, False))

    def test_loop_pairs_join_start_and_end(self):
        pairs = colmap.loop_pairs(100, 5)
        self.assertEqual(len(pairs), 25)
        self.assertIn((0, 99), pairs)
        self.assertTrue(all(a < 5 and b >= 95 for a, b in pairs))

    def test_exhaustive_pairs(self):
        self.assertEqual(len(colmap.exhaustive_pairs(250)), 250 * 249 // 2)


class PlanTest(unittest.TestCase):
    def test_plan_with_step_2_has_two_rounds(self):
        plan = colmap.build_plan(450, 2)
        self.assertEqual([p[0] for p in plan], [2, 2, 2, 1, 1])             # no exhaustive for 450 images
        self.assertEqual(plan[0][1], 225)
        self.assertTrue(plan[1][3])                                         # attempt 2 uses relaxed mapper

    def test_plan_with_step_1_is_one_round(self):
        plan = colmap.build_plan(250, 1)
        self.assertEqual(len(plan), 3)
        self.assertEqual(plan[2][4], ["all"])

    def test_costs_only_count_new_pairs(self):
        plan = colmap.build_plan(250, 1)
        costs = colmap.plan_costs(plan)
        self.assertEqual(costs[0]["extract"], 250)
        self.assertEqual(costs[1]["extract"], 0)                            # same database, no re-extraction
        seq = len(colmap.sequential_pairs(250, colmap.SEQ_OVERLAP, False))
        self.assertEqual(costs[0]["match"], [("match", seq)])
        total_new = sum(u for c in costs for _, u in c["match"])
        self.assertEqual(total_new, len(colmap.exhaustive_pairs(250)))     # every pair matched exactly once
        self.assertEqual(costs[2]["match"][0][0], "match_exhaustive")

    def test_new_round_resets_matches(self):
        costs = colmap.plan_costs(colmap.build_plan(400, 2))
        self.assertEqual(costs[3]["extract"], 400)
        self.assertEqual(costs[3]["match"][0][1], len(colmap.sequential_pairs(400, colmap.SEQ_OVERLAP, False)))

    def test_global_mapper_only_for_the_first_attempt_of_each_round(self):
        costs = colmap.plan_costs(colmap.build_plan(400, 2), use_global=True)
        self.assertEqual([c["map_kind"] for c in costs], ["map_global", "map", "map", "map_global", "map"])
        self.assertEqual({c["map_kind"] for c in colmap.plan_costs(colmap.build_plan(400, 2))}, {"map"})
        p = Progress(1600, rates_file=None, out=io.StringIO())
        fast, _ = colmap.remaining_time(p, colmap.plan_costs(colmap.build_plan(250, 1), True), 0, "extract", 0)
        slow, _ = colmap.remaining_time(p, colmap.plan_costs(colmap.build_plan(250, 1)), 0, "extract", 0)
        self.assertLess(fast, slow)

    def test_remaining_time(self):
        p = Progress(1600, rates_file=None, out=io.StringIO())
        costs = colmap.plan_costs(colmap.build_plan(250, 1))
        ok, worst = colmap.remaining_time(p, costs, 0, "extract", 0)
        self.assertGreater(worst, ok)
        self.assertEqual(colmap.remaining_time(p, costs, 0, "undistort", 0), (0.0, 0.0))
        ok_last, worst_last = colmap.remaining_time(p, costs, 2, "map", 0)
        self.assertAlmostEqual(ok_last, worst_last)                         # nothing after the last attempt


class ModelParsingTest(TempDirTestCase):
    def test_parse_images_txt_handles_empty_point_lines(self):
        f = self.tmp / "images.txt"
        f.write_text("# header\n# more\n1 1 0 0 0 0 0 0 1 a.jpg\n\n2 1 0 0 0 0 0 0 1 b.jpg\n1 2 3\n"
                     "3 1 0 0 0 0 0 0 1 c.jpg\n\n")
        self.assertEqual(colmap.parse_images_txt(f), {"a.jpg", "b.jpg", "c.jpg"})

    def test_find_gaps(self):
        names = [f"f_{i:03d}.jpg" for i in range(10)]
        reg = {names[i] for i in (0, 1, 4, 5, 9)}
        self.assertEqual(colmap.find_gaps(names, reg),
                         [("f_002.jpg", "f_003.jpg", 2), ("f_006.jpg", "f_008.jpg", 3)])
        self.assertEqual(colmap.find_gaps(names, set(names)), [])

    def test_summary_file(self):
        s = colmap.Summary(self.tmp)
        s.add("best", "sparse_try1/0")
        s.add("result", "OK")
        self.assertEqual(self.tmp.joinpath("summary.txt").read_text().splitlines()[1].split(":", 1)[1].strip(), "OK")


class WorkspaceTest(TempDirTestCase):
    def test_new_folder_is_locked_to_this_process(self):
        ws = self.tmp / "ws"
        colmap.claim_workspace(ws)
        self.assertEqual((ws / ".lock").read_text(), str(os.getpid()))

    def test_refuses_folder_used_by_live_process(self):
        ws = self.tmp / "ws"
        ws.mkdir()
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            (ws / ".lock").write_text(str(other.pid))
            with self.assertRaisesRegex(colmap.WorkspaceError, "in use"):
                colmap.claim_workspace(ws)
        finally:
            other.kill()
            other.wait()

    def test_clears_stale_lock_but_protects_old_results(self):
        ws = self.tmp / "ws"
        ws.mkdir()
        (ws / ".lock").write_text("999999")                                 # no such process
        (ws / "summary.txt").write_text("old run")
        with self.assertRaisesRegex(colmap.WorkspaceError, "previous run"):
            colmap.claim_workspace(ws)
        self.assertFalse((ws / ".lock").exists())
        colmap.claim_workspace(ws, overwrite=True)
        self.assertFalse((ws / "summary.txt").exists())

    def test_run_dirs_are_unique_within_a_second(self):
        parent = self.tmp / "workspace" / "vase"
        a = colmap.new_run_dir(parent)
        a.mkdir(parents=True)
        b = colmap.new_run_dir(parent)
        if b.name.startswith(a.name):                   # same second -> numbered suffix
            self.assertEqual(b.name, a.name + "_2")
        self.assertNotEqual(a, b)

    def test_point_latest(self):
        run = self.tmp / "workspace" / "vase" / "run_1"
        run.mkdir(parents=True)
        colmap.point_latest(run)
        self.assertEqual(os.readlink(run.parent / "latest"), "run_1")


if __name__ == "__main__":
    unittest.main()
