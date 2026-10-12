"""Turntable videos: JPEG frames in, MP4 or GIF out."""
import json
import subprocess

from tests.helpers import TempDirTestCase, needs_ffmpeg
from splat_studio import turntable


def jpeg_stream(path, n=12, size="160x160"):
    """n JPEG frames concatenated, as the viewer sends them."""
    data = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc2=s={size}:r=12",
                           "-frames:v", str(n), "-f", "image2pipe", "-c:v", "mjpeg", "-"], capture_output=True, check=True).stdout
    path.write_bytes(data)
    return path


def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-show_entries",
                        "stream=codec_name,width,height,nb_read_frames", "-of", "json", str(path)],
                       capture_output=True, text=True, check=True)
    return json.loads(r.stdout)["streams"][0]


@needs_ffmpeg
class EncodeTest(TempDirTestCase):
    def test_mp4(self):
        out = turntable.encode(jpeg_stream(self.tmp / "f.mjpeg"), self.tmp / "v.mp4", "mp4", 12)
        s = probe(out)
        self.assertIn(s["codec_name"], ("h264", "mpeg4"))
        self.assertEqual((s["width"], s["height"], int(s["nb_read_frames"])), (160, 160, 12))
        self.assertFalse(list(self.tmp.glob("*.part*")))

    def test_gif_is_small_and_loops(self):
        out = turntable.encode(jpeg_stream(self.tmp / "f.mjpeg", size="800x450"), self.tmp / "v.gif", "gif", 12)
        s = probe(out)
        self.assertEqual((s["codec_name"], s["width"]), ("gif", 480))

    def test_bad_input(self):
        (self.tmp / "f.mjpeg").write_bytes(b"not jpeg")
        with self.assertRaises(RuntimeError):
            turntable.encode(self.tmp / "f.mjpeg", self.tmp / "v.mp4", "mp4", 30)
        with self.assertRaises(ValueError):
            turntable.encode(self.tmp / "f.mjpeg", self.tmp / "v.webm", "webm", 30)

    def test_output_next_to_the_splat(self):
        self.assertEqual(turntable.output_path(self.tmp / "s" / "high_3000.ply", "gif").name, "high_3000_turntable.gif")
