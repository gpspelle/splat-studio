"""Coverage: where the cameras were around the object, checked before training."""
import unittest
from pathlib import Path
import tempfile

from tests.helpers import FAKES, TempDirTestCase, needs_ffmpeg  # noqa: F401  (FAKES puts tests/fakes on the path)
from plyfile import write_images_bin
from splat_studio import coverage, guidance, objects


def ring(n=36, keep=lambda k: True):
    with tempfile.TemporaryDirectory() as td:
        ims = objects.read_images_bin(write_images_bin(Path(td) / "images.bin", n=n, radius=3.0, target=(0, -0.25, 0)))
    return [im for k, im in enumerate(ims) if keep(k)]


class MeasureTest(unittest.TestCase):
    def test_all_the_way_round_at_three_heights(self):
        c = coverage.measure(ring())
        self.assertEqual(c["cameras"], 36)
        self.assertLess(c["gap"]["degrees"], 15)
        self.assertGreater(c["height"]["spread"], coverage.HEIGHT_WARN)
        self.assertEqual(coverage.problems(c), [])
        self.assertEqual(len(c["dirs"]), 36)                              # each camera's direction, for the viewer's globe
        self.assertTrue(all(abs(sum(x * x for x in d) - 1) < 0.01 for d in c["dirs"]))

    def test_one_side_missing(self):
        c = coverage.measure(ring(keep=lambda k: not 10 <= k < 22))       # 12 of 36 cameras gone: 130 degrees
        self.assertAlmostEqual(c["gap"]["degrees"], 130, delta=2)
        self.assertEqual(coverage.problems(c), ["gap"])
        self.assertEqual({c["gap"]["first"], c["gap"]["last"]}, {"frame_0010.jpg", "frame_0023.jpg"})

    def test_one_height(self):
        c = coverage.measure(ring(keep=lambda k: k % 3 == 1))
        self.assertEqual(coverage.problems(c), ["height"])

    def test_too_few_cameras(self):
        self.assertIsNone(coverage.measure(ring()[:2]))
        self.assertEqual(coverage.problems(None), [])


def wall(cov, dist, width=4.0, low=-0.5, high=1.5, n=800, spread=None):
    """Points of a flat wall standing `dist` from the object, facing it, in the middle of its gap."""
    import math
    import random
    b, g = cov["basis"], cov["gap"]
    m = math.radians(g["from"] + g["degrees"] / 2)
    t, up, e1, e2 = (b[k] for k in ("target", "up", "e1", "e2"))
    d = [math.cos(m) * e1[i] + math.sin(m) * e2[i] for i in range(3)]
    side = [math.cos(m + math.pi / 2) * e1[i] + math.sin(m + math.pi / 2) * e2[i] for i in range(3)]
    rnd = random.Random(3)
    out = []
    for _ in range(n):
        s, h = rnd.uniform(-width / 2, width / 2), rnd.uniform(low, high)
        out.append(tuple(t[i] + dist * d[i] + s * side[i] + h * up[i] for i in range(3)))
    return out


class BlockedSideTest(unittest.TestCase):
    """#36: a side something stands in front of (a wall) can't be filmed, so it isn't a gap."""

    def gap(self):
        return coverage.measure(ring(keep=lambda k: not 10 <= k < 22))   # 130 degrees nobody filmed

    def test_a_wall_close_by_excuses_the_side(self):
        c = self.gap()
        self.assertAlmostEqual(c["distance"], 3.0, delta=0.1)              # how far people stood
        coverage.excuse_blocked(c, wall(c, 2.2))
        self.assertTrue(c["blocked"])
        self.assertGreater(coverage.blocked_degrees(c), 60)
        self.assertLess(coverage.open_gap(c)["degrees"], coverage.GAP_WARN)
        self.assertEqual(coverage.problems(c), [])
        self.assertAlmostEqual(c["gap"]["degrees"], 130, delta=2)         # the raw gap is kept
        codes = [t["code"] for t in guidance.coverage_advice(c)]
        self.assertEqual(codes, ["run.coverage_blocked"])                 # no warning, and not "all the way round"
        self.assertIn("blocked", coverage.summary_line(c))

    def test_what_does_not_block(self):
        for name, pts in [
            ("floor", [(x / 4, 0.75, z / 4) for x in range(-16, 17) for z in range(-16, 17)]),   # flat, under it
            ("far wall", None),                                           # you could have walked there
            ("low step", "low"),                                          # too short to stand in the way
            ("nothing", [])]:
            c = self.gap()
            if pts is None:
                pts = wall(c, 6.0)
            elif pts == "low":
                pts = wall(c, 2.2, low=-0.9, high=-0.8)
            coverage.excuse_blocked(c, pts)
            self.assertEqual(coverage.problems(c), ["gap"], name)
            self.assertFalse(c.get("blocked"), name)

    def test_a_wall_over_part_of_the_gap_leaves_the_rest(self):
        c = self.gap()
        coverage.excuse_blocked(c, wall(c, 2.2, width=1.0))               # narrow: covers a slice only
        self.assertTrue(c["blocked"])
        self.assertLess(coverage.open_gap(c)["degrees"], c["gap"]["degrees"])
        g = coverage.open_gap(c)
        if g["degrees"] > coverage.GAP_WARN:
            self.assertEqual(coverage.problems(c), ["gap"])
            self.assertIsNone(g["first"])                                 # the open part ends at the wall, not at a frame

    def test_points3d_round_trip(self):
        import struct
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "points3D.bin"
            body = struct.pack("<Q", 2) + struct.pack("<Q3d3BdQ", 1, 1.0, 2.0, 3.0, 9, 9, 9, 0.5, 2) + struct.pack("<4I", 1, 0, 2, 5) \
                + struct.pack("<Q3d3BdQ", 2, -1.0, 0.5, 0.25, 1, 1, 1, 0.1, 0)
            f.write_bytes(body)
            self.assertEqual(coverage.read_points3d(f), [(1.0, 2.0, 3.0), (-1.0, 0.5, 0.25)])
            f.write_bytes(b"")
            self.assertEqual(coverage.read_points3d(f), [])


