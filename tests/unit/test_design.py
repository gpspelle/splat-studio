"""The design system (static/design.css): every page uses it, and its colours stay accessible.

Contrast targets are WCAG 2.2 AA: 4.5:1 for text, 3:1 for input borders and focus rings."""
import re
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "src" / "splat_studio" / "static"
CSS = (STATIC / "design.css").read_text()


def tokens(block: str) -> dict:
    return dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})", block))


def theme(selector: str) -> dict:
    start = CSS.index(selector)
    return tokens(CSS[start:CSS.index("}", CSS.index("{", start))])


LIGHT = theme(":root {")
DARK = {**LIGHT, **theme(':root[data-theme="dark"]')}


def luminance(hex_color: str) -> float:
    def channel(c):
        c = int(c, 16) / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    h = hex_color.lstrip("#")
    r, g, b = (channel(h[i:i + 2]) for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


class ContrastTest(unittest.TestCase):
    TEXT = ("ink", "ink-2", "ink-3", "signal-ink", "ok", "warn", "bad")

    def check(self, t, name):
        for bg in ("paper", "surface", "sunken"):
            for fg in self.TEXT:
                self.assertGreaterEqual(contrast(t[fg], t[bg]), 4.5, f"{name}: {fg} on {bg}")
            self.assertGreaterEqual(contrast(t["focus"], t[bg]), 3, f"{name}: focus ring on {bg}")
        for bg in ("paper", "surface"):
            self.assertGreaterEqual(contrast(t["field"], t[bg]), 3, f"{name}: input border on {bg}")
        self.assertGreaterEqual(contrast(t["on-signal"], t["signal"]), 4.5, f"{name}: text on the signal colour")

    def test_light_theme(self):
        self.check(LIGHT, "light")

    def test_dark_theme(self):
        self.check(DARK, "dark")

    def test_system_dark_matches_explicit_dark(self):
        start = CSS.index("@media (prefers-color-scheme: dark)")
        system = tokens(CSS[start:CSS.index(":root[data-theme=\"dark\"]")])
        self.assertEqual(system, theme(':root[data-theme="dark"]'))

    def test_hud_text_on_canvas(self):
        self.assertGreaterEqual(contrast(LIGHT["hud-ink"], LIGHT["canvas"]), 4.5)
        self.assertGreaterEqual(contrast(LIGHT["hud-ink-2"], LIGHT["canvas"]), 4.5)


class UsageTest(unittest.TestCase):
    def test_every_page_uses_the_design_system(self):
        for page in ("index.html", "viewer.html", "compare.html", "keep.html"):
            html = (STATIC / page).read_text()
            self.assertRegex(html, r'href="\.?/design\.css"', page)
            # pages define no colours of their own: everything comes from tokens
            styles = "".join(re.findall(r"<style>(.*?)</style>", html, re.S))
            self.assertEqual(re.findall(r"#[0-9a-fA-F]{6}\b", styles.replace("#ff8a80", "")), [], page)

    def test_fonts_are_bundled_with_their_licences(self):
        for f in ("InterVariable.woff2", "JetBrainsMono.woff2", "Inter-OFL.txt", "JetBrainsMono-OFL.txt"):
            self.assertTrue((STATIC / "fonts" / f).is_file(), f)
        for url in re.findall(r'url\("([^"]+)"\)', CSS):
            self.assertTrue((STATIC / url).is_file(), url)

    def test_reduced_motion_and_focus_are_handled(self):
        self.assertIn("prefers-reduced-motion: reduce", CSS)
        self.assertIn(":focus-visible", CSS)
