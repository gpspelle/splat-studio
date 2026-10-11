"""scripts/check_docs.py: user-facing changes need README or docs changes."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from check_docs import problems  # noqa: E402


class CheckDocsTest(unittest.TestCase):
    def test_ui_change_without_docs_is_flagged(self):
        self.assertEqual(problems(["src/splat_studio/static/index.html", "tests/unit/test_jobs.py"]),
                         ["src/splat_studio/static/index.html"])

    def test_cli_change_without_docs_is_flagged(self):
        self.assertEqual(problems(["src/splat_studio/pipeline.py"]), ["src/splat_studio/pipeline.py"])

    def test_readme_or_docs_change_passes(self):
        self.assertEqual(problems(["src/splat_studio/static/i18n.js", "README.md"]), [])
        self.assertEqual(problems(["src/splat_studio/static/index.html", "docs/images/studio.png"]), [])

    def test_internal_change_passes(self):
        self.assertEqual(problems(["src/splat_studio/colmap.py", "tests/unit/test_colmap_logic.py"]), [])

    def test_opt_out_line_in_pr_body(self):
        body = "Refactor.\n\nNo docs needed: only renames a helper."
        self.assertEqual(problems(["src/splat_studio/server/jobs.py"], body), [])
        self.assertEqual(problems(["src/splat_studio/server/jobs.py"], "No docs needed:"),
                         ["src/splat_studio/server/jobs.py"])     # a reason is required


if __name__ == "__main__":
    unittest.main()
