"""iPhone captures (capture.py): reading packages, ARKit to COLMAP cameras, turning frames upright."""
import json
import math
import zipfile
from pathlib import Path

from splat_studio import capture
from splat_studio.coverage import read_points3d
from splat_studio.objects import qvec_to_rotmat, read_cameras_bin, read_images_bin
from tests.helpers import TempDirTestCase, arkit_pose, gray_level, make_capture, needs_ffmpeg

X = (0.1, 0.05, -0.08)          # a world point near the object


def arkit_pixel(pose16, K, p):
    """Where ARKit's camera sees world point p in the sensor image (camera looks along -z, y up)."""
    m = [pose16[0:4], pose16[4:8], pose16[8:12]]
    d = [p[k] - m[k][3] for k in range(3)]
    x, y, z = (sum(m[k][a] * d[k] for k in range(3)) for a in range(3))      # R^T d
    return K[2] + K[0] * x / -z, K[3] - K[1] * y / -z


def model_pixel(img, cam, p):
    R, t = img["R"], img["t"]
    c = [sum(R[a][k] * p[k] for k in range(3)) + t[a] for a in range(3)]
    fx, fy, cx, cy = cam["params"]
    return fx * c[0] / c[2] + cx, fy * c[1] / c[2] + cy


class GeometryTest(TempDirTestCase):
    def test_how_the_phone_was_held_decides_the_turn(self):
        centre = (1.5, 0.2, 0.0)
        for held, turns in [("portrait", 1), ("landscape", 0), ("upside-down", 2)]:
            R, _ = capture.colmap_camera(arkit_pose(centre, held=held))
            self.assertEqual(capture.upright_turns([R]), turns, held)

    def test_quarter_turn_keeps_every_point_where_the_turned_image_shows_it(self):
        K, w, h = (500.0, 490.0, 330.0, 250.0), 640, 480
        pose = arkit_pose((1.2, 0.4, 0.9), held="portrait")
        u, v = arkit_pixel(pose, K, X)
        R, C = capture.colmap_camera(pose)
        for _ in range(4):
            u, v = h - v, u                                   # where the turned image shows it
            R, K, w, h = capture.turn_cw(R, K, w, h)
            Rw = capture.transpose(R)
            t = [-sum(Rw[r][k] * C[k] for k in range(3)) for r in range(3)]
            pu, pv = model_pixel({"R": Rw, "t": t}, {"params": K}, X)
            self.assertAlmostEqual(pu, u, places=6)
            self.assertAlmostEqual(pv, v, places=6)
        self.assertEqual((w, h), (640, 480))

    def test_quaternion_round_trip(self):
        for held in ("portrait", "landscape", "upside-down"):
            for c in [(1.5, 0.2, 0.0), (-0.3, 1.0, -1.4), (0.0, -0.5, 1.5)]:
                R, _ = capture.colmap_camera(arkit_pose(c, held=held))
                Rw = capture.transpose(R)
                q = capture.quaternion(Rw)
                self.assertGreaterEqual(q[0], 0)
                back = qvec_to_rotmat(q)
                for a in range(3):
                    for b in range(3):
                        self.assertAlmostEqual(back[a][b], Rw[a][b], places=9)


class ChooseFramesTest(TempDirTestCase):
    def rows(self, n, limited=()):
        return [{"tracking": "limited-excessiveMotion" if i in limited else "normal"} for i in range(n)]

    def test_leaves_out_limited_tracking_and_spreads_evenly(self):
        picked, left_out = capture.choose_frames(self.rows(100, limited=range(10)), 30)
        self.assertEqual(left_out, 10)
        self.assertEqual(len(picked), 30)
        self.assertTrue(all(i >= 10 for i in picked))
        gaps = {b - a for a, b in zip(picked, picked[1:])}
        self.assertLessEqual(max(gaps) - min(gaps), 1)

    def test_fewer_frames_than_asked_keeps_them_all(self):
        self.assertEqual(capture.choose_frames(self.rows(12, limited=[0]), 50), (list(range(1, 12)), 1))

    def test_mostly_limited_uses_every_frame(self):
        picked, left_out = capture.choose_frames(self.rows(40, limited=range(35)), 40)
        self.assertEqual((len(picked), left_out), (40, 0))


