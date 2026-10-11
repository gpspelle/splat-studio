import unittest

import tests  # noqa: F401  (puts src/ on sys.path)
from splat_studio.common import child_env, fmt_time, fmt_timestamp, module_cmd, to_seconds


class FormattingTest(unittest.TestCase):
    def test_fmt_time(self):
        self.assertEqual(fmt_time(None), "?")
        self.assertEqual(fmt_time(0), "0s")
        self.assertEqual(fmt_time(59.4), "59s")
        self.assertEqual(fmt_time(60), "1m00s")
        self.assertEqual(fmt_time(754), "12m34s")
        self.assertEqual(fmt_time(3600), "1h00m")
        self.assertEqual(fmt_time(4000), "1h06m")

    def test_to_seconds(self):
        self.assertEqual(to_seconds("12"), 12)
        self.assertEqual(to_seconds("1:05"), 65)
        self.assertEqual(to_seconds("0:01:05.5"), 65.5)

    def test_fmt_timestamp(self):
        self.assertEqual(fmt_timestamp(0), "0:00")
        self.assertEqual(fmt_timestamp(65.4), "1:05")
        self.assertEqual(fmt_timestamp(-3), "0:00")


class ChildProcessTest(unittest.TestCase):
    def test_module_cmd_runs_package_modules(self):
        cmd = module_cmd("frames", "a.mov", 5)
        self.assertEqual(cmd[1:4], ["-u", "-m", "splat_studio.frames"])
        self.assertEqual(cmd[-2:], ["a.mov", "5"])

    def test_child_env_makes_package_importable(self):
        env = child_env({"X": "1"})
        self.assertIn("src", env["PYTHONPATH"].split(":")[0])
        self.assertEqual(env["X"], "1")


if __name__ == "__main__":
    unittest.main()
