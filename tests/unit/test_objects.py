"""One splat per object: COLMAP model reading, projection, and the multi-view vote."""
import math
import unittest
from pathlib import Path

from tests.helpers import TempDirTestCase, needs_ffmpeg
from plyfile import THREE_OBJECTS, write_cameras_bin, write_images_bin, write_three_objects  # tests/fakes
from splat_studio import masks, objects
from splat_studio.splats import PlyFile


class ProjectionTest(unittest.TestCase):
    IDENT = {"R": [[1, 0, 0], [0, 1, 0], [0, 0, 1]], "t": (0, 0, 0)}

    def test_pinhole(self):
        cam = {"model": "PINHOLE", "params": (100.0, 200.0, 50.0, 40.0), "width": 100, "height": 80}
        self.assertEqual(objects.project(cam, self.IDENT, (0, 0, 2)), (50.0, 40.0))
        self.assertEqual(objects.project(cam, self.IDENT, (1, 0.5, 2)), (100.0, 90.0))
        self.assertIsNone(objects.project(cam, self.IDENT, (0, 0, -1)))       # behind the camera

    def test_opencv_distortion(self):
        cam = {"model": "OPENCV", "params": (100.0, 100.0, 0.0, 0.0, 0.1, 0.0, 0.0, 0.0), "width": 1, "height": 1}
        u, v = objects.project(cam, self.IDENT, (1, 0, 1))       # r^2 = 1: x' = x (1 + k1)
        self.assertAlmostEqual(u, 110.0)
        self.assertAlmostEqual(v, 0.0)
        cam = {"model": "SIMPLE_RADIAL", "params": (100.0, 0.0, 0.0, -0.1), "width": 1, "height": 1}
        self.assertAlmostEqual(objects.project(cam, self.IDENT, (0, 1, 1))[1], 90.0)

    def test_model_files_round_trip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            cams = objects.read_cameras_bin(write_cameras_bin(Path(td) / "cameras.bin"))
            self.assertEqual(cams[1]["model"], "OPENCV")
            self.assertEqual((cams[1]["width"], cams[1]["height"]), (320, 240))
            imgs = objects.read_images_bin(write_images_bin(Path(td) / "images.bin", n=12))
            self.assertEqual(len(imgs), 12)
            self.assertEqual(imgs[0]["name"], "frame_0001.jpg")

    def test_part_names(self):
        taken = set()
        p = Path("splat/standard_30000.ply")
        self.assertEqual(objects.part_path(p, "Blue Mug!", taken).name, "standard_30000_part-blue-mug.ply")
        self.assertEqual(objects.part_path(p, "blue mug", taken).name, "standard_30000_part-blue-mug-2.ply")
        self.assertEqual(objects.part_path(p, "???", taken).name, "standard_30000_part-object.ply")


@needs_ffmpeg
class SplitTest(TempDirTestCase):
    """Three balls on a table, seen by 36 cameras; masks drawn from the true positions."""

    def make_scene(self, occluded_share=0.0):
        run, frames = self.tmp / "workspace" / "balls" / "run_1", self.tmp / "frames" / "balls"
        model = run / "sparse" / "0"
        cams = objects.read_cameras_bin(write_cameras_bin(model / "cameras.bin"))
        imgs = objects.read_images_bin(write_images_bin(model / "images.bin", n=36, radius=3.0, target=(0, -0.25, 0)))
        (run / "dense").mkdir(parents=True)
        (run / "dense" / "source_model.txt").write_text(str(model))
        cam = cams[1]
        for img in imgs:
            for oid, c in THREE_OBJECTS.items():
                uv = objects.project(cam, img, c)
                edge = objects.project(cam, img, (c[0], c[1] - 0.22, c[2]))
                r = math.dist(uv, edge) * 1.15 if uv and edge else 0
                w, h = cam["width"], cam["height"]
                rows = [bytes(255 if uv and (x - uv[0]) ** 2 + (y - uv[1]) ** 2 <= r * r else 0 for x in range(w))
                        for y in range(h)]
                masks.write_png_gray(frames / "masks" / "objects" / str(oid) / img["name"].replace(".jpg", ".png"),
                                     w, h, rows)
        (frames / "masks" / "objects.json").write_text('[{"id": 1, "name": "Red ball"}, {"id": 2, "name": "Blue ball"},'
                                                        ' {"id": 3, "name": "Green ball"}]')
        ply = write_three_objects(run / "splat" / "standard_30000.ply")
        return ply, run, frames

    def test_each_ball_gets_its_own_splat(self):
        ply, run, frames = self.make_scene()
        res = objects.split(ply, run, frames, log=lambda *_: None)
        self.assertEqual([r["name"] for r in res], ["Red ball", "Blue ball", "Green ball"])
        for r, (oid, c) in zip(res, THREE_OBJECTS.items()):
            out = PlyFile(Path(r["output"]))
            d = out.read(["x", "y", "z"])
            self.assertGreater(out.count, 1500 * 0.9, r["name"])            # nearly all of the ball
            near = sum(1 for p in zip(d["x"], d["y"], d["z"]) if math.dist(p, c) < 0.3)
            self.assertEqual(near, out.count, r["name"])                    # and nothing else
        self.assertTrue(Path(res[0]["output"]).name.endswith("_part-red-ball.ply"))
        self.assertTrue(ply.exists())

    def test_needs_two_objects(self):
        ply, run, frames = self.make_scene()
        import shutil
        shutil.rmtree(frames / "masks" / "objects" / "2")
        shutil.rmtree(frames / "masks" / "objects" / "3")
        with self.assertRaisesRegex(ValueError, "fewer than two"):
            objects.split(ply, run, frames, log=lambda *_: None)


if __name__ == "__main__":
    unittest.main()
