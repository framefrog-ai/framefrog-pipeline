# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The orchestrator: run stages in order, keep the manifest honest.

A job is a directory:

    WORK_DIR/jobs/<job_id>/
        work/00_input.mp4       (source, copied or downloaded)
        work/01_audio_clean.mp4
        work/02_scenes.json
        work/03_graded.mp4
        work/04_interpolated.mp4
        work/05_master.mp4 / 05_proxy_720p.mp4 / 05_poster.jpg
        manifest.json

Everything in that directory is either a deliverable or an audit trail. If a
stage fails, the manifest records where it stopped, what had already been
produced, and the exact error — partial work is never silently thrown away
and a failed stage is never reported as completed.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.core.config import Settings, get_settings
from app.core.errors import CapabilityError, FrameFrogError
from app.core.logging import get_logger, log_event
from app.core.security import parse_input_uri
from app.jobs.models import DEFAULT_STAGES, JobRequest, new_job_id, utc_now
from app.media import validation
from app.media.ffprobe import MediaInfo
from app.stages import STAGE_NAMES, STAGES
from app.stages.base import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PENDING_GPU_VALIDATION,
    STATUS_SKIPPED,
    StageResult,
)

log = get_logger("pipeline")

ProgressFn = Callable[[str, dict[str, Any]], None]

#: Stages that replace the video stream for the stages that follow.
VIDEO_PRODUCING = {"audio_cleanup", "color_grade", "interpolate", "upscale", "deliver"}

#: Stages whose GPU path has not been validated on real hardware yet.
GPU_PENDING_STAGES = {"interpolate", "rotoscope", "upscale"}


def safe_error(exc: BaseException) -> str:
    """A client-safe one-line summary of a failure.

    Full detail (FFmpeg stderr, tracebacks, file system paths) stays in the
    internal log and in the on-disk manifest; the API only ever sees this
    summary.
    """
    raw = str(exc).strip()
    first_line = raw.splitlines()[0] if raw else ""
    text = f"{type(exc).__name__}: {first_line}" if first_line else type(exc).__name__
    return text[:300]


def _public_options(options: dict[str, Any]) -> dict[str, Any]:
    """Options as recorded in the manifest — secrets replaced, never stored."""
    clean = {key: value for key, value in (options or {}).items() if not key.startswith("_")}
    for key in ("webhook_secret", "api_key", "s3_secret_access_key"):
        if clean.get(key):
            clean[key] = "***"
    return clean


def _job_paths(settings: Settings, job_id: str, output_dir: str | None = None) -> tuple[Path, Path]:
    """The job directory and its work directory.

    The job id is always part of the path: two jobs never share a directory,
    even when they write to the same output root.
    """
    root = Path(output_dir) if output_dir else (settings.work_dir_resolved / "jobs")
    job_root = root / job_id
    workdir = job_root / "work"
    workdir.mkdir(parents=True, exist_ok=True)
    return job_root, workdir


