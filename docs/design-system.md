# Design system: "Instrument"

Splat Studio looks like a precision tool, not a generic web app: warm paper and true black,
one signal orange, hairline rules instead of shadows, square corners, and monospace for data
and labels. Everything lives in [`src/splat_studio/static/design.css`](../src/splat_studio/static/design.css)
and is shared by the Studio page, the viewer, the comparison page and exported websites.

## Where it comes from

| Reference | What we took | What we left |
|---|---|---|
| Teenage Engineering (OP-1, product pages) | Constraint as identity: a five-colour palette with orange reserved for "this matters", monospace labels, spec-sheet grids with 1px rules | Fully square, all-mono UI: body text and buttons stay in a proportional face for readability |
| Halide / camera apps | The 3D viewer as a viewfinder: dark canvas in both themes, HUD in small mono capitals, corner marks | Gesture-only controls: every action keeps a visible, labelled button |
| Linear | Few inputs drive the whole theme (paper, ink, signal); neutral, timeless accent use; density with clear hierarchy | LCH theme generator (overkill for two themes) |
| Vercel Geist | Monochrome first, colour only for state and action, "default to stillness" motion | Its fonts and look (too recognisable) |
| Spline | Rationing the accent to one lit element per view; the canvas as the hero | Glow effects, dark-only |
| Apple HIG (Liquid Glass) | Controls float as a separate layer above content | Translucent glass everywhere: only the viewer HUD uses a light blur, for legibility over the model |
| Radix Colors | Role-based scale: backgrounds, component states, borders, solid fills, text | 12 steps per hue (we need far fewer) |
| WCAG 2.2 | AA contrast, visible focus, 24px minimum targets, reduced motion | - |

## Tokens

| Token | Light | Dark | Use |
|---|---|---|---|
| `--paper` | `#efede7` | `#0d0d0c` | Page background |
| `--surface` | `#f8f7f3` | `#171715` | Panels, inputs |
| `--sunken` | `#e5e2da` | `#090908` | Wells, hover, tracks |
| `--ink` | `#121211` | `#efede7` | Text, structural rules |
| `--ink-2` / `--ink-3` | `#45433e` / `#66635c` | `#c4c1b8` / `#9a968d` | Secondary / tertiary text |
| `--signal` | `#ff4a00` | `#ff5a14` | The accent: fills only, one primary action per view |
| `--signal-ink` | `#b83300` | `#ff7a3d` | The accent as text (links) |
| `--ok` `--warn` `--bad` | `#0f6e39` `#8a5600` `#b42318` | `#3ccf7d` `#e0a43a` `#ff6b5e` | Status, always paired with an icon or label |
| `--canvas` | `#0b0b0a` | same | 3D viewport (dark in both themes) |

All text pairs meet WCAG AA (4.5:1) on every background, input borders and focus rings meet
3:1, and text on the signal colour is 5.6:1. `tests/unit/test_design.py` recomputes these from
the stylesheet, so a colour change that breaks contrast fails the build.

**Type.** Inter (UI, headings; tight tracking at display sizes) and JetBrains Mono (labels in
tracked capitals, numbers with tabular figures, metadata, the viewer HUD). Both are bundled
(SIL Open Font License), so nothing loads from the internet. Scale: label 10.5, meta 12,
small 13, body 14, title 15, heading 20, display 26.

**Space** 4px grid (`--s-1` … `--s-10`). **Shape** 2px radius, 1px hairlines. **Controls** 34px
high (28px compact): above the 24px WCAG minimum. **Motion** 120/200 ms, one easing curve,
used only to explain a change; all of it switches off with *Reduce motion*.

## Components

- **Buttons:** outlined by default; `.primary` (signal fill, one per view), `.ghost`, `.small`, `.link`.
- **Segmented control** `.seg`: a joined row, the chosen option inked.
- **Checkbox:** square, fills with signal when checked.
- **Panel** `.card`: ruled box whose heading row carries a small signal square.
- **Status** `.pill`: mono capitals with a coloured dot (running pulses, unless motion is reduced).
- **Progress** `.bar`: ruled track, ink fill with a signal tip. **Steps** `.steps`: four ruled cells.
- **Advice list**, **toast** (ink with a signal edge), **dialog** (hard offset shadow).
- **Site header and footer** (`.site-head`, `.site-nav`, `.site-foot`), built by `site.js`: wordmark with the
  logo, navigation with the current page underlined in signal, and a ruled footer with link columns.
- **Run rows** (`.run`, `.run.compact`, `.runs-sub`), built by `runs.js`: a full row with steps and progress
  while a run is going, then one compact line that opens with **Details**.
- **HUD** (`.hud`, `.hud-chip`, `.hud-bar`): viewer chrome over the 3D canvas, mono capitals,
  joined by hairlines; `.marks` draws the viewfinder corners.

## Rules of thumb

1. One orange thing per view. If two things are orange, one of them is wrong.
2. Structure with rules and space, not shadows and cards.
3. Numbers, sizes, times and file names in mono; sentences in Inter.
4. Colour never carries meaning alone: status has an icon or a word.
5. Pages define no colours of their own; add a token instead (the tests check this).
