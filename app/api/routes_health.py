# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Liveness and readiness endpoints.

``/health`` answers "is this process alive?" and never fails because an
optional dependency is missing.

``/ready`` answers "can this process accept a job right now?" — it checks the
binaries, the database and the storage backend, and returns 503 when one of
them is unusable.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Request, Response, status

from app.api.schemas import HealthResponse, ReadyResponse
from app.core import capabilities
from app.core.config import Settings

router = APIRouter(tags=["health"])


def _version() -> str:
    import app

    return app.__version__


@router.get("/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    """Liveness probe."""
    settings: Settings = request.app.state.settings
    started = float(request.app.state.started_at)
    return HealthResponse(
        status="ok",
        version=_version(),
        uptime_s=round(time.time() - started, 3),
        app_env=settings.app_env,
    )


@router.get("/ready", response_model=ReadyResponse)
def ready(request: Request, response: Response) -> ReadyResponse:
    """Readiness probe — 503 when a required dependency is unusable."""
    settings: Settings = request.app.state.settings
    checks: dict[str, Any] = {}

    ff = capabilities.ffmpeg_info(settings.ffmpeg_path)
    fp = capabilities.ffprobe_info(settings.ffprobe_path)
    checks["ffmpeg"] = {"ok": bool(ff.get("available")), "version": ff.get("version")}
    checks["ffprobe"] = {"ok": bool(fp.get("available")), "version": fp.get("version")}

    repository = request.app.state.repository
    try:
        repository.list(limit=1)
        checks["database"] = {"ok": True, "backend": type(repository).__name__}
    except Exception as exc:
        checks["database"] = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}

    storage = request.app.state.storage
    try:
        storage_health = storage.health()
    except Exception as exc:
        storage_health = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    checks["storage"] = storage_health

    runner = request.app.state.runner
    checks["runner"] = {
        "ok": True,
        "max_workers": getattr(runner, "max_workers", None),
        "queued": runner.queue_size() if hasattr(runner, "queue_size") else 0,
    }

    # The optional model runtimes are reported but never decide readiness:
    # the pipeline has documented fallbacks for every one of them.
    from app.models.discovery import model_report

    models: dict[str, Any] = model_report()
    checks["models"] = {
        "ok": True,
        "deepfilternet": bool(models["deepfilternet"]["available"]),
        "onnxruntime": bool(models["rife"]["onnxruntime"]),
        "rife_model": models["rife"]["model"],
        "rife_available": bool(models["rife"]["available"]),
    }
    checks["gpu"] = {"ok": True, "available": capabilities.cuda_available()}

    ok = all(bool(check.get("ok")) for check in checks.values())
    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadyResponse(status="ready" if ok else "not_ready", checks=checks)
