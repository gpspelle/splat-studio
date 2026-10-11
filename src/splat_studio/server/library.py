"""Everything on disk in the data folder: videos, runs, splats, and website export."""
from __future__ import annotations

import io
import json
import re
import time
import zipfile
from pathlib import Path
from urllib.parse import unquote

from .. import guidance

SPLAT_EXT = {".ply", ".spz", ".splat", ".ksplat"}
VIDEO_EXT = {".mov", ".mp4", ".m4v", ".avi", ".mkv", ".webm"}
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


class DataDir:
    """The folder that holds all user data (videos, frames, workspace, jobs, imports)."""

    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()
        self.videos = self.root / "videos"
        self.jobs = self.root / "jobs"
        self.imports = self.root / "imports"
        self.frames = self.root / "frames"
        self.workspace = self.root / "workspace"
        for d in (self.root, self.videos, self.jobs, self.imports):
            d.mkdir(parents=True, exist_ok=True)

    # ---- paths
    def rel(self, p: Path | str) -> str:
        p = Path(p)
        p = p if p.is_absolute() else self.root / p
        try:
            return str(p.resolve().relative_to(self.root))
        except ValueError:
            return str(p)

    def safe_path(self, relpath: str) -> Path | None:
        """Resolve a user-supplied relative path, refusing anything outside the data folder."""
        if not relpath:
            return None
        p = (self.root / unquote(relpath)).resolve()
        try:
            p.relative_to(self.root)
        except ValueError:
            return None
        return p

    # ---- videos
    def save_video_meta(self, video: Path) -> dict:
        meta = guidance.probe_video(video)
        info = {"meta": meta, "advice": guidance.video_advice(meta)}
        video.with_name(video.name + ".meta.json").write_text(json.dumps(info))
        return info

    def video_meta(self, video: Path) -> dict:
        try:
            return json.loads(video.with_name(video.name + ".meta.json").read_text())
        except (OSError, ValueError):
            return {}

    def list_videos(self) -> list[dict]:
        out = []
        for v in sorted(self.videos.glob("*"), key=lambda v: -v.stat().st_mtime):
            if v.suffix.lower() in VIDEO_EXT and v.is_file():
                out.append({"path": self.rel(v), "name": v.name, "size": v.stat().st_size, **self.video_meta(v)})
        return out

    # ---- runs
    def frames_name(self, video: str | None = None, run_dir: str | None = None) -> str | None:
        if video:
            return Path(video).stem.replace(" ", "_")
        if run_dir:
            parts = Path(run_dir).parts
            return parts[1] if len(parts) >= 2 and parts[0] == "workspace" else None
        return None

    def thumbnail(self, video: str | None = None, run_dir: str | None = None) -> str | None:
        name = self.frames_name(video, run_dir)
        d = self.frames / name if name else None
        if d and d.is_dir():
            first = next(iter(sorted(d.glob("*.jpg"))), None)
            if first:
                return self.rel(first)
        return None

    def extraction_info(self, video: str | None = None, run_dir: str | None = None) -> dict:
        name = self.frames_name(video, run_dir)
        return guidance.load_extraction_info(self.frames / name) if name else {}

    def splats_in(self, run_dir: str) -> list[str]:
        d = self.root / run_dir / "splat"
        return [self.rel(p) for p in sorted(d.glob("*.ply"), key=lambda p: p.stat().st_mtime)] if d.is_dir() else []

    def latest_splat(self, run_dir: str) -> str | None:
        s = self.splats_in(run_dir)
        return s[-1] if s else None

    def disk_runs(self, known: set[str]) -> list[dict]:
        """Runs made from the command line (not through the UI), so the history is complete."""
        out = []
        for rd in self.workspace.glob("*/run_*"):
            r = self.rel(rd)
            if r in known or not rd.is_dir():
                continue
            s = read_summary(rd)
            result = s.get("result", "")
            status = ("running" if (rd / ".lock").exists() else "done" if result.startswith("OK")
                      else "failed" if result else "unknown")
            try:
                ts = time.mktime(time.strptime(s["started"], "%Y-%m-%d %H:%M:%S"))
            except (KeyError, ValueError):
                ts = rd.stat().st_mtime
            splats = self.splats_in(r)
            best = s.get("best", "")
            out.append({"id": "cli:" + r, "type": "cli", "source": "cli", "status": status, "created": ts,
                        "started": ts, "run_dir": r, "summary": s, "splats": splats,
                        "splat": splats[-1] if splats else None, "video": rd.parent.name,
                        "registered": best.split("  ")[-1].split(",")[0] if best else None,
                        "thumb": self.thumbnail(run_dir=r)})
        return out

    def run_dir_of(self, splat: Path) -> str | None:
        """workspace/<name>/run_x/splat/file.ply -> workspace/<name>/run_x (if it has camera poses)."""
        parts = Path(self.rel(splat)).parts
        if len(parts) >= 5 and parts[0] == "workspace" and parts[3] == "splat":
            rd = "/".join(parts[:3])
            return rd if (self.root / rd / "dense").is_dir() else None
        return None

    # ---- splat library
    def list_splats(self) -> list[dict]:
        items = []
        for p in list(self.workspace.glob("*/run_*/splat/*")) + list(self.imports.glob("*")):
            if p.suffix.lower() in SPLAT_EXT and p.is_file():
                st = p.stat()
                imported = p.parent == self.imports
                src = "imported" if imported else self.rel(p.parent.parent)
                items.append({"path": self.rel(p), "name": p.name, "size": st.st_size, "mtime": st.st_mtime,
                              "source": src, "title": splat_title(self.rel(p)),
                              "cleaned": p.stem.endswith("_clean"),
                              "isolated": p.stem.endswith("_object"),
                              "thumb": None if imported else self.thumbnail(run_dir=src)})
        return sorted(items, key=lambda i: -i["mtime"])