class ModelTest(TempDirTestCase):
    def test_binary_model_reads_back(self):
        R, C = capture.colmap_camera(arkit_pose((1.5, 0.2, 0.0)))
        Rw = capture.transpose(R)
        t = [-sum(Rw[r][k] * C[k] for k in range(3)) for r in range(3)]
        cam = {"id": 3, "width": 192, "height": 256, "params": (200.0, 201.0, 96.0, 128.0)}
        capture.write_model(self.tmp / "m", cam, [{"id": 7, "name": "a_0001.jpg", "R": Rw, "t": t, "camera": 3}],
                            [{"x": 0.1, "y": 0.2, "z": 0.3, "r": 200, "g": 100, "b": 50}])
        cams = read_cameras_bin(self.tmp / "m" / "cameras.bin")
        self.assertEqual(cams[3]["model"], "PINHOLE")
        self.assertEqual((cams[3]["width"], cams[3]["height"]), (192, 256))
        self.assertEqual(cams[3]["params"], (200.0, 201.0, 96.0, 128.0))
        (img,) = read_images_bin(self.tmp / "m" / "images.bin")
        self.assertEqual((img["name"], img["camera"]), ("a_0001.jpg", 3))
        for a in range(3):
            self.assertAlmostEqual(img["t"][a], t[a], places=9)
        (p,) = read_points3d(self.tmp / "m" / "points3D.bin")
        self.assertAlmostEqual(p[0], 0.1)
        self.assertEqual(capture.model_counts(self.tmp / "m"), (1, 1))
        self.assertEqual(capture.model_counts(self.tmp / "nothing"), (0, 0))

    def test_too_few_points_get_a_ball_to_start_from(self):
        centres = [[1.5 * math.cos(a), 0.0, 1.5 * math.sin(a)] for a in (0, math.pi / 2, math.pi, 3 * math.pi / 2)]
        pts = capture.fill_points([{"x": 0, "y": 0, "z": 0, "r": 1, "g": 2, "b": 3}], centres)
        self.assertEqual(len(pts), capture.MIN_POINTS)
        self.assertTrue(all(math.dist((p["x"], p["y"], p["z"]), (0, 0, 0)) <= 0.76 for p in pts))
        self.assertEqual(pts, capture.fill_points([{"x": 0, "y": 0, "z": 0, "r": 1, "g": 2, "b": 3}], centres))
        many = [{"x": i, "y": 0, "z": 0, "r": 0, "g": 0, "b": 0} for i in range(capture.MIN_POINTS)]
        self.assertIs(capture.fill_points(many, centres), many)


class PackageTest(TempDirTestCase):
    def write(self, meta=None, rows=None, video=True):
        root = self.tmp / "pkg"
        root.mkdir(exist_ok=True)
        (root / "meta.json").write_text(json.dumps(meta or {"format": "splat-capture", "version": 1,
                                                            "width": 4, "height": 4}))
        (root / "frames.jsonl").write_text("\n".join(json.dumps(r) for r in (rows if rows is not None else [
            {"i": 0, "t": 0, "pose": [0] * 16, "K": [1, 1, 2, 2], "tracking": "normal"}])) + "\n")
        if video:
            (root / "video.mov").write_bytes(b"x")
        return root

    def test_reads_a_package_folder(self):
        root = self.write()
        cap = capture.load(capture.unpack(self.tmp, self.tmp / "u"))
        self.assertEqual(cap["root"], root)
        self.assertEqual(len(cap["rows"]), 1)
        self.assertEqual(cap["points"], [])
        self.assertTrue(capture.is_capture(self.tmp))
        self.assertFalse(capture.is_capture(self.tmp / "u"))

    def test_refuses_other_formats_newer_versions_and_bad_lines(self):
        self.write({"format": "something-else"})
        with self.assertRaisesRegex(capture.CaptureError, "isn't a Splat Capture package"):
            capture.load(self.tmp / "pkg")
        self.write({"format": "splat-capture", "version": 2})
        with self.assertRaisesRegex(capture.CaptureError, "newer Splat Capture"):
            capture.load(self.tmp / "pkg")
        self.write(rows=[{"i": 0, "pose": [0] * 15, "K": [1, 1, 1, 1]}])
        with self.assertRaisesRegex(capture.CaptureError, "line 1"):
            capture.load(self.tmp / "pkg")
        self.write(video=False)
        (self.tmp / "pkg" / "video.mov").unlink(missing_ok=True)
        with self.assertRaisesRegex(capture.CaptureError, "no video.mov"):
            capture.load(self.tmp / "pkg")

    def test_zip_is_unpacked_once_and_never_outside_its_folder(self):
        root = self.write()
        z = self.tmp / "cap.zip"
        with zipfile.ZipFile(z, "w") as zf:
            for p in root.iterdir():
                zf.write(p, f"cap/{p.name}")
        self.assertTrue(capture.is_capture(z))
        out = capture.unpack(z, self.tmp / "captures")
        self.assertEqual(out, self.tmp / "captures" / "cap" / "cap")
        (out / "marker").write_text("kept")
        self.assertEqual(capture.unpack(z, self.tmp / "captures"), out)
        self.assertTrue((out / "marker").exists())                       # reused, not unpacked again
        bad = self.tmp / "bad.zip"
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr("frames.jsonl", "{}")
            zf.writestr("../evil.txt", "x")
        with self.assertRaisesRegex(capture.CaptureError, "unsafe"):
            capture.unpack(bad, self.tmp / "captures")
        self.assertFalse((self.tmp / "evil.txt").exists())
        other = self.tmp / "other.zip"
        with zipfile.ZipFile(other, "w") as zf:
            zf.writestr("readme.txt", "x")
        self.assertFalse(capture.is_capture(other))
        self.assertFalse(capture.is_capture(self.tmp / "pkg" / "video.mov"))


