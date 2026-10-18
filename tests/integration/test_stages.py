"""Single stages with real ffmpeg and the stand-in COLMAP."""
import json

from tests.helpers import TempDirTestCase, fake_tools_env, make_video, needs_ffmpeg, run_module


@needs_ffmpeg
class FramesStageTest(TempDirTestCase):
    def test_extracts_about_target_frames_and_records_timing(self):
        video = make_video(self.tmp / "My Clip.mov", seconds=10)
        r = run_module("frames", [video, "--target", "40"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = self.tmp / "frames" / "My_Clip"
        n = len(list(out.glob("*.jpg")))
        self.assertTrue(36 <= n <= 42, n)
        self.assertIn("Extracting at 4.0 fps", r.stdout)
        info = json.loads((out / "extraction.json").read_text())
        self.assertEqual((info["fps"], info["start"]), (4.0, 0.0))

    def test_blurry_frames_are_swapped_for_sharp_neighbours(self):
        """Frames 58-62 are blurred (only the moment's own frame, 60, is hit: its neighbours 65 and 70
        are sharp) and 148-162 (every candidate of the moment at 5 s is hit)."""
        try:
            import cv2  # noqa: F401
        except ImportError:
            self.skipTest("OpenCV not installed")
        import subprocess
        video = self.tmp / "shaky.mov"
        blur = "boxblur=12:3:enable='between(n,58,62)+between(n,148,162)'"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "testsrc2=duration=10:size=640x360:rate=30", "-vf", blur, "-pix_fmt", "yuv420p",
                        "-c:v", "libx264", "-qp", "0", str(video)], check=True)
        r = run_module("frames", [video, "--target", "20"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = self.tmp / "frames" / "shaky"
        self.assertIn("Looking at 3 frames around each moment", r.stdout)
        self.assertIn("Replaced 1 blurry frames with sharper ones next to them", r.stdout)
        self.assertIn("Too blurry to use around 0:05", r.stdout)
        names = sorted(p.name for p in out.glob("*.jpg"))
        self.assertEqual(len(names), 19)
        self.assertIn("shaky_0005.jpg", names)                     # the moment at 2 s, swapped for a sharp neighbour
        self.assertNotIn("shaky_0011.jpg", names)                  # the moment at 5 s: nothing sharp nearby
        self.assertEqual(sorted(p.name for p in (out / "_blurry").glob("*.jpg")), ["shaky_0011.jpg"])
        self.assertFalse((out / "_candidates").exists())
        stats = json.loads((out / "extraction.json").read_text())["stats"]
        self.assertEqual({k: stats[k] for k in ("target", "auto", "kept", "replaced", "dropped", "dropped_at")},
                         {"target": 20, "auto": False, "kept": 19, "replaced": 1, "dropped": 1, "dropped_at": [5.0]})
        line = next(l for l in r.stdout.splitlines() if l.startswith("Frame stats: "))
        self.assertEqual(json.loads(line[len("Frame stats: "):]), stats)

    def test_trim_and_refuse_overwrite(self):
        video = make_video(self.tmp / "clip.mp4", seconds=10)
        r = run_module("frames", [video, "--out", "f", "--fps", "2", "--start", "0:02", "--end", "0:07"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(list((self.tmp / "f").glob("*.jpg"))), 10)
        again = run_module("frames", [video, "--out", "f"], cwd=self.tmp)
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("already has frames", again.stderr)

    def test_bad_trim(self):
        video = make_video(self.tmp / "clip.mp4", seconds=4)
        r = run_module("frames", [video, "--start", "0:05"], cwd=self.tmp)
        self.assertNotEqual(r.returncode, 0)


class ColmapStageTest(TempDirTestCase):
    def frames(self, n=60):
        d = self.tmp / "frames" / "vase"
        d.mkdir(parents=True)
        for i in range(1, n + 1):
            (d / f"vase_{i:04d}.jpg").write_bytes(b"\xff\xd8 not really a jpeg")
        return d

    def test_clean_capture_stops_after_first_attempt(self):
        r = run_module("colmap", [self.frames(), "--step", "1"], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Attempt 1/3", r.stdout)
        self.assertNotIn("Attempt 2/3", r.stdout)
        self.assertIn("ignoring 1 extra small model", r.stdout)
        run = (self.tmp / "workspace" / "vase" / "latest").resolve()
        self.assertTrue((run / "dense" / "sparse" / "0" / "cameras.bin").exists())
        self.assertIn("OK - train on", (run / "summary.txt").read_text())
        self.assertFalse((run / ".lock").exists())                           # released on exit

    def test_broken_track_escalates_and_reports_gaps(self):
        env = fake_tools_env(FAKE_COLMAP_REGISTER="0.8")
        r = run_module("colmap", [self.frames(), "--step", "1"], cwd=self.tmp, env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Attempt 3/3: exhaustive", r.stdout)                   # tried everything
        self.assertIn("Track breaks", r.stdout)
        self.assertIn("Usable but incomplete (80%)", r.stdout)
        summary = next((self.tmp / "workspace" / "vase").glob("run_*/summary.txt")).read_text()
        self.assertIn("track break:", summary)

    def test_step_2_retries_with_every_frame(self):
        env = fake_tools_env(FAKE_COLMAP_REGISTER="0.8")
        r = run_module("colmap", [self.frames(80), "--step", "2"], cwd=self.tmp, env=env)
        self.assertIn("retrying with every frame", r.stdout)
        self.assertIn("Attempt 5/6", r.stdout)

    def test_unusable_capture_fails_clearly(self):
        env = fake_tools_env(FAKE_COLMAP_REGISTER="0.4")
        r = run_module("colmap", [self.frames(), "--step", "1"], cwd=self.tmp, env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("Not good enough", r.stdout)
        summary = next((self.tmp / "workspace" / "vase").glob("run_*/summary.txt")).read_text()
        self.assertIn("NOT GOOD ENOUGH", summary)

    def test_explicit_workspace_is_protected(self):
        frames = self.frames()
        self.assertEqual(run_module("colmap", [frames, "ws", "--step", "1"], cwd=self.tmp).returncode, 0)
        again = run_module("colmap", [frames, "ws", "--step", "1"], cwd=self.tmp)
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("previous run", again.stderr)
        self.assertEqual(run_module("colmap", [frames, "ws", "--step", "1", "--overwrite"], cwd=self.tmp).returncode, 0)