def splat_title(relpath: str) -> str:
    """'workspace/IMG_4821/run_x/splat/high_40000.ply' -> 'IMG_4821 · high'; imports keep their name."""
    parts = Path(relpath).parts
    if len(parts) >= 5 and parts[0] == "workspace":
        quality = re.sub(r"_\d+$", "", re.sub(r"_(clean|object)$", "", Path(parts[-1]).stem))
        return f"{parts[1]} · {quality}"
    return Path(relpath).name


def read_summary(run_dir: Path) -> dict:
    """summary.txt -> dict. Repeated keys (attempts, track breaks) become lists."""
    out: dict = {}
    f = Path(run_dir) / "summary.txt"
    if not f.exists():
        return out
    for line in f.read_text().splitlines():
        if ":" not in line:
            continue
        k, v = (x.strip() for x in line.split(":", 1))
        if k.startswith("attempt"):
            out.setdefault("attempts", []).append(v)
        elif k == "track break":
            out.setdefault("track_breaks", []).append(v)
        else:
            out.setdefault(k, v)
    return out


def _add_design_files(z: zipfile.ZipFile, static_dir: Path) -> None:
    """The design system: stylesheet and bundled fonts (with their licences)."""
    z.write(static_dir / "design.css", "design.css")
    for f in sorted((static_dir / "fonts").glob("*")):
        if f.is_file():
            z.write(f, "fonts/" + f.name, compress_type=zipfile.ZIP_STORED if f.suffix == ".woff2" else zipfile.ZIP_DEFLATED)


def _add_site_files(z: zipfile.ZipFile, page: str, static_dir: Path) -> None:
    z.write(static_dir / page, "index.html")
    z.write(static_dir / "i18n.js", "i18n.js")
    _add_design_files(z, static_dir)
    for f in (static_dir / "vendor").rglob("*"):
        if f.is_file() and not f.name.endswith(".map"):
            z.write(f, "vendor/" + str(f.relative_to(static_dir / "vendor")))


def export_compare(models: list[dict], static_dir: Path = STATIC_DIR) -> bytes:
    """A self-contained website showing two splats side by side with linked cameras.
    models: [{path, title, rotation, crop}, …] (two of them)."""
    buf = io.BytesIO()
    cfg = {"models": [], "linked": True}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        _add_site_files(z, "compare.html", static_dir)
        for i, m in enumerate(models):
            entry = {"file": f"model_{'ab'[i]}{Path(m['path']).suffix.lower()}", "title": m["title"],
                     "rotation": m.get("rotation")}
            if m.get("crop"):
                entry["crop"] = m["crop"]
            cfg["models"].append(entry)
            z.write(m["path"], entry["file"], compress_type=zipfile.ZIP_STORED)
        z.writestr("compare-config.json", json.dumps(cfg, indent=1))
        titles = " vs ".join(m["title"] for m in models)
        z.writestr("README.txt",
                   f"{titles}\n\nA self-contained website comparing two 3D splats. Upload this whole folder to any\n"
                   "static web host (GitHub Pages, Netlify, S3, your own server) and open index.html.\n"
                   "Preview locally:  python3 -m http.server   then open http://localhost:8000\n"
                   "(Opening index.html straight from disk won't work: browsers block file:// model loading.)\n")
    return buf.getvalue()


def export_site(splat: Path, title: str, rotation: list | None, static_dir: Path = STATIC_DIR,
                crop: float | None = None) -> bytes:
    """A self-contained viewer website (zip): index.html + i18n + vendor libs + the model + config."""
    buf = io.BytesIO()
    cfg = {"file": "model" + splat.suffix.lower(), "title": title, "rotation": rotation}
    if crop:
        cfg["crop"] = crop                  # only when set, so earlier exports stay identical
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(static_dir / "viewer.html", "index.html")
        z.write(static_dir / "i18n.js", "i18n.js")
        _add_design_files(z, static_dir)
        for f in (static_dir / "vendor").rglob("*"):
            if f.is_file() and not f.name.endswith(".map"):
                z.write(f, "vendor/" + str(f.relative_to(static_dir / "vendor")))
        z.writestr("viewer-config.json", json.dumps(cfg, indent=1))
        z.write(splat, cfg["file"], compress_type=zipfile.ZIP_STORED)
        z.writestr("README.txt",
                   f"{title}\n\nA self-contained 3D splat viewer. Upload this whole folder to any static web host\n"
                   "(GitHub Pages, Netlify, S3, your own server) and open index.html.\n"
                   "Preview locally:  python3 -m http.server   then open http://localhost:8000\n"
                   "(Opening index.html straight from disk won't work: browsers block file:// model loading.)\n")
    return buf.getvalue()
