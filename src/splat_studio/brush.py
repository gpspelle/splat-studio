"""Stage 3 helpers: Brush training settings, and finding / building the Brush binary.

Brush (https://github.com/ArthurBrussee/brush) trains Gaussian splats on the GPU
(Metal on Apple Silicon). The option names below come from Brush's source code
(crates/brush-train, brush-dataset, brush-process at commit 1388f74).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .common import TOOLS_DIR, say

BRUSH_REPO = "https://github.com/ArthurBrussee/brush.git"
BRUSH_REF = "1388f74c6fe0236f68ee4915564bf00e9d2e3747"   # tested commit; override with SPLAT_STUDIO_BRUSH_REF
MIN_RUST = (1, 88)

QUALITY = {
    #           steps per image, min/max steps, max image size, SH degree, max splats
    "preview":  dict(per_img=30,  lo=3000,  hi=8000,  res=1024, sh=2, splats=500_000),
    "standard": dict(per_img=120, lo=10000, hi=30000, res=1600, sh=3, splats=2_000_000),
    "high":     dict(per_img=160, lo=20000, hi=50000, res=1920, sh=3, splats=4_000_000),
}
# Lower threshold = splats grow more readily = more fine detail (and bigger files).
DETAIL = {"low": 0.0035, "normal": 0.0025, "high": 0.0018}   # normal = Brush's default


def auto_detail(n_images: int, n_points: int | None) -> str:
    """Judge detail from COLMAP's sparse points per image (a proxy for texture).

    Never "low": its threshold is above Brush's own default (0.0025), and few sparse points
    usually means smooth or slightly blurry footage, which needs more growth, not less.
    "low" stays available as an explicit choice for smaller files."""
    if not n_points or not n_images:
        return "normal"
    return "high" if n_points / n_images > 600 else "normal"


def choose_brush_params(n_images: int, n_points: int | None, quality: str = "standard",
                        detail: str = "auto", image_size: int | None = None) -> tuple[dict, str]:
    """Brush options from the size and complexity of the reconstruction.

    - Steps scale with the image count (~120 views per image at standard quality), clamped.
    - Splat growth stops halfway (Brush's own default ratio, 15k of 30k).
    - refine-every ~ "the number of images it takes to cover your scene" (Brush docs).
    - Detail "auto" uses sparse points per image.
    """
    if quality not in QUALITY:
        raise ValueError(f"unknown quality {quality!r}")
    q = QUALITY[quality]
    steps = max(q["lo"], min(q["hi"], n_images * q["per_img"]))
    steps = int(round(steps / 1000) * 1000)
    if detail == "auto":
        detail = auto_detail(n_images, n_points)
    if detail not in DETAIL:
        raise ValueError(f"unknown detail {detail!r}")
    res = q["res"] if image_size is None else min(q["res"], image_size)
    params = {
        "--total-train-iters": steps,
        "--growth-stop-iter": steps // 2,
        "--refine-every": max(100, min(300, n_images)),
        "--growth-grad-threshold": DETAIL[detail],
        "--max-resolution": res,
        "--sh-degree": q["sh"],
        "--max-splats": q["splats"],
        "--export-every": steps,          # only the final result (Brush always exports the last step)
    }
    why = (f"{n_images} images, {n_points if n_points else '?'} sparse points "
           f"-> {quality} quality, {detail} detail, {steps} steps")
    return params, why


# ---------------------------------------------------------------- finding / building Brush

def brush_repo_candidates() -> list[Path]:
    seen, out = set(), []
    for d in [TOOLS_DIR / "brush", Path.cwd() / "brush", Path.home() / "3d" / "brush"]:
        d = d.resolve()
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def is_brush_repo(d: Path) -> bool:
    return (d / "Cargo.toml").is_file() and (d / "apps" / "brush-app").is_dir()


def find_brush() -> Path | None:
    """An existing brush binary: $SPLAT_STUDIO_BRUSH, PATH, or a built checkout."""
    env = os.environ.get("SPLAT_STUDIO_BRUSH")
    if env and Path(env).is_file():
        return Path(env)
    on_path = shutil.which("brush")
    if on_path:
        return Path(on_path)
    for repo in brush_repo_candidates():
        binary = repo / "target" / "release" / "brush"
        if binary.is_file():
            return binary
    return None


def find_cargo() -> str | None:
    c = shutil.which("cargo") or str(Path.home() / ".cargo" / "bin" / "cargo")
    return c if Path(c).is_file() else None


def rust_version(cargo: str) -> tuple[int, int] | None:
    rustc = Path(cargo).with_name("rustc")
    r = subprocess.run([str(rustc) if rustc.is_file() else "rustc", "--version"], capture_output=True, text=True)
    m = re.search(r"(\d+)\.(\d+)", r.stdout)
    return (int(m.group(1)), int(m.group(2))) if m else None


def ensure_brush(explicit: str | None = None, dry: bool = False) -> Path:
    """Return the brush binary, cloning and building it first if needed."""
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            sys.exit(f"--brush {p}: file not found")
        return p
    found = find_brush()
    if found:
        return found

    repo = next((r for r in brush_repo_candidates() if is_brush_repo(r)), None)
    if dry:
        say(f"    (dry run: Brush is not built yet; it would be built in {repo or TOOLS_DIR / 'brush'})")
        return Path("brush")
    cargo = find_cargo()
    if cargo is None:
        sys.exit("Brush isn't built and Rust isn't installed. Run the setup first:  ./splat-studio setup")
    ver = rust_version(cargo)
    if ver and ver < MIN_RUST:
        sys.exit(f"Brush needs Rust {MIN_RUST[0]}.{MIN_RUST[1]}+, you have {ver[0]}.{ver[1]}. Run: rustup update")
    if repo is None:
        repo = TOOLS_DIR / "brush"
        if repo.exists() and any(repo.iterdir()):
            sys.exit(f"{repo} exists but isn't the Brush source - move it away or set SPLAT_STUDIO_BRUSH")
        repo.parent.mkdir(parents=True, exist_ok=True)
        say(f"==> Downloading Brush to {repo}")
        if subprocess.run(["git", "clone", BRUSH_REPO, str(repo)]).returncode != 0:
            sys.exit("git clone failed - check your internet connection")
        ref = os.environ.get("SPLAT_STUDIO_BRUSH_REF", BRUSH_REF)
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", ref])
    say(f"==> Building Brush in {repo} (one time only, ~5-15 minutes)")
    if subprocess.run([cargo, "build", "--release", "-p", "brush-app"], cwd=repo).returncode != 0:
        sys.exit("Building Brush failed - see the errors above. If they mention Xcode or a linker,\n"
                 "run `xcode-select --install` once and try again.")
    binary = repo / "target" / "release" / "brush"
    if not binary.is_file():
        sys.exit(f"Build finished but {binary} is missing")
    say(f"    built: {binary}")
    return binary


# ---------------------------------------------------------------- reading the reconstruction

def model_stats(model_dir: Path) -> tuple[int | None, int | None]:
    """(registered images, sparse points) from `colmap model_analyzer`."""
    r = subprocess.run(["colmap", "model_analyzer", "--path", str(model_dir)], capture_output=True, text=True)
    return parse_model_stats(r.stdout + r.stderr)


def parse_model_stats(text: str) -> tuple[int | None, int | None]:
    imgs = re.search(r"Registered images:\s*(\d+)", text)
    pts = re.search(r"Points:\s*(\d+)", text)
    return (int(imgs.group(1)) if imgs else None, int(pts.group(1)) if pts else None)


def image_size(dense: Path) -> int | None:
    """Longest side of the training images (sips on macOS, ffprobe elsewhere)."""
    imgs = sorted((dense / "images").glob("*.jpg"))
    if not imgs:
        return None
    if shutil.which("sips"):
        r = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(imgs[0])],
                           capture_output=True, text=True)
        nums = [int(x) for x in re.findall(r"pixel(?:Width|Height):\s*(\d+)", r.stdout)]
    elif shutil.which("ffprobe"):
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height", "-of", "csv=p=0", str(imgs[0])], capture_output=True, text=True)
        nums = [int(x) for x in re.findall(r"\d+", r.stdout)]
    else:
        return None
    return max(nums) if nums else None
