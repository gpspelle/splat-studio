"""Job queue: runs the pipeline one job at a time and turns its output into live status.

Each job runs `python -m splat_studio.pipeline` under a pseudo-terminal, so every tool
prints its live progress bar (COLMAP stage and Brush), which is parsed here.
"""
from __future__ import annotations

import json
import os
import pty
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from .. import guidance
from ..colmap import pid_alive
from ..common import child_env, module_cmd
from .library import DataDir, read_summary

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]|\x1b[=>]")
QUALITIES = ("preview", "standard", "high")
DETAILS = ("auto", "low", "normal", "high")


class Job:
    FIELDS = ["id", "type", "status", "created", "started", "finished", "video", "options", "cmd",
              "run_dir", "stage", "stage_label", "step_label", "step_pct", "eta", "eta_worst", "attempt",
              "registered", "train_step", "train_total", "train_eta", "splat", "frames", "blurry",
              "track_breaks", "error", "returncode", "clean_splat", "cleaned",
              "object_splat", "isolated"]

    def __init__(self, jobs_dir: Path, **kw):
        for f in self.FIELDS:
            setattr(self, f, kw.get(f))
        self.jobs_dir = jobs_dir
        self.proc: subprocess.Popen | None = None
        self.cancel_requested = False
        self._last_save = 0.0

    @property
    def dir(self) -> Path:
        return self.jobs_dir / self.id

    def to_dict(self) -> dict:
        return {f: getattr(self, f) for f in self.FIELDS}

    def save(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_save < 1.0:
            return
        self._last_save = now
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / "job.json.tmp"
        tmp.write_text(json.dumps(self.to_dict(), indent=1))
        tmp.replace(self.dir / "job.json")

    @classmethod
    def load(cls, path: Path) -> "Job":
        return cls(path.parent.parent, **json.loads(path.read_text()))


# ---------------------------------------------------------------- output parsing

RE_STAGE = re.compile(r"==> (\d)/3 (.+)")
RE_WS = re.compile(r"==> 2/3 Camera poses \(COLMAP\): (.+)$")
RE_ATTEMPT = re.compile(r"==> Attempt (\d+)/(\d+): (.+)")
RE_COLMAP_BAR = re.compile(r"^\s*(.+?)\s+[█░]+\s+(\d+)%\s+step left (\S+) \| total \S+ \| left (~?\S+)"
                           r"(?: \(worst case (~?\S+)\))?")
RE_REGISTERED = re.compile(r"largest registers (\d+)/(\d+) frames \((\d+)%\)")
RE_BRUSH = re.compile(r"(\d+)/(\d+)\s+Steps\s+\(([^,]*),\s*([^)]*?)\s+remaining\)")
RE_SPLAT = re.compile(r"Done\. Splat: (.+\.ply)")
RE_KEPT = re.compile(r"Kept (\d+) frames(?:, moved (\d+) blurry)?")
RE_REUSE = re.compile(r"reusing (\d+) frames")
RE_TRACK = re.compile(r"^\s+(\S+\.jpg) -> (\S+\.jpg)\s+\((\d+) frames\)")
RE_CLEAN_COPY = re.compile(r"Clean copy: (.+\.ply)")
RE_CLEAN_KEPT = re.compile(r"Kept ([\d,]+) of ([\d,]+) splats")
RE_OBJECT_COPY = re.compile(r"Object copy: (.+\.ply)")
RE_STAGE_ISOLATE = re.compile(r"==> Removing the background")
STAGE_NAMES = {"0": "Setup", "1": "Frames", "2": "Camera poses", "3": "Training"}
ERROR_HINTS = ("failed", "Error", "error:", "Not good enough", "not found", "Traceback", "isn't installed",
               "could not reconstruct")


def parse_line(job: Job, line: str, rel=lambda p: p) -> None:
    """Update a job's status from one line of pipeline output."""
    line = line.rstrip()
    if not line.strip():
        return
    m = RE_STAGE.search(line)
    if m:
        job.stage = int(m.group(1))
        job.stage_label = STAGE_NAMES.get(m.group(1), m.group(2))
        job.step_label = job.step_pct = job.eta = job.eta_worst = None
    m = RE_WS.search(line)
    if m:
        job.run_dir = rel(m.group(1).strip())
    m = RE_ATTEMPT.search(line)
    if m:
        job.attempt = f"{m.group(1)}/{m.group(2)} - {m.group(3)}"
    m = RE_COLMAP_BAR.search(line)
    if m:
        job.step_label, job.step_pct = m.group(1).strip(), int(m.group(2))
        job.eta, job.eta_worst = m.group(4), m.group(5)
    m = RE_REGISTERED.search(line)
    if m:
        job.registered = f"{m.group(1)}/{m.group(2)} frames ({m.group(3)}%)"
    m = RE_BRUSH.search(line)
    if m:
        job.stage, job.stage_label = 3, "Training"
        job.train_step, job.train_total = int(m.group(1)), int(m.group(2))
        job.train_eta = m.group(4).strip()
        job.step_label = "training splat"
        job.step_pct = int(100 * job.train_step / job.train_total) if job.train_total else None
        job.eta, job.eta_worst = job.train_eta, None
    m = RE_SPLAT.search(line)
    if m:
        job.splat = rel(m.group(1).strip())
    m = RE_KEPT.search(line)
    if m:
        job.frames = int(m.group(1))
        job.blurry = int(m.group(2)) if m.group(2) else 0
    m = RE_REUSE.search(line)
    if m:
        job.frames = int(m.group(1))
    m = RE_TRACK.search(line)
    if m:
        brk = {"first": m.group(1), "last": m.group(2), "count": int(m.group(3))}
        job.track_breaks = [b for b in (job.track_breaks or []) if b["first"] != brk["first"]] + [brk]
    if RE_STAGE_ISOLATE.search(line):
        job._isolating = True
    m = RE_CLEAN_KEPT.search(line)
    if m:
        counts = {"kept": int(m.group(1).replace(",", "")), "total": int(m.group(2).replace(",", ""))}
        if job.type == "isolate" or getattr(job, "_isolating", False):
            job.isolated = counts                                  # background removal, not fog
        else:
            job.cleaned = counts
    m = RE_OBJECT_COPY.search(line)
    if m:
        job.object_splat = rel(m.group(1).strip())
        if job.type == "isolate":
            job.splat = job.object_splat
    m = RE_CLEAN_COPY.search(line)
    if m:
        job.clean_splat = rel(m.group(1).strip())
        if job.type == "clean":
            job.splat = job.clean_splat
    if any(k in line for k in ERROR_HINTS):
        job.error = line.strip()[:400]


def build_cmd(job: Job) -> list[str]:
    o = job.options or {}
    if job.type == "setup":
        return module_cmd("pipeline", "--setup-only")
    if job.type == "clean":
        args = ["clean", o["splat"]] + (["--run", job.run_dir] if job.run_dir else [])
        return [sys.executable, "-u", "-m", "splat_studio", *args]
    if job.type == "isolate":
        args = ["isolate", o["splat"]] + (["--run", job.run_dir] if job.run_dir else [])
        return [sys.executable, "-u", "-m", "splat_studio", *args]
    if job.type == "retrain":
        args = ["--train-only", job.run_dir]
    else:
        args = [job.video]
        if o.get("frames"):
            args += ["--frames", str(int(o["frames"]))]
        for k in ("start", "end"):
            if o.get(k):
                args += [f"--{k}", str(o[k])]
        if o.get("reextract"):
            args.append("--re-extract")
    args += ["--quality", o.get("quality", "standard"), "--detail", o.get("detail", "auto")]
    if o.get("eval"):
        args.append("--eval")
    if o.get("clean"):
        args.append("--clean")
    if o.get("remove_background"):
        args.append("--remove-background")
    extra = (o.get("brush_args") or "").split()
    if extra:
        args += ["--", *extra]
    return module_cmd("pipeline", *args)


class ApiError(ValueError):
    """A request error with a stable code the UI can translate (see static/i18n.js: err.<code>)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def validate_options(body: dict) -> dict:
    opts = {k: body.get(k) for k in ("quality", "detail", "frames", "start", "end", "eval", "reextract",
                                     "brush_args", "clean", "remove_background")
            if body.get(k) not in (None, "", False)}
    if opts.get("quality", "standard") not in QUALITIES:
        raise ApiError("quality", "quality must be preview, standard or high")
    if opts.get("detail", "auto") not in DETAILS:
        raise ApiError("detail", "detail must be auto, low, normal or high")
    if "frames" in opts:
        try:
            ok = 20 <= int(opts["frames"]) <= 2000
        except (TypeError, ValueError):
            ok = False
        if not ok:
            raise ApiError("frames", "frames must be between 20 and 2000")
    for k in ("start", "end"):
        if k in opts and not re.fullmatch(r"\d+(:\d{1,2}){0,2}(\.\d+)?", str(opts[k])):
            raise ApiError("trim", f"{k} must look like 1:05 or 65")
    if "brush_args" in opts and not re.fullmatch(r"[\w\s.,=+-]*", str(opts["brush_args"])):
        raise ApiError("brush_args", "extra Brush options may only contain letters, numbers, spaces and . , = + -")
    return opts


# ---------------------------------------------------------------- runner

class Runner:
    def __init__(self, data: DataDir, start: bool = True):
        self.data = data
        self.jobs: dict[str, Job] = {}
        self.cond = threading.Condition()
        for p in sorted(data.jobs.glob("*/job.json")):
            try:
                j = Job.load(p)
            except (OSError, ValueError, TypeError):
                continue
            if j.status == "running":                 # the server stopped mid-run
                j.status, j.error = "interrupted", "Splat Studio was stopped while this job ran"
                j.save(force=True)
            self.jobs[j.id] = j
        if start:
            threading.Thread(target=self._loop, daemon=True).start()

    # ---- queue operations
    def submit(self, type_: str, video: str | None = None, options: dict | None = None,
               run_dir: str | None = None) -> Job:
        jid = time.strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:4]
        job = Job(self.data.jobs, id=jid, type=type_, status="queued", created=time.time(), video=video,
                  options=options or {}, run_dir=run_dir)
        job.cmd = build_cmd(job)
        job.save(force=True)
        with self.cond:
            self.jobs[jid] = job
            self.cond.notify()
        return job

    def cancel(self, jid: str) -> bool:
        job = self.jobs.get(jid)
        if not job:
            return False
        job.cancel_requested = True
        if job.status == "queued":
            job.status, job.finished = "cancelled", time.time()
            job.save(force=True)
        elif job.proc and job.proc.poll() is None:
            try:
                os.killpg(job.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        return True

    def delete(self, jid: str) -> bool:
        job = self.jobs.get(jid)
        if not job or job.status in ("running", "queued"):
            return False
        shutil.rmtree(job.dir, ignore_errors=True)     # only the job record + log, never results
        del self.jobs[jid]
        return True

    def splat_in_use(self, splat: str) -> bool:
        """A queued or running job reads this splat, or writes into its run folder (UI or command line)."""
        parts = splat.split("/")
        run_dir = "/".join(parts[:3]) if parts[0] == "workspace" else None
        lock = self.data.root / run_dir / ".lock" if run_dir else None
        try:
            if lock and pid_alive(int(lock.read_text().strip())):
                return True
        except (OSError, ValueError):
            pass
        return any(j.status in ("queued", "running") and
                   ((j.options or {}).get("splat") == splat or (run_dir and j.run_dir == run_dir))
                   for j in list(self.jobs.values()))

    def stop_all(self) -> None:
        for j in self.jobs.values():
            if j.proc and j.proc.poll() is None:
                try:
                    os.killpg(j.proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass

    # ---- views
    def view(self, j: Job) -> dict:
        d = j.to_dict()
        d["source"] = "ui"
        if j.run_dir:
            d["summary"] = read_summary(self.data.root / j.run_dir)
            d["splats"] = self.data.splats_in(j.run_dir)
        info = self.data.extraction_info(j.video, j.run_dir)
        d["track_breaks"] = guidance.track_break_times(j.track_breaks or [], info.get("fps"), info.get("start", 0.0))
        d["thumb"] = self.data.thumbnail(j.video, j.run_dir)
        d["advice"] = guidance.run_advice(d) if j.status in ("done", "failed") else []
        return d

    def all_views(self) -> list[dict]:
        views = [self.view(j) for j in list(self.jobs.values())]
        # only jobs that *created* a run folder claim it; re-train jobs point at existing runs
        known = {v["run_dir"] for v in views if v.get("run_dir") and v.get("type") == "video"}
        items = views + self.data.disk_runs(known)
        return sorted(items, key=lambda v: -(v.get("created") or 0))

    def log_text(self, jid: str, lines: int = 300) -> str:
        job = self.jobs.get(jid)
        f = job.dir / "output.log" if job else None
        if not f or not f.exists():
            return ""
        text = ANSI.sub("", f.read_bytes()[-200_000:].decode("utf-8", "replace"))
        out = [raw.split("\r")[-1] if "\r" in raw.rstrip("\r") else raw.rstrip("\r") for raw in text.split("\n")]
        return "\n".join(out[-lines:])

    # ---- execution
    def _next(self) -> Job | None:
        queued = [j for j in self.jobs.values() if j.status == "queued"]
        return min(queued, key=lambda j: j.created) if queued else None

    def _loop(self) -> None:
        while True:
            with self.cond:
                job = self._next()
                while job is None:
                    self.cond.wait()
                    job = self._next()
            self.run_job(job)

    def run_job(self, job: Job) -> None:
        job.status, job.started, job.error = "running", time.time(), None
        job.save(force=True)
        master, slave = pty.openpty()
        env = child_env({"TERM": "xterm-256color", "COLUMNS": "160", "SPLAT_STUDIO_NONINTERACTIVE": "1"})
        try:
            job.proc = subprocess.Popen(job.cmd, cwd=self.data.root, stdin=slave, stdout=slave, stderr=slave,
                                        env=env, start_new_session=True, close_fds=True)
        except OSError as e:
            os.close(slave)
            os.close(master)
            job.status, job.error, job.finished = "failed", str(e), time.time()
            job.save(force=True)
            return
        os.close(slave)
        buf = ""
        with open(job.dir / "output.log", "ab") as log:
            while True:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                log.write(chunk)
                log.flush()
                buf += ANSI.sub("", chunk.decode("utf-8", "replace"))
                parts = re.split(r"[\r\n]", buf)
                buf = parts.pop()                     # keep the unfinished line
                for line in parts:
                    parse_line(job, line, self.data.rel)
                if buf:
                    parse_line(job, buf, self.data.rel)   # progress bars redraw without a newline
                job.save()
        os.close(master)
        job.returncode = job.proc.wait()
        job.finished = time.time()
        if job.cancel_requested:
            job.status = "cancelled"
        elif job.returncode == 0:
            job.status, job.error, job.step_pct = "done", None, 100
            if job.type in ("video", "retrain") and not job.splat and job.run_dir:
                job.splat = self.data.latest_splat(job.run_dir)
        else:
            job.status = "failed"
            job.error = job.error or f"exited with code {job.returncode}"
        job.save(force=True)