@needs_ffmpeg
class PrepareTest(TempDirTestCase):
    def test_frames_are_the_right_ones_upright_with_matching_cameras(self):
        z = make_capture(self.tmp, frames=60, held="portrait", limited=4)
        frames = self.tmp / "frames" / "capture_test"
        plan = capture.prepare(z, frames, 20, 256, self.tmp / "captures")
        self.assertFalse(plan["reused"])
        self.assertEqual((plan["turns"], plan["left_out"], len(plan["frames"])), (1, 4, 20))
        self.assertEqual((plan["camera"]["width"], plan["camera"]["height"]), (192, 256))   # upright
        rows = [json.loads(l) for l in (self.tmp / "captures/capture_test/capture_test/frames.jsonl").open()]
        for f in plan["frames"]:
            self.assertGreaterEqual(f["row"], 4)                              # limited tracking left out
            want = round((20 + 3 * f["row"] - 16) * 255 / 219)                # video range -> JPEG range
            self.assertLessEqual(abs(gray_level(frames / f["name"]) - want), 2, f["name"])
        # every camera sees a world point where the turned, scaled image shows it
        for f, img in zip(plan["frames"], capture.images_of(plan)):
            u, v = arkit_pixel(rows[f["row"]]["pose"], rows[f["row"]]["K"], X)
            u, v = 192 - v * 0.8, u * 0.8                                     # scaled to 256x192, turned
            pu, pv = model_pixel(img, plan["camera"], X)
            self.assertAlmostEqual(pu, u, places=4)
            self.assertAlmostEqual(pv, v, places=4)
        again = capture.prepare(z, frames, 20, 256, self.tmp / "captures")
        self.assertTrue(again["reused"])
        self.assertEqual(again["frames"], plan["frames"])

    def test_arkit_run_folder(self):
        z = make_capture(self.tmp, frames=30, held="landscape", limited=0, points=50)
        frames = self.tmp / "frames" / "capture_test"
        plan = capture.prepare(z, frames, 10, 320, self.tmp / "captures")
        ws = self.tmp / "run"
        made = capture.write_arkit_run(plan, frames, ws)
        self.assertEqual(made, {"images": 10, "points": capture.MIN_POINTS, "arkit_points": 50})
        self.assertEqual(sorted(p.name for p in (ws / "dense" / "images").iterdir()),
                         sorted(f["name"] for f in plan["frames"]))
        self.assertEqual(capture.model_counts(ws / "dense" / "sparse" / "0"), (10, capture.MIN_POINTS))
        self.assertEqual(Path((ws / "dense" / "source_model.txt").read_text()), (ws / "sparse" / "0").resolve())
        self.assertEqual([c["model"] for c in read_cameras_bin(ws / "sparse/0/cameras.bin").values()], ["PINHOLE"])
