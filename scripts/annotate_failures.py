#!/usr/bin/env python3
"""Turn failed tests in a unittest log into GitHub annotations, so CI failures can be read
from the checks page (and its API) without downloading the job log.

    python -m unittest ... 2>&1 | tee test.log; python3 scripts/annotate_failures.py test.log
"""
import re
import sys
from pathlib import Path


def blocks(text: str):
    parts = re.split(r"^={50,}\n", text, flags=re.M)
    for p in parts[1:]:
        head, _, body = p.partition("\n")
        m = re.match(r"(FAIL|ERROR): (\S+) \(([^)]+)\)", head)
        if m:
            body = body.split("\n" + "-" * 70 + "\nRan ")[0].strip("-\n")
            yield f"{m.group(1)}: {m.group(3)}", body


def main(path: str) -> None:
    for title, body in blocks(Path(path).read_text(errors="replace")):
        lines = body.splitlines()[-25:]
        msg = "%0A".join(l.replace("%", "%25").replace("\r", "") for l in lines)
        print(f"::error title={title}::{msg}")


if __name__ == "__main__":
    main(sys.argv[1])
