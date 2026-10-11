"""HTTP server for the web UI (standard library only, bound to localhost).

Routes
  GET  /                         Studio page             GET  /viewer?file=...    splat viewer
  GET  /files/<path>             files in the data folder (Range supported, ?download=1)
  GET  /api/jobs                 runs (UI jobs + command-line runs), newest first
  GET  /api/jobs/<id>/log        terminal log of a job
  GET  /api/splats               splat library          GET  /api/videos         uploaded videos (+ advice)
  GET  /api/setup                dependency status      GET  /api/export?file=   viewer website (zip)
  GET  /compare?a=...&b=...      two splats side by side GET  /api/export-compare?a=&b=  comparison website (zip)
  POST /api/upload               raw video body, X-Filename header -> {video, advice}
  POST /api/import               raw .ply body, X-Filename header
  POST /api/jobs                 {type: video|retrain|setup|clean|isolate, ...options}
  POST /api/jobs/<id>/cancel     DELETE /api/jobs/<id>   (removes the record, never results)
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .. import __version__, setup_deps

mimetypes.add_type("font/woff2", ".woff2")       # bundled fonts (not in every system's mime table)
from .jobs import ApiError, Runner, validate_options
from .library import SPLAT_EXT, STATIC_DIR, VIDEO_EXT, DataDir, export_compare, export_site, splat_title


def make_handler(data: DataDir, runner: Runner, static_dir: Path = STATIC_DIR):
    static_dir = static_dir.resolve()

    class Handler(BaseHTTPRequestHandler):
        server_version = f"SplatStudio/{__version__}"

        def log_message(self, fmt, *args):          # quiet, except errors
            if len(args) > 1 and str(args[1])[:1] in ("4", "5") and not os.environ.get("SPLAT_STUDIO_QUIET"):
                sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        # ---- responses
        def send_json(self, obj, status=200):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def send_bytes(self, data: bytes, ctype: str, filename: str | None = None):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(data)

        def send_file(self, path: Path | None, download_name: str | None = None):
            if not path or not path.is_file():
                return self.send_error(HTTPStatus.NOT_FOUND)
            size = path.stat().st_size
            ctype = "text/javascript" if path.suffix in (".js", ".mjs") else \
                mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            start, end = 0, size - 1
            m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
            if m and size and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else size - 1
                else:
                    start = max(0, size - int(m.group(2)))
                end = min(end, size - 1)
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Cache-Control", "no-cache")
            if download_name:
                self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
            self.end_headers()
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    left -= len(chunk)

        # ---- requests
        def read_json(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return {}

        def receive_upload(self, folder: Path, allowed: set) -> Path | None:
            name = unquote(self.headers.get("X-Filename") or "")
            name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).name).strip("._") or "upload"
            if Path(name).suffix.lower() not in allowed:
                self.send_json({"error": f"unsupported file type (allowed: {', '.join(sorted(allowed))})",
                                "code": "file_type"}, 400)
                return None
            dest, i = folder / name, 2
            while dest.exists():                    # never overwrite: frames/ are keyed by the file name
                dest = folder / f"{Path(name).stem}_{i}{Path(name).suffix}"
                i += 1
            left = int(self.headers.get("Content-Length") or 0)
            if left <= 0:
                self.send_json({"error": "empty upload", "code": "empty_upload"}, 400)
                return None
            tmp = dest.with_name(dest.name + ".part")
            with open(tmp, "wb") as f:
                while left > 0:
                    chunk = self.rfile.read(min(1 << 20, left))
                    if not chunk:
                        break
                    f.write(chunk)
                    left -= len(chunk)
            if left > 0:
                tmp.unlink(missing_ok=True)
                self.send_json({"error": "upload interrupted", "code": "upload_interrupted"}, 400)
                return None
            tmp.replace(dest)
            return dest

        # ---- routes
        def do_GET(self):
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            p = url.path
            if p == "/":
                return self.send_file(static_dir / "index.html")
            if p == "/viewer":
                return self.send_file(static_dir / "viewer.html")
            if p == "/compare":
                return self.send_file(static_dir / "compare.html")
            if p.startswith("/files/"):
                f = data.safe_path(p[len("/files/"):])
                return self.send_file(f, download_name=f.name if f and q.get("download") else None)
            if p == "/api/jobs":
                return self.send_json(runner.all_views())
            m = re.match(r"^/api/jobs/([^/]+)/log$", p)
            if m:
                return self.send_json({"log": runner.log_text(m.group(1), int(q.get("lines", 300)))})
            if p == "/api/splats":
                return self.send_json(data.list_splats())
            if p == "/api/videos":
                return self.send_json(data.list_videos())
            if p == "/api/setup":
                return self.send_json({"items": setup_deps.check(), "data_dir": str(data.root), "version": __version__})
            if p == "/api/export":
                f = data.safe_path(q.get("file", ""))
                if not f or not f.is_file() or f.suffix.lower() not in SPLAT_EXT:
                    return self.send_error(HTTPStatus.NOT_FOUND)
                try:
                    rot = json.loads(q["rotation"]) if q.get("rotation") else None
                    if rot is not None and not (isinstance(rot, list) and len(rot) == 4):
                        raise ValueError
                except ValueError:
                    return self.send_json({"error": "rotation must be a JSON array of 4 numbers"}, 400)
                try:
                    crop = float(q["crop"]) if q.get("crop") else None
                    if crop is not None and not 0 < crop <= 10:
                        raise ValueError
                except ValueError:
                    return self.send_json({"error": "crop must be a number between 0 and 10"}, 400)
                title = q.get("title") or splat_title(data.rel(f))
                name = re.sub(r"[^A-Za-z0-9_-]+", "_", title).strip("_") + "_viewer.zip"
                return self.send_bytes(export_site(f, title, rot, static_dir, crop), "application/zip", name)
            if p == "/api/export-compare":
                models = []
                for k in "ab":
                    f = data.safe_path(q.get(k, ""))
                    if not f or not f.is_file() or f.suffix.lower() not in SPLAT_EXT:
                        return self.send_error(HTTPStatus.NOT_FOUND)
                    try:
                        rot = json.loads(q["r" + k]) if q.get("r" + k) else None
                        if rot is not None and not (isinstance(rot, list) and len(rot) == 4):
                            raise ValueError
                    except ValueError:
                        return self.send_json({"error": "rotation must be a JSON array of 4 numbers",
                                               "code": "rotation"}, 400)
                    try:
                        crop = float(q["c" + k]) if q.get("c" + k) else None
                        if crop is not None and not 0 < crop <= 10:
                            raise ValueError
                    except ValueError:
                        return self.send_json({"error": "crop must be a number between 0 and 10", "code": "crop"}, 400)
                    models.append({"path": f, "title": q.get("t" + k) or splat_title(data.rel(f)),
                                   "rotation": rot, "crop": crop})
                name = re.sub(r"[^A-Za-z0-9_-]+", "_", f"{models[0]['title']} vs {models[1]['title']}").strip("_")
                return self.send_bytes(export_compare(models, static_dir), "application/zip", name + "_compare.zip")
            f = (static_dir / p.lstrip("/")).resolve()          # static assets (vendor libraries)
            if f.is_file() and str(f).startswith(str(static_dir) + "/"):
                return self.send_file(f)
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self):
            p = urlparse(self.path).path
            if p == "/api/upload":
                dest = self.receive_upload(data.videos, VIDEO_EXT)
                if dest:
                    info = data.save_video_meta(dest)
                    self.send_json({"video": data.rel(dest), "name": dest.name, **info})
                return
            if p == "/api/import":
                dest = self.receive_upload(data.imports, SPLAT_EXT)
                if dest:
                    self.send_json({"path": data.rel(dest), "name": dest.name})
                return
            if p == "/api/jobs":
                body = self.read_json()
                try:
                    opts = validate_options(body)
                except ApiError as e:
                    return self.send_json({"error": str(e), "code": e.code}, 400)
                t = body.get("type", "video")
                if t == "video":
                    v = data.safe_path(body.get("video", ""))
                    if not v or not v.is_file() or v.suffix.lower() not in VIDEO_EXT:
                        return self.send_json({"error": "video not found", "code": "video_missing"}, 400)
                    job = runner.submit("video", video=data.rel(v), options=opts)
                elif t == "retrain":
                    rd = data.safe_path(body.get("run_dir", ""))
                    if not rd or not (rd / "dense").is_dir():
                        return self.send_json({"error": "that run has no camera poses to train on",
                                               "code": "no_poses"}, 400)
                    job = runner.submit("retrain", run_dir=data.rel(rd), options=opts)
                elif t == "setup":
                    job = runner.submit("setup")
                elif t in ("clean", "isolate"):
                    f = data.safe_path(body.get("splat", ""))
                    if not f or not f.is_file() or f.suffix.lower() != ".ply":
                        return self.send_json({"error": "splat not found", "code": "splat_missing"}, 400)
                    run_dir = data.run_dir_of(f)
                    job = runner.submit(t, options={"splat": data.rel(f)}, run_dir=run_dir)
                else:
                    return self.send_json({"error": "unknown job type", "code": "unknown_type"}, 400)
                return self.send_json(runner.view(job))
            m = re.match(r"^/api/jobs/([^/]+)/cancel$", p)
            if m:
                return self.send_json({"ok": runner.cancel(m.group(1))})
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_DELETE(self):
            m = re.match(r"^/api/jobs/([^/]+)$", urlparse(self.path).path)
            if m:
                return self.send_json({"ok": runner.delete(m.group(1))})
            self.send_error(HTTPStatus.NOT_FOUND)

    return Handler


class StudioServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            return                                  # the browser closed the connection: not an error
        super().handle_error(request, client_address)


def create_server(data_dir: Path, port: int = 8765, start_runner: bool = True):
    """Build (but don't start) the server. Returns (server, runner, data)."""
    data = DataDir(data_dir)
    runner = Runner(data, start=start_runner)
    srv = StudioServer(("127.0.0.1", port), make_handler(data, runner))   # local only
    return srv, runner, data


def serve(data_dir: Path, port: int = 8765, open_browser: bool = True) -> None:
    srv, runner, data = create_server(data_dir, port)
    url = f"http://localhost:{srv.server_address[1]}"
    print(f"Splat Studio {__version__} running at {url}  (Ctrl+C to stop)\nData folder: {data.root}", flush=True)
    if open_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping. Running jobs are stopped too; start them again from the UI.")
        runner.stop_all()
