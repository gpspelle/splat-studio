# Notes for coding agents

Read [CONTRIBUTING.md](CONTRIBUTING.md) first: setup, tests, layout and conventions all live there.

## Keep the README current

The README (English and Portuguese halves) and the screenshots in `docs/images/` are how people
learn the app, so they ship in the same PR as the change they describe.

- **Changed something a user sees or does** (a page in `static/`, interface text in `i18n.js`, a
  CLI command or flag, a default, a file the app writes)? Update the README sections that mention
  it, in both languages, and `docs/*.md` where relevant. Add a line to `CHANGELOG.md`.
- **Changed how a page looks?** Regenerate the screenshots and look at every image before you
  commit it:

  ```
  pip install playwright opencv-python-headless && python -m playwright install chromium
  python3 scripts/readme_screenshots.py      # ~5 min; rewrites docs/images/*.png
  ```

  If a screenshot needs a state the script can't reach yet, extend the script instead of
  taking a manual screenshot, so the next person can regenerate it too.
- **Removed or renamed an option, button or file?** Search the README and `docs/` for its old
  name (`grep -rn "Old name" README.md docs/`) and fix every mention.
- CI (`.github/workflows/docs.yml`) fails a PR that touches user-facing code without touching
  `README.md` or `docs/`. If the change truly needs no docs (a refactor, a test-only fix), put a
  line starting with `No docs needed:` and the reason in the PR description.