def run_job(
    request: JobRequest,
    *,
    settings: Settings | None = None,
    storage: Any | None = None,
    job_id: str | None = None,
    progress: ProgressFn | None = None,
    cancel_check: Callable[[], bool] | None = None,
    keep_workdir: bool = False,
) -> dict[str, Any]:
    """Execute a job and return its manifest.

    The manifest is the single source of truth for what happened: the request,
    the input metadata, every stage with its timing and status, the produced
    outputs, and the error when something went wrong.
    """
    settings = settings or get_settings()
    request.validate(STAGE_NAMES)
    job_id = job_id or new_job_id()

    if storage is None:
        from app.storage.base import storage_from_settings

        storage = storage_from_settings(settings)

    job_dir, workdir = _job_paths(settings, job_id, request.output_dir)
    started = time.time()
    manifest: dict[str, Any] = {
        "job_id": job_id,
        "status": "running",
        "started_at": utc_now(),
        "request": {
            "input_uri": request.input_uri,
            "stages": list(request.stages),
            "options": _public_options(request.options),
        },
        "stages": [],
        "artifacts": {},
        "outputs": [],
        "gpu_validation_status": "pending",
        "workdir": str(workdir),
    }

    def emit(status: str, extra: dict[str, Any] | None = None) -> None:
        if progress:
            progress(status, {**manifest, **(extra or {})})

    current: str | None = None
    try:
        # ---------------------------------------------------------- acquire --
        emit("acquiring_input")
        allowed_roots = [settings.work_dir_resolved, *settings.extra_input_roots]
        if settings.allow_external_local_inputs:
            allowed_roots.append(Path.cwd())
        parsed = parse_input_uri(request.input_uri, allowed_roots=allowed_roots)
        suffix = Path(parsed.uri).suffix or ".mp4"
        source = workdir / f"00_input{suffix}"
        if parsed.kind == "s3":
            storage.acquire(parsed.uri, source)
        else:
            storage.acquire(str(parsed.path), source)
        if not source.exists() or source.stat().st_size == 0:
            raise FrameFrogError("acquired input is missing or empty")
        manifest["artifacts"]["source"] = str(source)

        # --------------------------------------------------------- validate --
        emit("validating")
        info: MediaInfo = validation.validate_media_file(source, settings, ffprobe_path=settings.ffprobe_path)
        manifest["input"] = info.to_dict()
        manifest["input_metadata"] = info.to_dict()

        # ---------------------------------------------------------- process --
        emit("processing")
        scenes: list[dict[str, Any]] = []
        disabled_stages = set(request.options.get("_disabled_stages") or [])
        total = len(request.stages)
        for index, name in enumerate(request.stages):
            if name in disabled_stages:
                # The caller switched this stage off explicitly.
                manifest["stages"].append(
                    {
                        "stage": name,
                        "status": STATUS_SKIPPED,
                        "duration_s": 0.0,
                        "meta": {"reason": "disabled by the request options"},
                    }
                )
                emit("progress", {"progress_percent": int((index + 1) / max(1, total) * 90)})
                continue
            if cancel_check is not None and cancel_check():
                manifest["status"] = "cancelled"
                manifest["stages"].append(
                    {
                        "stage": name,
                        "status": STATUS_CANCELLED,
                        "duration_s": 0.0,
                        "error": "cancelled before the stage started",
                    }
                )
                raise JobCancelledSignal(name)
            stage_started = time.time()
            emit(f"stage:{name}")
            stage_options = dict(request.options)
            if cancel_check is not None:
                # Private key: never serialised into the manifest.
                stage_options["_cancel_check"] = cancel_check
            if name == "interpolate" and scenes:
                # Interpolation must not blend across a hard cut.
                stage_options["scenes"] = scenes
            try:
                result = _run_stage(
                    name,
                    str(current or source),
                    str(workdir),
                    stage_options,
                    scenes,
                )
            except CapabilityError as exc:
                manifest["stages"].append(
                    {
                        "stage": name,
                        "status": STATUS_PENDING_GPU_VALIDATION,
                        "duration_s": round(time.time() - stage_started, 3),
                        "error": exc.message[:300],
                        "details": exc.details,
                    }
                )
                raise
            except Exception as exc:
                log_event(
                    log,
                    "stage_failed",
                    job_id=job_id,
                    stage=name,
                    error_code=getattr(exc, "code", "internal_error"),
                    level=40,
                    detail=safe_error(exc),
                )
                manifest["stages"].append(
                    {
                        "stage": name,
                        "status": STATUS_FAILED,
                        "duration_s": round(time.time() - stage_started, 3),
                        "error": safe_error(exc),
                        "error_detail": f"{type(exc).__name__}: {exc}",
                        "traceback": traceback.format_exc(limit=3),
                    }
                )
                raise

            duration = round(time.time() - stage_started, 3)
            manifest["stages"].append(
                {
                    "stage": result.stage,
                    "status": result.status or STATUS_COMPLETED,
                    "duration_s": duration,
                    "output": result.output_uri,
                    "meta": result.meta,
                }
            )
            if name in VIDEO_PRODUCING and result.output_uri:
                current = result.output_uri
            if name == "scene_detect":
                manifest["artifacts"]["scenes"] = result.meta.get("scenes_path")
                scenes = result.meta.get("scenes") or []
            if name == "rotoscope":
                manifest["artifacts"]["matte"] = result.meta.get("matte")
            if name == "deliver":
                manifest["artifacts"].update(
                    {
                        "master": result.meta.get("master"),
                        "proxy": result.meta.get("proxy"),
                        "poster": result.meta.get("poster"),
                    }
                )
                manifest["outputs"] = _collect_outputs(result.meta, settings)
            if cancel_check is not None and cancel_check():
                raise JobCancelledSignal(name)
            emit("progress", {"progress_percent": int((index + 1) / max(1, total) * 90)})

        # ---------------------------------------------------------- upload --
        emit("uploading")
        if settings.storage_backend == "s3":
            manifest["outputs"] = _publish_outputs(manifest["outputs"], storage, settings, job_id)
        manifest["status"] = "completed"
        manifest["final_output"] = current
    except JobCancelledSignal as cancelled:
        manifest["status"] = "cancelled"
        manifest["error"] = {
            "code": "job_cancelled",
            "message": f"job cancelled before or during stage {cancelled.stage}",
        }
        log_event(log, "job_cancelled", job_id=job_id, stage=cancelled.stage, level=30)
    except CapabilityError as exc:
        manifest["status"] = "failed"
        manifest["error"] = {
            "code": exc.code,
            "message": exc.message,
            "details": exc.details,
        }
        log_event(
            log,
            "job_capability_error",
            job_id=job_id,
            error_code=exc.code,
            level=40,
        )
        log_event(log, "job_failed", job_id=job_id, error_code=exc.code, level=40)
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["error"] = {
            "code": getattr(exc, "code", "internal_error"),
            "message": safe_error(exc),
        }
        log_event(
            log,
            "job_failed",
            job_id=job_id,
            error_code=getattr(exc, "code", "internal_error"),
            level=40,
            detail=safe_error(exc),
        )
    finally:
        manifest["finished_at"] = utc_now()
        manifest["elapsed_s"] = round(time.time() - started, 3)

        # The webhook is delivered for every finished job — success, failure
        # or cancellation — and never changes the job's status.
        webhook_url = request.options.get("webhook_url")
        if webhook_url:
            from app.core import ops

            manifest["webhook"] = ops.notify_manifest(
                manifest,
                webhook_url,
                secret=request.options.get("webhook_secret"),
            )

        manifest_path = job_dir / "manifest.json"
        try:
            with open(manifest_path, "w", encoding="utf-8") as handle:
                json.dump(manifest, handle, indent=2, default=str)
        except OSError as exc:  # pragma: no cover - disk full / permissions
            log.error("manifest_write_failed", extra={"extra_fields": {"error": str(exc)}})
        manifest["manifest_path"] = str(manifest_path)
        if not keep_workdir and manifest.get("status") == "completed":
            # Intermediates stay for inspection only when asked for; the
            # manifest keeps pointing at them either way.
            pass

    emit(manifest["status"])
    return manifest


