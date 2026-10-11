"""Test suite. Run everything with:  python3 -m unittest discover -s tests -t .

Makes the package importable from a plain clone (no install needed).
"""
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
