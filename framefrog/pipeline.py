"""The orchestrator: run stages in order, keep the manifest honest.

A job is a directory:

    outputs/jobs/fj_ab12cd34/
        00_input.mp4          (source, copied or downloaded)
        01_audio_clean.mp4
        02_scenes.json
        03_graded.mp4
        04_interpolated.mp4
        05_master.mp4 / 05_proxy_720p.mp4 / 05_poster.jpg
        manifest.json

Everything in that directory is a deliverable or audit trail. If a stage
fails, the manifest records where it stopped, the FFmpeg tail, and what had
already been produced — partial work is never silently thrown away.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import traceback
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from framefrog import ffmpeg, ops
from framefrog.stages import STAGES, STAGE_NAMES
from framefrog.storage import is_remote, resolve_input
from framefrog.types import JobRequest, StageResult, new_job_id

ProgressFn = Callable[[str, Dict], None]

VIDEO_PRODUCING = {"audio_cleanup", "color_grade", "interpolate", "deliver"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _public_options(options: Dict) -> Dict:
    """Options as recorded in the manifest — secrets replaced, never stored."""
    clean = dict(options or {})
    if clean.get("webhook_secret"):
        clean["webhook_secret"] = "***"
    return clean


def run_job(req: JobRequest, progress: Optional[ProgressFn] = None, job_id: Optional[str] = None) -> Dict:
    """Execute a job and return its manifest (also written to disk)."""
    req.validate(STAGE_NAMES)
    job_id = job_id or new_job_id()

    root = req.output_dir or tempfile.mkdtemp(prefix="framefrog_")
    job_dir = os.path.join(root, job_id)
    workdir = os.path.join(job_dir, "work")
    os.makedirs(workdir, exist_ok=True)

    started = time.time()
    manifest: Dict = {
        "job_id": job_id,
        "status": "running",
        "started_at": _now(),
        "request": {"input_uri": req.input_uri, "stages": req.stages, "options": _public_options(req.options)},
        "stages": [],
        "artifacts": {},
        "workdir": workdir,
    }

    def emit(status: str, extra: Optional[Dict] = None) -> None:
        if progress:
            progress(status, {**manifest, **(extra or {})})

    try:
        if is_remote(req.input_uri):
            current = resolve_input(req.input_uri, workdir)
            manifest["artifacts"]["source"] = current
        else:
            current = os.path.abspath(req.input_uri)
            if not os.path.exists(current):
                raise FileNotFoundError(f"input not found: {current}")
            local_copy = os.path.join(workdir, "00_input" + os.path.splitext(current)[1])
            shutil.copyfile(current, local_copy)
            current = local_copy
            manifest["artifacts"]["source"] = current

        src_info = ffmpeg.probe(current)
        manifest["input"] = {
            "duration_s": round(src_info.duration_s, 3),
            "width": src_info.width,
            "height": src_info.height,
            "fps": round(src_info.fps, 3),
            "has_audio": src_info.has_audio,
            "video_codec": src_info.v_codec,
            "audio_codec": src_info.a_codec,
            "size_bytes": src_info.size_bytes,
        }

        emit("running")
        for name in req.stages:
            stage_fn = STAGES[name]
            stage_started = time.time()
            emit(f"stage:{name}")
            try:
                result: StageResult = stage_fn(current, workdir, req.options)
            except Exception as exc:  # stage failure -> record and stop
                manifest["stages"].append({
                    "stage": name,
                    "status": "failed",
                    "duration_s": round(time.time() - stage_started, 3),
                    "error": str(exc),
                    "traceback": traceback.format_exc(limit=3),
                })
                raise
            result.duration_s = round(time.time() - stage_started, 3)
            manifest["stages"].append({
                "stage": result.stage,
                "status": "completed",
                "duration_s": result.duration_s,
                "output": result.output_uri,
                "meta": result.meta,
            })
            if name in VIDEO_PRODUCING:
                current = result.output_uri
            if name == "scene_detect":
                manifest["artifacts"]["scenes"] = result.meta.get("scenes_path")
            if name == "deliver":
                manifest["artifacts"].update({
                    "master": result.meta.get("master"),
                    "proxy": result.meta.get("proxy"),
                    "poster": result.meta.get("poster"),
                })

        manifest["status"] = "completed"
        manifest["final_output"] = current
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        manifest["final_output"] = locals().get("current")
    finally:
        manifest["finished_at"] = _now()
        manifest["elapsed_s"] = round(time.time() - started, 3)

    manifest["cost"] = ops.compute_costs(
        manifest["stages"],
        gpu_hourly_usd=req.options.get("gpu_hourly_usd"),
        cpu_hourly_usd=req.options.get("cpu_hourly_usd"),
    )

    webhook_url = req.options.get("webhook_url")
    if webhook_url:
        manifest["webhook"] = ops.notify_manifest(
            manifest,
            webhook_url,
            secret=req.options.get("webhook_secret"),
        )

    manifest_path = os.path.join(job_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, default=str)
    manifest["manifest_path"] = manifest_path
    emit(manifest["status"])
    return manifest


def describe() -> Dict:
    """Machine-readable capability list for /health and the marketing site."""
    from framefrog.models import model_report

    return {
        "name": "framefrog-pipeline",
        "version": __import__("framefrog").__version__,
        "ffmpeg": ffmpeg.has_ffmpeg(),
        "stages": STAGE_NAMES,
        "models": model_report(),
        "ops": ops.capabilities(),
    }
