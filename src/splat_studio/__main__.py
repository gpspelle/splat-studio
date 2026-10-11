"""Command line entry point.

    splat-studio                      open the web UI (same as `splat-studio ui`)
    splat-studio ui [--data-dir D] [--port N] [--no-browser]
    splat-studio run VIDEO [options]  whole pipeline in the terminal (see `run --help`)
    splat-studio setup                install missing tools and build Brush (may ask for a password)
    splat-studio check                show which tools are installed
    splat-studio frames | colmap ...  run a single stage
    splat-studio inspect MODEL.ply    explain fog / background in a trained splat
    splat-studio clean MODEL.ply      save MODEL_clean.ply without the background fog
    splat-studio isolate MODEL.ply    save MODEL_object.ply with only the object (no table, floor, room)
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__

DEFAULT_DATA = Path(os.environ.get("SPLAT_STUDIO_DATA", Path.home() / "SplatStudio"))


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help", "-V", "--version"):
        cmd, rest = argv[0], argv[1:]
    elif argv and not argv[0].startswith("-"):
        cmd, rest = argv[0], argv[1:]
    else:
        cmd, rest = "ui", argv                      # `splat-studio --port 9000` means the UI

    if cmd in ("-h", "--help", "help"):
        print(__doc__)
        return
    if cmd in ("-V", "--version", "version"):
        print(__version__)
        return
    if cmd == "ui":
        ap = argparse.ArgumentParser(prog="splat-studio ui", description="Open the Splat Studio web UI.")
        ap.add_argument("--data-dir", type=Path, default=DEFAULT_DATA,
                        help=f"where videos, frames and runs are kept (default: {DEFAULT_DATA})")
        ap.add_argument("--port", type=int, default=8765)
        ap.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
        a = ap.parse_args(rest)
        from .server.app import serve
        return serve(a.data_dir, a.port, not a.no_browser)
    if cmd == "run":
        from .pipeline import main as run_main
        return run_main(rest)
    if cmd == "setup":
        from .pipeline import main as run_main
        return run_main(["--setup-only", *rest])
    if cmd == "check":
        from .setup_deps import check
        items = check()
        for it in items:
            mark = "✓" if it["ok"] else ("✗" if it["required"] else "·")
            print(f"  {mark} {it['name']:<9} {it['why']}")
        missing = [i["name"] for i in items if i["required"] and not i["ok"]]
        print("\nAll set." if not missing else f"\nMissing: {', '.join(missing)}. Run: splat-studio setup")
        sys.exit(1 if missing else 0)
    if cmd == "frames":
        from .frames import main as frames_main
        return frames_main(rest)
    if cmd == "colmap":
        from .colmap import main as colmap_main
        return colmap_main(rest)
    if cmd == "inspect":
        from .splats import main_inspect
        return main_inspect(rest)
    if cmd == "clean":
        from .splats import main_clean
        return main_clean(rest)
    if cmd == "isolate":
        from .splats import main_isolate
        return main_isolate(rest)
    sys.exit(f"unknown command {cmd!r}. Try: splat-studio --help")


if __name__ == "__main__":
    main()
