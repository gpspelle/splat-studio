"""Translations: every language has every key, placeholders match, and nothing the code
uses is missing (UI markup, scripts, advice codes, API error codes, progress labels)."""
import re
import unittest
from pathlib import Path

import tests  # noqa: F401
from splat_studio import colmap, guidance, setup_deps

STATIC = Path(__file__).resolve().parents[2] / "src" / "splat_studio" / "static"
SRC = STATIC.parent
PAIR = re.compile(r'"((?:[^"\\]|\\.)+)":\s*"((?:[^"\\]|\\.)*)"')


def load_dicts() -> dict:
    js = (STATIC / "i18n.js").read_text()
    body = js[js.index("const I18N = {"):js.index("function getLang")]
    blocks = re.split(r"\n    (en|pt): \{", body)
    out = {}
    for lang, block in zip(blocks[1::2], blocks[2::2]):
        out[lang] = dict(PAIR.findall(block))
    return out


DICTS = load_dicts()
EN, PT = DICTS["en"], DICTS["pt"]


def placeholders(s: str) -> set:
    return set(re.findall(r"\{(\w+)\}", s))


class DictionaryTest(unittest.TestCase):
    def test_both_languages_present_and_substantial(self):
        self.assertEqual(set(DICTS), {"en", "pt"})
        self.assertGreater(len(EN), 150)

    def test_same_keys(self):
        self.assertEqual(sorted(set(EN) - set(PT)), [], "missing in Portuguese")
        self.assertEqual(sorted(set(PT) - set(EN)), [], "missing in English")

    def test_same_placeholders(self):
        for k in EN:
            self.assertEqual(placeholders(EN[k]), placeholders(PT[k]), k)

    def test_portuguese_is_actually_translated(self):
        same = [k for k in EN if EN[k] == PT[k] and len(EN[k]) > 12 and "{" not in EN[k]]
        self.assertLess(len(same), 5, same)

    def test_html_values_are_simple_markup(self):
        for d in DICTS.values():
            for k, v in d.items():
                self.assertNotIn("<script", v.lower(), k)
                if k.endswith(".html"):
                    self.assertEqual(v.count("<li>"), v.count("</li>"), k)


class CoverageTest(unittest.TestCase):
    def keys_in(self, text: str) -> set:
        keys = set(re.findall(r'data-i18n(?:-html|-placeholder|-title|-aria-label)?="([^"]+)"', text))
        keys |= set(re.findall(r'\bt\("([a-zA-Z0-9_.]+)"', text))
        return keys

    def test_markup_and_script_keys_exist(self):
        for page in ("index.html", "guide.html", "history.html", "keep.html", "site.js", "runs.js", "viewer.html", "compare.html"):
            keys = {k for k in self.keys_in((STATIC / page).read_text())
                    if not k.endswith(".") and "$" not in k}         # skip "prefix." + x and ${key} templates
            missing = sorted(k for k in keys if k not in EN)
            self.assertEqual(missing, [], page)

    def test_every_advice_code_is_translated(self):
        codes = set(re.findall(r'tip\("\w+", "([\w.]+)"', (SRC / "guidance.py").read_text()))
        self.assertGreater(len(codes), 12)
        self.assertEqual(sorted(c for c in codes if "advice." + c not in EN), [])

    def test_every_api_error_code_is_translated(self):
        text = (SRC / "server" / "jobs.py").read_text() + (SRC / "server" / "app.py").read_text()
        codes = set(re.findall(r'ApiError\("(\w+)"', text)) | set(re.findall(r'"code": "(\w+)"', text))
        self.assertGreater(len(codes), 8)
        self.assertEqual(sorted(c for c in codes if "err." + c not in EN), [])

    def test_progress_labels_are_translated(self):
        steps = set(re.findall(r'label="([^"]+)"', (SRC / "colmap.py").read_text())) | {"training splat"}
        self.assertEqual(sorted(s for s in steps if "step." + s not in EN), [])
        attempts = {p[2] for p in colmap.build_plan(100, 2)}
        self.assertEqual(sorted(a for a in attempts if "attempt." + a not in EN), [])

    def test_tool_descriptions_are_translated(self):
        names = {i["name"] for i in setup_deps.check()} | {"Homebrew"}
        self.assertEqual(sorted(n for n in names if "tool." + n not in EN), [])

    def test_english_advice_matches_python_text(self):
        """The UI's English rendering of each advice code equals the server's English text."""
        samples = guidance.video_advice({"width": 1280, "height": 720, "fps": 15.0, "duration": 12.0,
                                         "codec": "h264", "hdr": True, "portrait": True})
        samples += guidance.video_advice({"width": 3840, "height": 2160, "fps": 30.0, "duration": 90.0,
                                          "codec": "hevc", "hdr": False, "portrait": False})
        samples += guidance.run_advice({"status": "done", "registered": "200/250 frames (80%)", "frames": 150,
                                        "blurry": 60, "cleaned": {"kept": 1200, "total": 3400},
                                        "track_breaks": [{"first": "a", "last": "b", "count": 3, "when": "0:10–0:12"}]})
        for a in samples:
            rendered = re.sub(r"\{(\w+)\}", lambda m: a["params"][m.group(1)], EN["advice." + a["code"]])
            self.assertEqual(rendered, a["text"], a["code"])


if __name__ == "__main__":
    unittest.main()
