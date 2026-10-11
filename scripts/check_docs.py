#!/usr/bin/env python3
"""Fail when a change touches user-facing code but not the README or docs/.

    python3 scripts/check_docs.py origin/main     # compares HEAD with the merge base

Opt out for one PR with a line starting `No docs needed:` in the PR description (read from
the PR_BODY environment variable in CI).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from fnmatch import fnmatch

# What a user sees or does: the pages, their text, the CLI, and what runs produce.
USER_FACING = [
    "src/splat_studio/static/*.html",
    "src/splat_studio/static/*.css",
    "src/splat_studio/static/*.js",
    "src/splat_studio/__main__.py",
    "src/splat_studio/pipeline.py",
    "src/splat_studio/guidance.py",
    "src/splat_studio/splats.py",
    "src/splat_studio/server/*.py",
]
DOCS = ["README.md", "docs/*"]
OPT_OUT = re.compile(r"^\s*no docs needed:\s*\S", re.I | re.M)


def matches(path: str, patterns: list) -> bool:
    return any(fnmatch(path, p) for p in patterns)


def problems(changed: list, pr_body: str = "") -> list:
    """The user-facing files changed without docs, or [] when the change is fine."""
    code = [p for p in changed if matches(p, USER_FACING)]
    if not code or any(matches(p, DOCS) for p in changed) or OPT_OUT.search(pr_body or ""):
        return []
    return code


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "origin/main"
    changed = subprocess.run(["git", "diff", "--name-only", f"{base}...HEAD"], check=True,
                             capture_output=True, text=True).stdout.split()
    code = problems(changed, os.environ.get("PR_BODY", ""))
    if not code:
        print("Docs check passed.")
        return 0
    print("These user-facing files changed, but README.md and docs/ did not:")
    print("\n".join(f"  {p}" for p in code))
    print("\nUpdate the README (both languages) and, if a page looks different, regenerate the\n"
          "screenshots with `python3 scripts/readme_screenshots.py`. If nothing a user sees\n"
          "changed, add a line `No docs needed: <reason>` to the PR description. See AGENTS.md.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
