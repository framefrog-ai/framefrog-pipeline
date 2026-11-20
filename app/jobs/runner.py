# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""In-process background runner.

The HTTP request creates a job row and returns immediately; the work happens
in a bounded thread pool. The pool size is ``MAX_CONCURRENT_JOBS`` so a burst
of requests cannot fork-bomb the box with FFmpeg processes.

The runner is deliberately simple — a thread pool plus the state machine — so
it can be replaced by a queue consumer (SQS, Redis, ...) later without
changing the API.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from app.core.config import Settings, get_settings
from app.core.logging import get_logger, log_event
from app.jobs.models import JobRecord, JobRequest, utc_now
from app.jobs.pipeline import run_job
from app.jobs.repository import JobRepository
from app.jobs.states import JobState

log = get_logger("runner")


class JobRunner:
    """Runs jobs in a bounded thread pool and persists their state."""

    def __init__(
        self,
        repository: JobRepository,
        settings: Settings | None = None,
        storage: Any | None = None,
        max_workers: int | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.repository = repository
        self._storage = storage
        self.max_workers = int(max_workers or self.settings.max_concurrent_jobs)
        self._executor: ThreadPoolExecutor | None = None
        self._lock = threading.Lock()
        self._futures: dict[str, Future] = {}

    # -- lifecycle ----------------------------------------------------------- #
    @property
    def storage(self) -> Any:
        if self._storage is None:
            from app.storage.base import storage_from_settings

            self._storage = storage_from_settings(self.settings)
        return self._storage

    def start(self) -> None:
        """Create the thread pool."""
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=self.max_workers, thread_name_prefix="framefrog-job"
            )
            log_event(log, "runner_started", workers=self.max_workers)

    def shutdown(self, wait: bool = True) -> None:
        """Stop the thread pool."""
        if self._executor is not None:
            self._executor.shutdown(wait=wait, cancel_futures=not wait)
            self._executor = None
            log_event(log, "runner_stopped")

    def queue_size(self) -> int:
        """Number of jobs submitted but not finished."""
        with self._lock:
            return sum(1 for fut in self._futures.values() if not fut.done())

    # -- submission ----------------------------------------------------------- #
    def submit(self, record: JobRecord) -> Future:
        """Queue a persisted job for execution."""
        self.start()
        assert self._executor is not None
        request = JobRequest(
            input_uri=str(record.request.get("input_uri", "")),
            stages=list(record.request.get("stages") or []),
            options=dict(record.request.get("options") or {}),
            output_dir=None,
            idempotency_key=record.idempotency_key,
        )
        future = self._executor.submit(self._execute, record.job_id, request)
        with self._lock:
            self._futures[record.job_id] = future
        future.add_done_callback(lambda _f: self._cleanup(record.job_id))
        return future

    def _cleanup(self, job_id: str) -> None:
        with self._lock:
            self._futures.pop(job_id, None)

    # -- execution ------------------------------------------------------------ #
    def _cancelled(self, job_id: str) -> bool:
        try:
            return self.repository.is_cancelled(job_id)
        except Exception:  # pragma: no cover - defensive
            return False

    def _execute(self, job_id: str, request: JobRequest) -> dict[str, Any]:
        record = self.repository.get(job_id)
        if record is None:  # pragma: no cover - defensive
            raise RuntimeError(f"job {job_id} disappeared from the repository")

        started = time.time()

        def on_progress(status: str, payload: dict[str, Any]) -> None:
            self._persist_progress(record, status, payload)

        try:
            self._transition(record, JobState.ACQUIRING_INPUT)
            manifest = run_job(
                request,
                settings=self.settings,
                storage=self.storage,
                job_id=job_id,
                progress=on_progress,
                cancel_check=lambda: self._cancelled(job_id),
            )
        except Exception as exc:  # pragma: no cover - run_job handles its own errors
            log_event(
                log,
                "job_crashed",
                job_id=job_id,
                error_code=getattr(exc, "code", "internal_error"),
                level=40,
            )
            manifest = {
                "job_id": job_id,
                "status": "failed",
                "error": {"code": getattr(exc, "code", "internal_error"), "message": str(exc)},
                "stages": [],
                "outputs": [],
            }

        self._finalise(record, manifest, started)
        return manifest

    @staticmethod
    def _public_stages(stages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Stage results without the internal detail (tracebacks, stderr tails)."""
        public: list[dict[str, Any]] = []
        for stage in stages or []:
            public.append({k: v for k, v in stage.items() if k not in ("error_detail", "traceback")})
        return public

    def _persist_progress(self, record: JobRecord, status: str, payload: dict[str, Any]) -> None:
        """Persist intermediate progress without leaving the state machine."""
        try:
            current = self.repository.get(record.job_id)
            if current is None or JobState(current.status).is_terminal:
                return
            stages = payload.get("stages") or []
            current.stage_results = self._public_stages(stages)
            current.current_stage = payload.get("current_stage") or _current_stage_from(status)
            current.progress_percent = _progress_from(
                status, stages, len(payload.get("request", {}).get("stages") or [])
            )
            current.manifest = {k: v for k, v in payload.items() if k != "stages"}
            if current.started_at is None:
                current.started_at = utc_now()
            self.repository.save(current)
        except Exception as exc:  # pragma: no cover - never fail a job on bookkeeping
            log.warning(
                "progress_persist_failed",
                extra={"extra_fields": {"error": f"{type(exc).__name__}: {exc}"}},
            )

    def _transition(self, record: JobRecord, target: JobState) -> None:
        current = self.repository.get(record.job_id)
        if current is None:
            return
        if current.status == target.value:
            return
        self.repository.set_status(current, target)
        if target == JobState.ACQUIRING_INPUT and current.started_at is None:
            current.started_at = utc_now()
            self.repository.save(current)

    def _finalise(self, record: JobRecord, manifest: dict[str, Any], started: float) -> None:
        current = self.repository.get(record.job_id)
        if current is None:
            return
        status = str(manifest.get("status") or "failed")
        current.manifest = manifest
        current.stage_results = self._public_stages(manifest.get("stages") or [])
        current.outputs = list(manifest.get("outputs") or [])
        current.input_metadata = dict(manifest.get("input_metadata") or manifest.get("input") or {})
        current.gpu_validation_status = str(manifest.get("gpu_validation_status") or "pending")
        current.progress_percent = 100 if status == "completed" else current.progress_percent
        current.current_stage = None
        error = manifest.get("error")
        current.error = error if isinstance(error, dict) else ({"message": str(error)} if error else None)
        if current.started_at is None:
            current.started_at = utc_now()
        if status in {JobState.COMPLETED.value, JobState.FAILED.value, JobState.CANCELLED.value}:
            current.completed_at = utc_now()
        current.status = status
        self.repository.save(current)  # terminal write, no transition check
        log_event(
            log,
            "job_finished",
            job_id=current.job_id,
            duration_seconds=round(time.time() - started, 3),
            error_code=(current.error or {}).get("code") if current.error else None,
            level=40 if status == "failed" else 20,
            status=status,
        )


def _current_stage_from(status: str) -> str | None:
    if status.startswith("stage:"):
        return status.split(":", 1)[1]
    return None


def _progress_from(status: str, stages: list[dict[str, Any]], total: int) -> int:
    if status.startswith("stage:"):
        done = len(stages)
        return int(min(95, done / max(1, total) * 90))
    if status in {"acquiring_input", "validating"}:
        return 5
    if status == "processing":
        return 50
    if status == "uploading":
        return 95
    return 0
