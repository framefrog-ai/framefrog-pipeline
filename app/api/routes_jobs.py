# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Job endpoints (version 1).

    POST /v1/jobs                  create a job (returns immediately)
    GET  /v1/jobs/{job_id}         job state, stage results, outputs
    POST /v1/jobs/{job_id}/cancel  request cancellation
    GET  /v1/jobs/{job_id}/outputs the produced artifacts

The create endpoint never blocks on processing: it persists the job and hands
it to the background runner.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, status

from app.api.deps import get_repository, get_runner, get_settings_dep, require_auth
from app.api.schemas import (
    ErrorModel,
    JobCreateRequest,
    JobResponse,
    OutputModel,
    StageResultModel,
    to_stage_options,
    validate_target_fps,
)
from app.core.config import Settings
from app.core.errors import ConflictError, NotFoundError
from app.core.logging import get_logger, log_event
from app.core.security import parse_input_uri
from app.jobs.models import DEFAULT_STAGES, JobRecord, new_job_id
from app.jobs.repository import JobRepository
from app.jobs.states import JobState

router = APIRouter(prefix="/v1/jobs", tags=["jobs"])
log = get_logger("api.jobs")


def _to_response(record: JobRecord) -> JobResponse:
    return JobResponse(
        job_id=record.job_id,
        status=record.status,
        current_stage=record.current_stage,
        progress_percent=record.progress_percent,
        created_at=record.created_at,
        started_at=record.started_at,
        completed_at=record.completed_at,
        input_metadata=record.input_metadata,
        stage_results=[StageResultModel(**entry) for entry in record.stage_results],
        outputs=[OutputModel(**entry) for entry in record.outputs],
        error=ErrorModel(**record.error) if record.error else None,
        gpu_validation_status=record.gpu_validation_status,
    )


@router.post("", response_model=JobResponse, status_code=status.HTTP_202_ACCEPTED)
def create_job(
    body: JobCreateRequest,
    request: Request,
    settings: Settings = Depends(get_settings_dep),
    repository: JobRepository = Depends(get_repository),
    runner: Any = Depends(get_runner),
    _auth: None = Depends(require_auth),
) -> JobResponse:
    """Create a job and queue it for background processing."""
    if body.idempotency_key:
        existing = repository.find_by_idempotency_key(body.idempotency_key)
        if existing is not None:
            raise ConflictError(
                "a job with this idempotency key already exists",
                details={"job_id": existing.job_id},
            )

    # Validate the input reference before anything is persisted.
    parse_input_uri(
        body.input_uri,
        allowed_roots=[settings.work_dir_resolved, *settings.extra_input_roots],
    )
    validate_target_fps(body.options.interpolate.target_fps, settings.max_output_fps)

    stages = list(body.stages or DEFAULT_STAGES)
    record = JobRecord(
        job_id=new_job_id(),
        status=JobState.QUEUED.value,
        request={
            "input_uri": body.input_uri,
            "stages": stages,
            "options": to_stage_options(body.options),
        },
        idempotency_key=body.idempotency_key,
    )
    repository.create(record)
    runner.submit(record)
    log_event(
        log,
        "job_created",
        job_id=record.job_id,
        stages=len(stages),
        idempotent=bool(body.idempotency_key),
    )
    return _to_response(repository.require(record.job_id))


@router.get("/{job_id}", response_model=JobResponse)
def get_job(
    job_id: str,
    repository: JobRepository = Depends(get_repository),
    _auth: None = Depends(require_auth),
) -> JobResponse:
    """Return the current state of a job."""
    record = repository.get(job_id)
    if record is None:
        raise NotFoundError("job not found", details={"job_id": job_id})
    return _to_response(record)


@router.post("/{job_id}/cancel", response_model=JobResponse)
def cancel_job(
    job_id: str,
    repository: JobRepository = Depends(get_repository),
    _auth: None = Depends(require_auth),
) -> JobResponse:
    """Request cancellation of a running job.

    The flag is checked between stages; a job that already finished cannot be
    cancelled.
    """
    record = repository.get(job_id)
    if record is None:
        raise NotFoundError("job not found", details={"job_id": job_id})
    if JobState(record.status).is_terminal:
        raise ConflictError(
            "job already finished and cannot be cancelled",
            details={"job_id": job_id, "status": record.status},
        )
    if not repository.request_cancel(job_id):
        raise ConflictError("cancellation could not be requested", details={"job_id": job_id})
    log_event(log, "job_cancel_requested", job_id=job_id)
    return _to_response(repository.require(job_id))


@router.get("/{job_id}/outputs", response_model=list[OutputModel])
def job_outputs(
    job_id: str,
    repository: JobRepository = Depends(get_repository),
    _auth: None = Depends(require_auth),
) -> list[OutputModel]:
    """List the artifacts produced by a job."""
    record = repository.get(job_id)
    if record is None:
        raise NotFoundError("job not found", details={"job_id": job_id})
    outputs: list[dict[str, Any]] = list(record.outputs or [])
    return [OutputModel(**entry) for entry in outputs]
