"""Checking and installing the tools the pipeline needs.

macOS: installs what's missing with Homebrew (ffmpeg, COLMAP), rustup (Rust) and pip
(OpenCV), then clones and builds Brush. Other systems: reports what's missing.

    ./splat-studio setup            # interactive: may ask for your Mac password (Homebrew)
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import brush
from .common import say

BREW_INSTALL = '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
RUSTUP_INSTALL = "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y"
BREW_PACKAGES = {"ffmpeg": ["ffmpeg", "ffprobe"], "colmap": ["colmap"]}   # package -> commands it provides


def add_to_path(d: Path) -> None:
    if d.is_dir() and str(d) not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = f"{d}{os.pathsep}{os.environ.get('PATH', '')}"   # inherited by later steps


def find_brew() -> str | None:
    for c in [shutil.which("brew"), "/opt/homebrew/bin/brew", "/usr/local/bin/brew"]:
        if c and Path(c).is_file():
            add_to_path(Path(c).parent)
            return c
    return None


def extend_path() -> None:
    """Tools installed earlier may not be on this process's PATH yet."""
    add_to_path(Path.home() / ".cargo" / "bin")
    add_to_path(Path("/Applications/COLMAP.app/Contents/MacOS"))
    find_brew()


def has_cv2() -> bool:
    return subprocess.run([sys.executable, "-c", "import cv2"], capture_output=True).returncode == 0


def check() -> list[dict]:
    """Status of every dependency, without installing anything (used by the web UI)."""
    extend_path()
    items = [
        {"name": "ffmpeg", "ok": bool(shutil.which("ffmpeg") and shutil.which("ffprobe")), "required": True,
         "why": "reads the video and extracts frames"},
        {"name": "COLMAP", "ok": bool(shutil.which("colmap")), "required": True,
         "why": "works out where the camera was for each frame"},
        {"name": "Brush", "ok": brush.find_brush() is not None, "required": True,
         "why": "trains the splat on the GPU (built from source on first setup)"},
        {"name": "Rust", "ok": brush.find_cargo() is not None, "required": False,
         "why": "only needed to build Brush"},
        {"name": "OpenCV", "ok": has_cv2(), "required": False, "why": "drops blurry frames (optional)"},
    ]
    if sys.platform == "darwin":
        items.insert(0, {"name": "Homebrew", "ok": find_brew() is not None, "required": False,
                         "why": "installs ffmpeg and COLMAP"})
    return items


def sh(cmd: str, dry: bool) -> int:
    say(f"    $ {cmd}")
    return 0 if dry else subprocess.run(cmd, shell=True).returncode


def ensure_dependencies(dry: bool = False) -> None:
    """Install whatever is missing (macOS). Set SPLAT_STUDIO_NONINTERACTIVE=1 (the web UI
    does) to fail with instructions instead of starting steps that ask for a password."""
    say("==> 0/3 Checking dependencies")
    extend_path()
    noninteractive = bool(os.environ.get("SPLAT_STUDIO_NONINTERACTIVE"))

    if sys.platform != "darwin":
        missing = [c for cmds in BREW_PACKAGES.values() for c in cmds if not shutil.which(c)]
        if not (brush.find_brush() or (shutil.which("cargo") and shutil.which("git"))):
            missing.append("brush (or cargo + git to build it)")
        if missing:
            sys.exit(f"Missing: {', '.join(missing)}. Automatic install is only implemented for macOS.\n"
                     "Ubuntu/Debian: sudo apt install ffmpeg colmap git build-essential, then\n"
                     f"  {RUSTUP_INSTALL}")
        say("    all tools present")
        return

    # 1. Xcode command line tools: git, compiler and linker (Rust needs them to build Brush)
    if subprocess.run(["xcode-select", "-p"], capture_output=True).returncode != 0:
        say("    Xcode command line tools missing - a macOS install window will open; click Install.")
        if not dry:
            subprocess.run(["xcode-select", "--install"])
            say("    waiting for the install to finish (checking every 15 s)...")
            for _ in range(240):
                time.sleep(15)
                if subprocess.run(["xcode-select", "-p"], capture_output=True).returncode == 0:
                    break
            else:
                sys.exit("Xcode command line tools still not installed - finish the install and re-run.")
        say("    Xcode command line tools: installed")

    # 2. Homebrew (its installer asks for the Mac password)
    brew_bin = find_brew()
    if brew_bin is None:
        if noninteractive:
            sys.exit("Homebrew isn't installed, and installing it asks for your Mac password.\n"
                     "Open Terminal in the splat-studio folder and run:  ./splat-studio setup")
        say("    Homebrew missing - installing it (it will ask for your Mac password)")
        if sh(BREW_INSTALL, dry) != 0:
            sys.exit("Homebrew install failed - see https://brew.sh")
        brew_bin = find_brew() or "brew"

    # 3. ffmpeg + COLMAP
    for pkg, cmds in BREW_PACKAGES.items():
        if all(shutil.which(c) for c in cmds):
            continue
        say(f"    {pkg} missing - installing with Homebrew")
        if sh(f"{brew_bin} install {pkg}", dry) != 0:
            sys.exit(f"brew install {pkg} failed - see the errors above")
    if not dry:
        still = [c for cmds in BREW_PACKAGES.values() for c in cmds if not shutil.which(c)]
        if still:
            sys.exit(f"Installed, but still not found on PATH: {', '.join(still)}")

    # 4. Rust (to build Brush) - only if Brush isn't built yet
    if not brush.find_brush():
        if not brush.find_cargo():
            say("    Rust missing - installing it with rustup")
            if sh(RUSTUP_INSTALL, dry) != 0:
                sys.exit("Rust install failed - see https://rustup.rs")
            add_to_path(Path.home() / ".cargo" / "bin")
        cargo = brush.find_cargo()
        if cargo and not dry:
            ver = brush.rust_version(cargo)
            if ver and ver < brush.MIN_RUST:
                say(f"    Rust {ver[0]}.{ver[1]} is too old for Brush - updating")
                rustup = Path(cargo).with_name("rustup")
                if sh(f"{rustup if rustup.is_file() else 'rustup'} update stable", dry) != 0:
                    sys.exit("rustup update failed")

    # 5. OpenCV for the blur filter (optional)
    if not has_cv2():
        say("    OpenCV missing (used to drop blurry frames) - installing")
        py = shlex.quote(sys.executable)
        ok = (sh(f"{py} -m pip install --user --quiet opencv-python-headless", dry) == 0 or
              sh(f"{py} -m pip install --user --quiet --break-system-packages opencv-python-headless", dry) == 0)
        if not ok:
            say("    (couldn't install OpenCV - continuing; blurry frames won't be filtered)")
    say("    all dependencies present")
