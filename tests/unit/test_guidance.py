import unittest

import tests  # noqa: F401
from splat_studio import guidance


def meta(**kw):
    m = {"width": 3840, "height": 2160, "fps": 30.0, "duration": 150.0, "codec": "hevc", "hdr": False,
         "portrait": False}
    m.update(kw)
    return m


def levels(tips):
    return [t["level"] for t in tips]


class ProbeParsingTest(unittest.TestCase):
    def test_iphone_portrait_hdr(self):
        data = {"streams": [{"codec_type": "audio"},
                            {"codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 2160,
                             "avg_frame_rate": "30000/1001", "color_transfer": "arib-std-b67",
                             "side_data_list": [{"side_data_type": "Display Matrix", "rotation": -90}]}],
                "format": {"duration": "95.5"}}
        m = guidance.parse_probe(data)
        self.assertEqual((m["width"], m["height"]), (2160, 3840))          # rotated -> portrait
        self.assertTrue(m["portrait"] and m["hdr"])
        self.assertAlmostEqual(m["fps"], 29.97)
        self.assertEqual(m["duration"], 95.5)

    def test_no_video_stream(self):
        self.assertIsNone(guidance.parse_probe({"streams": [{"codec_type": "audio"}]}))


class VideoAdviceTest(unittest.TestCase):
    def test_good_video(self):
        tips = guidance.video_advice(meta())
        self.assertEqual(levels(tips), ["ok"])
        self.assertIn("4K", tips[0]["text"])

    def test_short_low_res_low_fps_are_warnings(self):
        tips = guidance.video_advice(meta(duration=12, width=1280, height=720, fps=15))
        self.assertEqual(levels(tips).count("warn"), 3)
        self.assertNotIn("ok", levels(tips))

    def test_iphone_video_shrunk_on_the_way_says_how_to_get_the_original(self):
        tips = guidance.video_advice(meta(duration=75, width=320, height=568, fps=30, portrait=True,
                                          phone="iPhone 16 Pro"))
        (t,) = [t for t in tips if t["level"] == "warn"]
        self.assertEqual(t["code"], "video.shrunk")
        self.assertIn("iPhone 16 Pro", t["text"])
        self.assertIn("AirDrop", t["text"])
        # a 720p iPhone video is a real (if low) setting: the usual advice
        tips = guidance.video_advice(meta(duration=75, width=1280, height=720, phone="iPhone 16 Pro"))
        self.assertEqual([t["code"] for t in tips if t["level"] == "warn"], ["video.low_res"])

    def test_probe_reads_the_phone_model(self):
        data = {"streams": [{"codec_type": "video", "width": 568, "height": 320, "avg_frame_rate": "30/1",
                             "side_data_list": [{"rotation": -90}]}],
                "format": {"duration": "75.6", "tags": {"com.apple.quicktime.make": "Apple",
                                                        "com.apple.quicktime.model": "iPhone 16 Pro"}}}
        m = guidance.parse_probe(data)
        self.assertEqual((m["width"], m["height"], m["phone"]), (320, 568, "iPhone 16 Pro"))
        data["format"]["tags"] = {}
        self.assertEqual(guidance.parse_probe(data)["phone"], "")

    def test_infos_keep_ok(self):
        tips = guidance.video_advice(meta(duration=45, hdr=True, portrait=True, width=2160, height=3840))
        self.assertEqual(levels(tips)[0], "ok")
        self.assertEqual(levels(tips).count("info"), 3)

    def test_unreadable(self):
        self.assertEqual(levels(guidance.video_advice(None)), ["warn"])


class RunAdviceTest(unittest.TestCase):
    def test_frame_time(self):
        self.assertAlmostEqual(guidance.frame_time("IMG_1_0001.jpg", 2.0), 0.0)
        self.assertAlmostEqual(guidance.frame_time("IMG_1_0061.jpg", 2.0, start=3), 33.0)
        self.assertIsNone(guidance.frame_time("x.jpg", 2.0))
        self.assertIsNone(guidance.frame_time("a_0005.jpg", None))

    def test_track_break_times(self):
        out = guidance.track_break_times([{"first": "v_0085.jpg", "last": "v_0102.jpg", "count": 18}], 2.0)
        self.assertEqual(out[0]["when"], "0:42–0:50")

    def test_registered_ratio(self):
        self.assertAlmostEqual(guidance.registered_ratio("230/250 frames (92%)"), 0.92)
        self.assertIsNone(guidance.registered_ratio(None))

    def test_excellent_capture(self):
        tips = guidance.run_advice({"status": "done", "registered": "250/250 frames (100%)"})
        self.assertEqual(levels(tips), ["ok"])

    def test_broken_track_points_at_timestamps(self):
        job = {"status": "done", "registered": "200/250 frames (80%)",
               "track_breaks": [{"first": "a", "last": "b", "count": 50, "when": "0:42–0:51"}]}
        tips = guidance.run_advice(job)
        self.assertIn("0:42–0:51", tips[0]["text"])
        self.assertEqual(tips[0]["level"], "warn")
        self.assertTrue(any("80%" in t["text"] for t in tips))

    def test_failure_and_blur(self):
        job = {"status": "failed", "error": "Not good enough to train on", "frames": 150, "blurry": 100}
        tips = guidance.run_advice(job)
        self.assertTrue(any("blurry" in t["text"] for t in tips))
        self.assertTrue(any("continuous take" in t["text"] for t in tips))

    def test_nothing_reconstructed(self):
        tips = guidance.run_advice({"status": "failed", "error": "COLMAP could not reconstruct anything."})
        self.assertTrue(any("textured surface" in t["text"] for t in tips))


class ExtractionInfoTest(unittest.TestCase):
    def test_roundtrip(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            guidance.save_extraction_info(Path(d), 2.5, 3.0, 100.0, "v.mov")
            self.assertEqual(guidance.load_extraction_info(Path(d))["fps"], 2.5)
            self.assertEqual(guidance.load_extraction_info(Path(d) / "missing"), {})


if __name__ == "__main__":
    unittest.main()
