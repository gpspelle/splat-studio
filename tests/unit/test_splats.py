import contextlib
import hashlib
import io
import math
import struct

from tests.helpers import TempDirTestCase  # also puts tests/fakes on sys.path
from plyfile import write_foggy_scene, write_images_bin, write_ply
from splat_studio import splats


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


class PlyReadingTest(TempDirTestCase):
    def test_header_and_fields(self):
        p = write_ply(self.tmp / "v.ply", n=500)
        ply = splats.PlyFile(p)
        self.assertEqual(ply.count, 540)                       # 500 + 40 floaters
        self.assertEqual(ply.stride, 62 * 4)
        self.assertEqual(ply.up_axis(), (0.0, -1.0, 0.0))
        f = ply.read(["x", "opacity"])
        self.assertEqual(len(f["x"]), 540)
        self.assertAlmostEqual(f["opacity"][0], 4.0)

    def test_up_axis_letter_and_missing(self):
        self.assertEqual(splats.PlyFile(write_ply(self.tmp / "a.ply", n=50, up_axis="y")).up_axis(), (0.0, 1.0, 0.0))
        self.assertIsNone(splats.PlyFile(write_ply(self.tmp / "b.ply", n=50, up_axis=None)).up_axis())

    def test_rejects_bad_files(self):
        (self.tmp / "x.ply").write_bytes(b"not a ply")
        with self.assertRaises(splats.PlyError):
            splats.PlyFile(self.tmp / "x.ply")
        (self.tmp / "c.ply").write_bytes(b"ply\nformat binary_little_endian 1.0\nelement chunk 1\nproperty float a\n"
                                         b"element vertex 1\nproperty float x\nend_header\n" + b"\0" * 8)
        with self.assertRaisesRegex(splats.PlyError, "more than one element"):
            splats.PlyFile(self.tmp / "c.ply")
        good = write_ply(self.tmp / "t.ply", n=50)
        (self.tmp / "trunc.ply").write_bytes(good.read_bytes()[:-100])
        with self.assertRaisesRegex(splats.PlyError, "truncated"):
            splats.PlyFile(self.tmp / "trunc.ply")


class CamerasTest(TempDirTestCase):
    def test_reads_colmap_images_and_finds_what_they_film(self):
        run = self.tmp / "run"
        write_images_bin(run / "dense" / "sparse" / "0" / "images.bin", n=24, radius=2.0, target=(0.5, -0.6, 0.2))
        cams = splats.find_cameras(run)
        self.assertEqual(len(cams), 24)
        for c in cams:
            self.assertAlmostEqual(math.dist(c["center"], (0.5, -0.6, 0.2)), 2.0, delta=0.6)
        target = splats.look_at_point(cams)
        self.assertLess(math.dist(target, (0.5, -0.6, 0.2)), 0.05)

    def test_missing_or_empty_camera_files(self):
        self.assertEqual(splats.find_cameras(self.tmp), [])
        (self.tmp / "dense" / "sparse" / "0").mkdir(parents=True)
        (self.tmp / "dense" / "sparse" / "0" / "images.bin").write_bytes(b"")
        self.assertEqual(splats.find_cameras(self.tmp), [])


class DiagnosisTest(TempDirTestCase):
    def test_foggy_scene_is_diagnosed(self):
        a = splats.analyze(write_foggy_scene(self.tmp / "fog.ply"))
        self.assertGreater(a["fog_share"], 0.3)
        text = " ".join(splats.diagnosis(a))
        self.assertIn("background fog", text)
        self.assertIn("camera starts inside the background cloud", text)

    def test_clean_object_has_no_fog_diagnosis(self):
        a = splats.analyze(write_ply(self.tmp / "v.ply", n=3000))
        self.assertLess(a["fog_share"], 0.05)
        self.assertIn("No obvious fog problem", " ".join(splats.diagnosis(a)))

    def test_report_mentions_cameras_when_given(self):
        run = self.tmp / "run"
        write_images_bin(run / "dense" / "sparse" / "0" / "images.bin")
        report = splats.format_report(splats.analyze(write_foggy_scene(self.tmp / "fog.ply"), run))
        self.assertIn("from cameras, 36 cameras", report)