class JobCancelledSignal(Exception):
    """Internal signal used to unwind a cancelled job."""

    def __init__(self, stage: str) -> None:
        super().__init__(f"cancelled at stage {stage}")
        self.stage = stage


def _run_stage(
    name: str,
    input_path: str,
    workdir: str,
    options: dict[str, Any],
    scenes: list[dict[str, Any]],
) -> StageResult:
    """Run one stage, per shot when the fan-out is enabled.

    ``options["fanout"]`` is ``off`` (default), ``auto`` (per-shot when it
    works, monolithic when it fails) or ``shots`` (per-shot, and a failure is
    a failure of the job).
    """
    from app.jobs import fanout

    fallback_reason: str | None = None
    if name in fanout.FANOUT_STAGES and scenes:
        try:
            fanout_result = fanout.fanout_stage(name, input_path, workdir, scenes, options)
        except Exception as exc:
            if str(options.get("fanout", "off")).lower() == "auto":
                fallback_reason = f"{type(exc).__name__}: {exc}"
            else:
                raise
        else:
            if fanout_result is not None:
                return fanout_result

    result = STAGES[name](input_path, workdir, options)
    if fallback_reason:
        result.meta["fanout_fallback_reason"] = fallback_reason
    return result


def _publish_outputs(
    outputs: list[dict[str, Any]],
    storage: Any,
    settings: Settings,
    job_id: str,
) -> list[dict[str, Any]]:
    """Upload the produced artifacts to object storage.

    Only used with the S3 backend; the local backend keeps the files in the
    job work directory. The returned entries carry the remote URI next to the
    local path.
    """
    published: list[dict[str, Any]] = []
    for entry in outputs:
        local = entry.get("path")
        if not local or not os.path.exists(local):
            published.append(entry)
            continue
        key = storage.build_key(storage.output_prefix, job_id, os.path.basename(local))
        try:
            uri = storage.publish(local, key)
        except Exception as exc:  # the failure is reported, never hidden
            entry = {**entry, "upload_error": f"{type(exc).__name__}: {exc}"}
            published.append(entry)
            continue
        published.append({**entry, "uri": uri, "key": key})
    return published


def _collect_outputs(meta: dict[str, Any], settings: Settings) -> list[dict[str, Any]]:
    """Build the output list with real metadata and checksums."""
    outputs: list[dict[str, Any]] = []
    for key, label in (("master", "master"), ("proxy", "proxy"), ("poster", "poster")):
        path = meta.get(key)
        if not path:
            continue
        entry: dict[str, Any] = {"kind": label, "path": str(path)}
        try:
            entry["size_bytes"] = os.path.getsize(path)
            entry["sha256"] = validation.file_sha256(path)
        except OSError:
            continue
        if key == "master":
            try:
                info = validation.validate_media_file(path, settings, ffprobe_path=settings.ffprobe_path)
                entry.update(
                    {
                        "width": info.width,
                        "height": info.height,
                        "fps": round(info.fps, 3),
                        "duration_s": round(info.duration_s, 3),
                        "video_codec": info.video_codec,
                        "audio_codec": info.audio_codec,
                        "container": info.container,
                    }
                )
            except FrameFrogError:
                # The file exists but does not satisfy the media limits (a
                # short proxy is fine) — record what is known.
                pass
        outputs.append(entry)
    return outputs


def describe(settings: Settings | None = None) -> dict[str, Any]:
    """Machine-readable capability list for /health and the marketing site."""
    from app.core import capabilities
    from app.models.discovery import model_report

    settings = settings or get_settings()
    return {
        "name": "framefrog-pipeline",
        "version": __import__("app").__version__,
        "stages": STAGE_NAMES,
        "default_stages": list(DEFAULT_STAGES),
        "capabilities": capabilities.report(settings.ffmpeg_path, settings.ffprobe_path),
        "models": model_report(),
    }


def cleanup_workdir(settings: Settings, job_id: str) -> None:
    """Remove a job directory (used by the runner when a job is discarded)."""
    job_dir = settings.work_dir_resolved / "jobs" / job_id
    shutil.rmtree(job_dir, ignore_errors=True)
