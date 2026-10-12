"""Library pictures of the chosen object(s) instead of the video's first frame."""
import json
import subprocess
import time
import unittest
from pathlib import Path

from tests.helpers import TempDirTestCase, needs_ffmpeg, write_ply
from splat_studio import masks, thumbs
from splat_studio.server.library import DataDir


def disc_masks(d: Path, n: int, centre, w=320, h=200, r0=20):
    for i in range(n):
        cx, cy = centre(i)
        r = r0 + i
        masks.write_png_gray(d / f"frame_{i:05d}.png", w, h,
                             [bytes(255 if (x - cx) ** 2 + (y - cy) ** 2 <= r * r else 0 for x in range(w)) for y in range(h)])


def frames(d: Path, n: int, w=320, h=200):
    d.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s={w}x{h}",
                        "-frames:v", "1", str(d / f"frame_{i:05d}.jpg")], check=True)


class CropTest(unittest.TestCase):
    def test_box_gets_room_and_the_tile_shape(self):
        x, y, w, h = thumbs.crop_box((0.4, 0.4, 0.6, 0.6), 1000, 1000)
        self.assertAlmostEqual(w / h, 16 / 10, delta=0.02)
        self.assertLessEqual(x, 400)
        self.assertGreaterEqual(x + w, 600)
        self.assertLessEqual(y, 400)
        self.assertGreaterEqual(y + h, 600)

    def test_stays_inside_the_frame(self):
        x, y, w, h = thumbs.crop_box((0.9, 0.0, 1.0, 0.95), 640, 480)
        self.assertTrue(0 <= x and x + w <= 640 and 0 <= y and y + h <= 480)


@needs_ffmpeg
class PictureTest(TempDirTestCase):
    def test_largest_view_fully_in_frame(self):
        d = self.tmp / "masks"
        # grows with i, but runs off the right edge in the last two frames
        disc_masks(d, 10, lambda i: (100 + 25 * i, 100))
        m, box = thumbs.best_frame(d)
        self.assertEqual(m.name, "frame_00007.png")                 # biggest one still inside
        self.assertTrue(0 < box[0] < box[2] < 1)

    def test_picture_of_the_object(self):
        f = self.tmp / "frames" / "vase"
        frames(f, 6)
        disc_masks(f / "masks", 6, lambda i: (220, 100))
        out = thumbs.make(f, f / "masks", self.tmp / "run" / "thumbs" / "run.jpg")
        w, h = masks.jpeg_size(out)
        self.assertEqual(w, 480)
        self.assertAlmostEqual(w / h, 16 / 10, delta=0.05)
        self.assertIsNone(thumbs.make(f, self.tmp / "nothing", self.tmp / "x.jpg"))

    def test_part_masks_by_name(self):
        f = self.tmp / "frames" / "table"
        for i in (1, 2):
            (f / "masks" / "objects" / str(i)).mkdir(parents=True)
        (f / "masks" / "objects.json").write_text(json.dumps([{"id": 1, "name": "Blue mug"}, {"id": 2, "name": "Vase"}]))
        self.assertEqual(thumbs.part_mask_dir(f, Path("standard_3000_part-vase.ply")).name, "2")
        self.assertEqual(thumbs.part_mask_dir(f, Path("standard_3000_part-blue-mug.ply")).name, "1")
        self.assertIsNone(thumbs.part_mask_dir(f, Path("standard_3000_part-lamp.ply")))
        self.assertIsNone(thumbs.part_mask_dir(f, Path("standard_3000.ply")))


@needs_ffmpeg
class LibraryPictureTest(TempDirTestCase):
    def test_runs_trained_on_a_selection_get_object_pictures(self):
        data = DataDir(self.tmp / "data")
        f = data.frames / "table"
        frames(f, 6)
        disc_masks(f / "masks", 6, lambda i: (160, 100))
        disc_masks(f / "masks" / "objects" / "1", 6, lambda i: (80, 100))
        disc_masks(f / "masks" / "objects" / "2", 6, lambda i: (240, 100))
        (f / "masks" / "objects.json").write_text(json.dumps([{"id": 1, "name": "Mug"}, {"id": 2, "name": "Vase"}]))
        rd = data.workspace / "table" / "run_1"
        (rd / "dense").mkdir(parents=True)
        (rd / "dense" / "masks.json").write_text('{"mode": "transparent", "count": 6}')
        for n in ("standard_3000.ply", "standard_3000_part-mug.ply", "standard_3000_part-vase.ply"):
            write_ply(rd / "splat" / n, n=200)
        first = {s["name"]: s["thumb"] for s in data.list_splats()}
        self.assertTrue(all(t.endswith("frame_00000.jpg") for t in first.values()))   # meanwhile: the first frame
        end = time.time() + 30
        while time.time() < end and not (rd / "thumbs" / "standard_3000_part-vase.jpg").exists():
            time.sleep(0.2)
        got = {s["name"]: s["thumb"] for s in data.list_splats()}
        self.assertEqual(got["standard_3000.ply"], "workspace/table/run_1/thumbs/run.jpg")
        self.assertEqual(got["standard_3000_part-mug.ply"], "workspace/table/run_1/thumbs/standard_3000_part-mug.jpg")
        self.assertEqual(got["standard_3000_part-vase.ply"], "workspace/table/run_1/thumbs/standard_3000_part-vase.jpg")
        self.assertEqual(data.thumbnail(run_dir="workspace/table/run_1"), "workspace/table/run_1/thumbs/run.jpg")

    def test_runs_without_a_selection_keep_the_first_frame(self):
        data = DataDir(self.tmp / "data")
        frames(data.frames / "cup", 2)
        rd = data.workspace / "cup" / "run_1"
        write_ply(rd / "splat" / "standard_3000.ply", n=200)
        (rd / "dense").mkdir()
        self.assertTrue(data.list_splats()[0]["thumb"].endswith("frames/cup/frame_00000.jpg"))
        time.sleep(0.3)
        self.assertFalse((rd / "thumbs").exists())


if __name__ == "__main__":
    unittest.main()
