# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""FastAPI application factory.

    uvicorn app.main:app --port 8080

The app owns the job repository, the background runner and the storage
backend for the lifetime of the process. Everything is wired in the lifespan
handler so that tests can create an app with their own settings and close it
cleanly again.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api import routes_health, routes_jobs
from app.core.config import Settings, get_settings
from app.core.errors import FrameFrogError
from app.core.logging import configure_logging, get_logger, log_event
from app.jobs.repository import JobRepository, repository_from_settings
from app.jobs.runner import JobRunner
from app.storage.base import storage_from_settings

log = get_logger("api")


def create_app(settings: Settings | None = None, repository: JobRepository | None = None) -> FastAPI:
    """Build the application.

    ``settings`` and ``repository`` can be injected by tests; in production
    they come from the environment.
    """
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.started_at = time.time()
        app.state.settings = settings
        repo = repository or repository_from_settings(settings)
        app.state.repository = repo
        storage = storage_from_settings(settings)
        app.state.storage = storage
        runner = JobRunner(repo, settings=settings, storage=storage)
        runner.start()
        app.state.runner = runner
        log_event(
            log,
            "app_started",
            app_env=settings.app_env,
            storage_backend=settings.storage_backend,
            max_workers=runner.max_workers,
        )
        try:
            yield
        finally:
            runner.shutdown(wait=False)
            repo.close()
            log_event(log, "app_stopped")

    app = FastAPI(
        title="FrameFrog Pipeline API",
        description=(
            "AI video post-production: scene detection, LUT grading, audio "
            "cleanup, frame interpolation and MP4 delivery."
        ),
        version=_version(),
        lifespan=lifespan,
    )

    @app.exception_handler(FrameFrogError)
    async def _framefrog_error_handler(_request: Request, exc: FrameFrogError) -> JSONResponse:
        log_event(
            log,
            "request_failed",
            error_code=exc.code,
            level=40,
            message=exc.message,
        )
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "request validation failed",
                    "details": {"errors": _safe_errors(exc)},
                }
            },
        )

    app.include_router(routes_health.router)
    app.include_router(routes_jobs.router)

    @app.get("/", tags=["meta"])
    def root() -> dict[str, Any]:
        """Service banner."""
        return {
            "name": "framefrog-pipeline",
            "version": _version(),
            "docs": "/docs",
            "endpoints": [
                "GET /health",
                "GET /ready",
                "POST /v1/jobs",
                "GET /v1/jobs/{job_id}",
                "POST /v1/jobs/{job_id}/cancel",
                "GET /v1/jobs/{job_id}/outputs",
            ],
        }

    return app


def _version() -> str:
    import app

    return app.__version__


def _safe_errors(exc: RequestValidationError) -> list:
    """Validation errors without echoing the submitted payload."""
    safe = []
    for error in exc.errors():
        safe.append(
            {
                "loc": [str(part) for part in error.get("loc", [])],
                "msg": str(error.get("msg", "")),
                "type": str(error.get("type", "")),
            }
        )
    return safe


app = create_app()