@needs_ffmpeg
class PerObjectTest(TempDirTestCase):
    """Three balls in a row; the green one is hidden from a third of the cameras."""

    def test_each_object_gets_its_own_ring(self):
        from tests.unit.test_objects import make_three_balls
        from plyfile import THREE_OBJECTS
        _, run, frames, imgs = make_three_balls(self.tmp, hide=lambda oid, k: oid == 3 and 12 <= k < 24)
        (run / "dense" / "sparse" / "0").mkdir(parents=True)
        import shutil
        shutil.copy(run / "sparse" / "0" / "images.bin", run / "dense" / "sparse" / "0" / "images.bin")
        cov = coverage.for_run(run, frames)
        objs = {o["name"]: o for o in cov["objects"]}
        self.assertEqual(sorted(objs), ["Blue ball", "Green ball", "Red ball"])
        for o in objs.values():                                   # each ring is around its own ball
            want = THREE_OBJECTS[o["id"]]
            self.assertLess(sum((a - b) ** 2 for a, b in zip(o["basis"]["target"], want)) ** 0.5, 0.15, o["name"])
            self.assertEqual(o["basis"]["e1"], cov["basis"]["e1"])          # the rings line up with each other
        self.assertEqual(objs["Green ball"]["cameras"], 24)
        self.assertGreater(objs["Green ball"]["gap"]["degrees"], 90)    # seen from its own centre, off to one side
        self.assertEqual(coverage.problems(objs["Green ball"]), ["gap"])
        self.assertEqual(objs["Red ball"]["cameras"], 36)
        tips = guidance.coverage_advice(cov)
        self.assertEqual([(t["code"], t["params"].get("name")) for t in tips], [("run.coverage_obj_gap", "Green ball")])
        # saved for later (the viewer reads it), and measured on demand for older runs
        self.assertIsNone(coverage.load(self.tmp / "nowhere"))
        self.assertEqual(coverage.load(run, frames)["objects"][2]["name"], "Green ball")
        self.assertTrue((run / "coverage.json").exists())


class AdviceTest(unittest.TestCase):
    def test_gap_names_the_moments_in_the_video(self):
        c = coverage.measure(ring(keep=lambda k: not 10 <= k < 22))
        tips = guidance.coverage_advice(c, fps=2.0)
        self.assertEqual([t["code"] for t in tips], ["run.coverage_gap_at"])
        self.assertEqual(tips[0]["params"]["when"], "0:04 and 0:11")      # frames 10 and 23 at 2 fps
        self.assertEqual(tips[0]["params"]["deg"], "130")
        self.assertEqual(guidance.coverage_advice(c)[0]["code"], "run.coverage_gap")   # no timing known

    def test_good_coverage_and_run_advice(self):
        self.assertEqual([t["code"] for t in guidance.coverage_advice(coverage.measure(ring()))], ["run.coverage_ok"])
        flat = guidance.coverage_advice(coverage.measure(ring(keep=lambda k: k % 3 == 1)))
        job = {"status": "done", "coverage_advice": flat + [{"level": "ok", "code": "x", "params": {}, "text": ""}]}
        self.assertEqual([t["code"] for t in guidance.run_advice(job)], ["run.coverage_height"])   # warnings only


if __name__ == "__main__":
    unittest.main()