class CleanTest(TempDirTestCase):
    def counts(self, path):
        d = splats.load(splats.PlyFile(path))
        obj = sum(1 for o, s in zip(d["opacity"], d["size"]) if o > 0.9 and s < 0.01)
        return obj, len(d["opacity"]) - obj

    def test_removes_fog_keeps_object_and_never_touches_original(self):
        src = write_foggy_scene(self.tmp / "fog.ply", n_object=4000, n_fog=3000)
        before = sha(src)
        s = splats.clean(src)
        self.assertEqual(sha(src), before)
        self.assertEqual(s["output"], str(self.tmp / "fog_clean.ply"))
        obj, fog = self.counts(s["output"])
        self.assertEqual(obj, 4000)
        self.assertLess(fog, 30)
        out = splats.PlyFile(s["output"])
        self.assertEqual(out.count, s["kept"])
        self.assertEqual(out.up_axis(), (0.0, -1.0, 0.0))            # header comments kept
        self.assertTrue(any("Cleaned by Splat Studio" in c for c in out.comments))

    def test_uses_cameras_when_a_run_is_given(self):
        src = write_foggy_scene(self.tmp / "fog.ply", n_object=4000, n_fog=3000)
        run = self.tmp / "run"
        write_images_bin(run / "dense" / "sparse" / "0" / "images.bin")
        s = splats.clean(src, self.tmp / "out.ply", run_dir=run)
        self.assertEqual(s["region"]["source"], "cameras")
        obj, fog = self.counts(s["output"])
        self.assertEqual(obj, 4000)
        self.assertLess(fog, 0.1 * 3000)

    def test_radius_scale_and_refuses_to_overwrite(self):
        src = write_foggy_scene(self.tmp / "fog.ply", n_object=2000, n_fog=2000)
        small = splats.clean(src, self.tmp / "s.ply", radius_scale=0.3)
        large = splats.clean(src, self.tmp / "l.ply", radius_scale=20)
        self.assertLess(small["kept"], large["kept"])
        with self.assertRaises(ValueError):
            splats.clean(src, src)

    def test_command_line(self):
        src = write_foggy_scene(self.tmp / "fog.ply", n_object=1000, n_fog=1000)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            splats.main_inspect([str(src)])
            splats.main_clean([str(src)])
        text = out.getvalue()
        self.assertIn("Diagnosis:", text)
        self.assertRegex(text, r"Kept [\d,]+ of 2,000 splats")
        self.assertIn("Clean copy: ", text)
        with self.assertRaises(SystemExit):
            splats.main_inspect([str(self.tmp / "missing.ply")])


def kinds(path):
    """Count splats per part of write_room_scene / write_ply, told apart by their size."""
    d = splats.load(splats.PlyFile(path))
    out = {"object": 0, "table": 0, "walls": 0, "fog": 0}
    for s in d["size"]:
        out["object" if s < 0.012 else "table" if s < 0.025 else "walls" if s < 0.05 else "fog"] += 1
    return out


class RoomSceneTest(TempDirTestCase):
    """A vase on a table with opaque walls and fog around it: what a real indoor capture looks like."""

    def setUp(self):
        super().setUp()
        from plyfile import write_room_scene
        self.src = write_room_scene(self.tmp / "room.ply")
        self.run = self.tmp / "run"
        write_images_bin(self.run / "dense" / "sparse" / "0" / "images.bin", n=36, radius=1.4, target=(0.0, -0.55, 0.0))

    def test_clean_removes_walls_and_fog_even_without_cameras(self):
        for run in (None, self.run):
            k = kinds(splats.clean(self.src, self.tmp / f"c{bool(run)}.ply", run_dir=run)["output"])
            self.assertEqual((k["object"], k["table"]), (6000, 5000), run)     # object and its table kept
            self.assertEqual(k["walls"], 0, run)
            self.assertLess(k["fog"], 0.1 * 3000, run)

    def test_isolate_keeps_only_the_object(self):
        for run in (None, self.run):
            s = splats.isolate(self.src, self.tmp / f"o{bool(run)}.ply", run_dir=run)
            k = kinds(s["output"])
            self.assertTrue(s["plane"], run)
            self.assertEqual((k["table"], k["walls"], k["fog"]), (0, 0, 0), run)
            self.assertGreater(k["object"], 0.93 * 6000, run)                  # a thin band at the base may go


class IsolateTest(TempDirTestCase):
    def test_vase_on_disc(self):
        src = write_ply(self.tmp / "vase.ply", n=20000)                         # 14000 vase, 6000 disc, 40 floaters
        before = sha(src)
        s = splats.isolate(src)
        self.assertEqual(sha(src), before)                                      # original untouched
        self.assertEqual(s["output"], str(self.tmp / "vase_object.ply"))
        k = kinds(s["output"])
        self.assertEqual((k["table"], k["walls"] + k["fog"]), (0, 0))
        self.assertGreater(k["object"], 0.95 * 14000)
        out = splats.PlyFile(s["output"])
        self.assertEqual(out.up_axis(), (0.0, -1.0, 0.0))
        self.assertTrue(any("Background removed" in c for c in out.comments))

    def test_object_without_a_surface(self):
        src = write_foggy_scene(self.tmp / "float.ply", n_object=3000, n_fog=2000)   # nothing to stand on
        s = splats.isolate(src)
        self.assertFalse(s["plane"])
        k = kinds(s["output"])
        self.assertGreater(k["object"], 0.95 * 3000)
        self.assertLess(k["fog"], 50)

    def test_refuses_overwrite_and_cli(self):
        src = write_ply(self.tmp / "v.ply", n=3000)
        with self.assertRaises(ValueError):
            splats.isolate(src, src)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            splats.main_isolate([str(src)])
        self.assertIn("support surface removed", out.getvalue())
        self.assertIn("Object copy: ", out.getvalue())

    def test_support_plane_detection(self):
        import random
        rnd = random.Random(1)
        floor = [(rnd.uniform(-1, 1), 0.0 + rnd.uniform(-0.002, 0.002), rnd.uniform(-1, 1)) for _ in range(800)]
        blob = [(rnd.gauss(0, 0.1), rnd.uniform(0.05, 0.5), rnd.gauss(0, 0.1)) for _ in range(400)]
        n, d, share = splats.find_support_plane(floor + blob, eps=0.01, up=(0, 1, 0))
        self.assertGreater(abs(n[1]), 0.99)
        self.assertAlmostEqual(d * (1 if n[1] > 0 else -1), 0.0, places=2)
        self.assertGreater(share, 0.5)
        self.assertIsNone(splats.find_support_plane(blob, eps=0.01, up=(0, 1, 0)))   # no surface in a blob
