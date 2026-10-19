import sys
import unittest
from unittest import mock

from splat_studio import setup_deps


class OpenCvInstall(unittest.TestCase):
    def test_virtualenv_installs_into_the_venv(self):
        """pip refuses --user inside a venv ("User site-packages are not visible")."""
        with mock.patch.object(sys, "prefix", "/proj/.venv"), mock.patch.object(sys, "base_prefix", "/usr"):
            self.assertTrue(setup_deps.in_virtualenv())
            cmds = setup_deps.cv2_install_commands("/proj/.venv/bin/python3")
        self.assertEqual(cmds, ["/proj/.venv/bin/python3 -m pip install --quiet opencv-python-headless"])

    def test_system_python_installs_for_the_user(self):
        with mock.patch.object(sys, "prefix", "/opt/homebrew"), mock.patch.object(sys, "base_prefix", "/opt/homebrew"):
            self.assertFalse(setup_deps.in_virtualenv())
            cmds = setup_deps.cv2_install_commands("/opt/homebrew/bin/python3")
        self.assertEqual(len(cmds), 2)
        self.assertTrue(all("--user" in c for c in cmds))
        self.assertIn("--break-system-packages", cmds[1])


if __name__ == "__main__":
    unittest.main()
