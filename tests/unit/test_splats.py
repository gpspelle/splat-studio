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
