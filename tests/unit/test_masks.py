"""Choose what to keep: prompts, the test backend, masks for every frame and for training."""
import json
import struct
import sys
import types
import unittest
import zlib
from pathlib import Path
from unittest import mock

from tests.helpers import TempDirTestCase, needs_ffmpeg
from splat_studio import masks


def read_png_gray(path: Path):
    """(w, h, rows) of an 8-bit grayscale PNG written by write_png_gray."""
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = struct.unpack(">II", data[16:24])
    i, idat = 8, b""
    while i < len(data):
        n = struct.unpack(">I", data[i:i + 4])[0]
        if data[i + 4:i + 8] == b"IDAT":
            idat += data[i + 8:i + 8 + n]
        i += 12 + n
    raw = zlib.decompress(idat)
    return w, h, [raw[r * (w + 1) + 1:(r + 1) * (w + 1)] for r in range(h)]


def tiny_jpeg(path: Path, w: int, h: int) -> Path:
    """Just enough of a JPEG for jpeg_size: SOI, an APP0 segment and a baseline SOF0 header."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, h, w, 1) + b"\x01\x11\x00"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xd8" + app0 + sof + b"\xff\xd9")
    return path


class HelpersTest(TempDirTestCase):
    def test_png_round_trip(self):
        rows = [bytes([0, 255, 0]), bytes([255, 255, 255])]
        p = self.tmp / "m.png"
        masks.write_png_gray(p, 3, 2, rows)
        self.assertEqual(read_png_gray(p), (3, 2, rows))
        self.assertFalse(Path(str(p) + ".part").exists())

    def test_jpeg_size(self):
        self.assertEqual(masks.jpeg_size(tiny_jpeg(self.tmp / "a.jpg", 640, 360)), (640, 360))
        (self.tmp / "b.jpg").write_bytes(b"not a jpeg at all")
        with self.assertRaises(ValueError):
            masks.jpeg_size(self.tmp / "b.jpg")

    def test_normalise_prompts_drops_bad_points_and_unknown_frames(self):
        got = masks.normalise_prompts({
            "f1.jpg": [[0.5, 0.5, 1], [2, 0.5, 1], [0.1, 0.1, 3], ["x", 0, 1], [0.2, 0.3, 0]],
            "f2.jpg": [[0.5, 0.5]],
            "nope.jpg": [[0.5, 0.5, 1]],
            "f3.jpg": "garbage",
        }, ["f1.jpg", "f2.jpg", "f3.jpg"])
        self.assertEqual(got, {"f1.jpg": [[0.5, 0.5, 1], [0.2, 0.3, 0]]})

    def test_disc_mask(self):
        rows = masks.disc_mask(20, 10, [[0.5, 0.5, 1]])
        self.assertEqual(len(rows), 10)
        self.assertEqual(rows[5][10], 255)                  # the click is kept
        self.assertEqual(rows[0][0], 0)                     # the corner is not
        # a remove click close by shrinks the disc
        small = masks.disc_mask(20, 10, [[0.5, 0.5, 1], [0.6, 0.5, 0]])
        self.assertLess(sum(map(sum, small)), sum(map(sum, rows)))
        self.assertEqual(sum(map(sum, masks.disc_mask(4, 4, [[0.5, 0.5, 0]]))), 0)   # nothing to keep

    def test_backend_choice(self):
        with mock.patch.dict("os.environ", {"SPLAT_STUDIO_MASK_BACKEND": "disc"}):
            self.assertEqual(masks.backend(), "disc")
        with mock.patch.dict("os.environ", {"SPLAT_STUDIO_MASK_BACKEND": "", "SPLAT_STUDIO_MASK_PYTHON": ""}):
            self.assertIsNone(masks.backend())               # not installed (HOME is a temp folder)


class MakeMasksTest(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.frames = self.tmp / "frames" / "vase"
        for i in range(1, 6):
            tiny_jpeg(self.frames / f"frame_{i:05d}.jpg", 32, 24)

    def test_one_mask_per_frame(self):
        out = masks.make_masks(self.frames, {"frame_00002.jpg": [[0.5, 0.5, 1], [0.9, 0.9, 0]]}, "disc")
        self.assertEqual(out, self.frames / "masks")
        self.assertEqual(sorted(p.name for p in out.glob("*.png")), [f"frame_{i:05d}.png" for i in range(1, 6)])
        self.assertTrue(masks.has_masks(self.frames))
        self.assertEqual(masks.load_prompts(self.frames), {"frame_00002.jpg": [[0.5, 0.5, 1], [0.9, 0.9, 0]]})
        w, h, rows = read_png_gray(out / "frame_00004.png")
        self.assertEqual((w, h), (32, 24))
        self.assertEqual(rows[12][16], 255)

    def test_needs_a_keep_click(self):
        with self.assertRaises(ValueError):
            masks.make_masks(self.frames, {"frame_00001.jpg": [[0.5, 0.5, 0]]}, "disc")
        self.assertFalse(masks.has_masks(self.frames))

    def test_not_installed(self):
        with mock.patch.object(masks, "backend", return_value=None), self.assertRaises(RuntimeError):
            masks.make_masks(self.frames, {"frame_00001.jpg": [[0.5, 0.5, 1]]})

    def test_redo_clears_old_masks(self):
        masks.make_masks(self.frames, {"frame_00001.jpg": [[0.5, 0.5, 1]]}, "disc")
        (self.frames / "masks" / "stale.png").write_bytes(b"x")
        masks.make_masks(self.frames, {"frame_00001.jpg": [[0.2, 0.2, 1]]}, "disc")
        self.assertFalse((self.frames / "masks" / "stale.png").exists())

    def test_cli(self):
        prompts = self.tmp / "p.json"
        prompts.write_text(json.dumps({"frame_00001.jpg": [[0.5, 0.5, 1]]}))
        with mock.patch.object(sys, "argv", ["mask", str(self.frames), "--prompts", str(prompts), "--backend", "disc"]):
            masks.main()
        self.assertTrue(masks.has_masks(self.frames))


@needs_ffmpeg
class PrepareForDenseTest(TempDirTestCase):
    """Masks go through the same resize and undistortion as the training images."""

    def test_masks_for_every_training_image(self):
        import os
        import subprocess
        from tests.helpers import FAKE_BIN, mock_env
        frames, ws = self.tmp / "frames" / "vase", self.tmp / "ws"
        names = [f"frame_{i:05d}.jpg" for i in range(1, 4)]
        for n in names:
            for d, size in ((frames, "64x48"), (ws / "images", "32x24"), (ws / "dense" / "images", "32x24")):
                d.mkdir(parents=True, exist_ok=True)
                subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"color=gray:s={size}",
                                "-frames:v", "1", str(d / n)], check=True)
        model = ws / "sparse" / "0"
        model.mkdir(parents=True)
        (model / "registered.txt").write_text("\n".join(names))
        (ws / "dense" / "source_model.txt").write_text(str(model))
        masks.make_masks(frames, {names[0]: [[0.5, 0.5, 1]]}, "disc")
        with mock_env({"PATH": f"{FAKE_BIN}{os.pathsep}{os.environ['PATH']}"}):
            self.assertEqual(masks.prepare_for_dense(frames, ws), 3)
        dense = ws / "dense"
        self.assertEqual(sorted(p.name for p in (dense / "masks").glob("*.png")),
                         [n.replace(".jpg", ".png") for n in names])
        self.assertEqual(masks.dense_mask_mode(dense), "transparent")
        self.assertFalse((ws / "mask_images").exists())

    def test_old_runs_are_refused_clearly(self):
        ws = self.tmp / "ws"
        (ws / "dense" / "images").mkdir(parents=True)
        tiny_jpeg(ws / "dense" / "images" / "frame_00001.jpg", 8, 8)
        tiny_jpeg(ws / "images" / "frame_00001.jpg", 8, 8)
        frames = self.tmp / "frames"
        masks.write_png_gray(frames / "masks" / "frame_00001.png", 8, 8, [bytes(8)] * 8)
        with self.assertRaisesRegex(ValueError, "before masks were supported"):
            masks.prepare_for_dense(frames, ws)


class DenseMaskModeTest(TempDirTestCase):
    def test_modes(self):
        d = self.tmp / "dense"
        d.mkdir()
        self.assertIsNone(masks.dense_mask_mode(d))
        (d / "masks.json").write_text(json.dumps({"mode": "ignore"}))
        self.assertIsNone(masks.dense_mask_mode(d))          # no masks folder
        (d / "masks").mkdir()
        self.assertEqual(masks.dense_mask_mode(d), "ignore")
        (d / "masks.json").write_text(json.dumps({"mode": "weird"}))
        self.assertIsNone(masks.dense_mask_mode(d))


try:
    import numpy as np  # noqa: F401
    from PIL import Image  # noqa: F401
    HAS_NP = True
except ImportError:
    HAS_NP = False


@unittest.skipUnless(HAS_NP, "numpy and Pillow not installed")
class MaskworkChunkingTest(TempDirTestCase):
    """maskwork.propagate with a stand-in SAM 2: every frame gets a mask, in chunks."""

    def test_forwards_and_backwards_in_chunks(self):
        import contextlib
        import numpy as np
        from PIL import Image
        frames = self.tmp / "frames"
        frames.mkdir()
        for i in range(10):
            Image.new("RGB", (16, 12), (i * 20, 0, 0)).save(frames / f"frame_{i:05d}.jpg")
        calls = []

        class Logits:
            def __init__(self, a): self.a = a
            def __gt__(self, v): return Logits(self.a > v)
            def __getitem__(self, i): return Logits(self.a[i])
            def cpu(self): return self
            def numpy(self): return self.a

        class Pred:
            def init_state(self, video_path, offload_video_to_cpu=False):
                return {"n": len(list(Path(video_path).iterdir())), "prompts": {}}
            def add_new_points_or_box(self, state, frame_idx, obj_id, points, labels):
                state["prompts"][frame_idx] = "points"
                return frame_idx, [1], Logits(np.ones((1, 1, 12, 16)))
            def add_new_mask(self, state, frame_idx, obj_id, mask):
                assert mask.shape == (12, 16)
                state["prompts"][frame_idx] = "mask"
            def propagate_in_video(self, state, start_frame_idx, reverse):
                calls.append((state["n"], dict(state["prompts"]), reverse))
                idx = range(start_frame_idx, -1, -1) if reverse else range(start_frame_idx, state["n"])
                for i in idx:
                    yield i, [1], Logits(np.ones((1, 1, 12, 16)))
            def reset_state(self, state): pass

        torch = types.ModuleType("torch")
        torch.inference_mode = contextlib.nullcontext
        torch.device = str
        torch.cuda = types.SimpleNamespace(is_available=lambda: False)
        torch.backends = types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda: False))
        from splat_studio import maskwork
        job = self.tmp / "job.json"
        out = self.tmp / "out"
        out.mkdir()
        job.write_text(json.dumps({"frames": [str(p) for p in sorted(frames.glob("*.jpg"))],
                                   "prompts": {"frame_00004.jpg": [[0.5, 0.5, 1]]},
                                   "out": str(out), "checkpoint": "x", "config": "y"}))
        with mock.patch.dict(sys.modules, {"torch": torch}), mock.patch.object(maskwork, "CHUNK", 4), \
                mock.patch.object(maskwork, "load_predictor", return_value=Pred()):
            maskwork.propagate(str(job))
        self.assertEqual(sorted(p.name for p in out.glob("*.png")), [f"frame_{i:05d}.png" for i in range(10)])
        forward = [c for c in calls if not c[2]]
        backward = [c for c in calls if c[2]]
        # forwards from frame 4: 4-7, 7-9 (one frame of overlap, carried as a mask)
        self.assertEqual([c[0] for c in forward], [4, 3])
        self.assertEqual(forward[0][1], {0: "points"})
        self.assertEqual(forward[1][1], {0: "mask"})
        # backwards from frame 4: 1-4 (starting from the clicks), then 0-1 (carried)
        self.assertEqual([c[0] for c in backward], [4, 2])
        self.assertEqual(backward[0][1], {3: "points"})
        self.assertEqual(backward[1][1], {1: "mask"})


if __name__ == "__main__":
    unittest.main()
