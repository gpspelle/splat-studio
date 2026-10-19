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


def in_virtualenv() -> bool:
    """Running from a venv (or virtualenv): pip installs into it, and --user is refused there."""
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix) or hasattr(sys, "real_prefix")


def cv2_install_commands(python: str = sys.executable) -> list[str]:
    """pip commands to try in turn for OpenCV: straight into a virtualenv, else into the user's
    site-packages (with --break-system-packages for a Homebrew Python that refuses plain --user)."""
    py = shlex.quote(python)
    if in_virtualenv():
        return [f"{py} -m pip install --quiet opencv-python-headless"]
    return [f"{py} -m pip install --user --quiet opencv-python-headless",
            f"{py} -m pip install --user --quiet --break-system-packages opencv-python-headless"]


def has_sam2() -> bool:
    from .masks import sam2_python
    return sam2_python() is not None


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
        {"name": "SAM 2", "ok": has_sam2(), "required": False,
         "why": "Choose what to keep: follows the object through the video (optional, ~1 GB)"},
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
        ok = any(sh(cmd, dry) == 0 for cmd in cv2_install_commands())
        if not ok:
            say("    (couldn't install OpenCV - continuing; blurry frames won't be filtered)")
    say("    all dependencies present")


# ---------------------------------------------------------------- optional: SAM 2 (Choose what to keep)

def find_python310() -> str | None:
    """A Python 3.10+ for the SAM 2 environment (the app itself runs on 3.9+)."""
    extend_path()
    for name in ("python3.13", "python3.12", "python3.11", "python3.10", "python3"):
        for d in ("/opt/homebrew/bin", "/usr/local/bin", None):
            exe = str(Path(d) / name) if d else shutil.which(name)
            if not exe or not Path(exe).exists():
                continue
            r = subprocess.run([exe, "-c", "import sys; print(sys.version_info >= (3, 10))"],
                               capture_output=True, text=True)
            if r.stdout.strip() == "True":
                return exe
    return None


def install_masking(dry: bool = False) -> None:
    """Install SAM 2 in its own environment (~/.splat-studio/sam2): PyTorch, the sam2 package
    (pinned) and the 'small' 2.1 checkpoint (~185 MB). About 1 GB in total."""
    from . import masks
    say("==> Installing the selection tool (SAM 2) for 'Choose what to keep'")
    py = find_python310()
    if py is None:
        brew_bin = find_brew()
        if sys.platform == "darwin" and brew_bin:
            say("    Python 3.10+ missing - installing python@3.12 with Homebrew")
            if sh(f"{brew_bin} install python@3.12", dry) != 0:
                sys.exit("brew install python@3.12 failed - see the errors above")
            py = find_python310() or "/opt/homebrew/bin/python3.12"
        else:
            sys.exit("SAM 2 needs Python 3.10 or newer. Install it, then run: splat-studio setup --masking")
    root = masks.SAM2_DIR
    venv = root / "venv"
    vpy = venv / "bin" / "python"
    if not vpy.exists():
        say(f"    creating {venv} with {py}")
        if sh(f"{shlex.quote(py)} -m venv {shlex.quote(str(venv))}", dry) != 0:
            sys.exit("couldn't create the SAM 2 environment")
    pip = f"{shlex.quote(str(vpy))} -m pip install --upgrade"
    steps = [
        (f"{pip} pip", "pip"),
        (f"{pip} torch torchvision", "PyTorch (the largest download)"),
        (f"SAM2_BUILD_CUDA=0 {pip} 'git+{masks.SAM2_REPO}@{masks.SAM2_REF}'", "SAM 2"),
    ]
    for cmd, what in steps:
        say(f"    installing {what}")
        if sh(cmd, dry) != 0:
            sys.exit(f"installing {what} failed - see the errors above")
    ckpt = root / masks.SAM2_CHECKPOINT
    if not ckpt.exists():
        say(f"    downloading the SAM 2.1 model ({masks.SAM2_CHECKPOINT}, ~185 MB)")
        if not dry:
            import urllib.request
            tmp = ckpt.with_name(ckpt.name + ".part")
            try:
                urllib.request.urlretrieve(masks.SAM2_URL, tmp)
            except OSError as e:
                sys.exit(f"download failed: {e}\nGet it by hand from {masks.SAM2_URL} into {root}")
            tmp.replace(ckpt)
    if not dry:
        r = subprocess.run([str(vpy), "-c", "import torch, sam2; print('mps' if torch.backends.mps.is_available() else 'cpu')"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit("SAM 2 installed but doesn't import:\n" + r.stderr[-1500:])
        say(f"    SAM 2 ready (runs on {r.stdout.strip() or 'cpu'})")
    say("\nSetup complete: Choose what to keep is ready.")
