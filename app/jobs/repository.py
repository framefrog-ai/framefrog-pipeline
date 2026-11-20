# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Persistent job storage.

The repository is an interface plus a SQLite implementation. The interface is
deliberately narrow so a PostgreSQL implementation can replace it later
without touching the runner or the API:

    class JobRepository(Protocol):
        def create(self, record: JobRecord) -> JobRecord: ...
        def get(self, job_id: str) -> JobRecord | None: ...
        def update(self, record: JobRecord) -> JobRecord: ...
        def find_by_idempotency_key(self, key: str) -> JobRecord | None: ...
        def list(self, limit: int = 50) -> list[JobRecord]: ...
        def request_cancel(self, job_id: str) -> bool: ...
        def is_cancelled(self, job_id: str) -> bool: ...
        def close(self) -> None: ...

SQLite is used in WAL mode with a lock around writes, which is enough for a
single-process API plus its background runner.
"""

from __future__ import annotations

import builtins
import json
import sqlite3
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from app.core.errors import ConflictError, NotFoundError
from app.jobs.models import JobRecord, utc_now
from app.jobs.states import JobState, validate_transition

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id              TEXT PRIMARY KEY,
    status              TEXT NOT NULL,
    idempotency_key     TEXT,
    request_json        TEXT NOT NULL,
    current_stage       TEXT,
    progress_percent    INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    started_at          TEXT,
    completed_at        TEXT,
    input_metadata_json TEXT NOT NULL DEFAULT '{}',
    stage_results_json  TEXT NOT NULL DEFAULT '[]',
    outputs_json        TEXT NOT NULL DEFAULT '[]',
    error_json          TEXT,
    cancel_requested    INTEGER NOT NULL DEFAULT 0,
    gpu_validation_status TEXT NOT NULL DEFAULT 'pending',
    manifest_json       TEXT NOT NULL DEFAULT '{}'
);

CREATE UNIQUE INDEX IF NOT EXISTS jobs_idempotency_key
    ON jobs (idempotency_key) WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS jobs_created_at ON jobs (created_at);
"""


class JobRepository(ABC):
    """Storage interface for jobs."""

    @abstractmethod
    def create(self, record: JobRecord) -> JobRecord:
        """Insert a new job."""

    @abstractmethod
    def get(self, job_id: str) -> JobRecord | None:
        """Load a job, or ``None``."""

    @abstractmethod
    def update(self, record: JobRecord) -> JobRecord:
        """Persist a modified job (state transitions are validated)."""

    @abstractmethod
    def save(self, record: JobRecord) -> JobRecord:
        """Persist a modified job without a state-transition check."""

    @abstractmethod
    def set_status(self, record: JobRecord, status: JobState) -> JobRecord:
        """Move a job to a new state, validating the transition."""

    @abstractmethod
    def require(self, job_id: str) -> JobRecord:
        """Load a job or raise :class:`NotFoundError`."""

    @abstractmethod
    def find_by_idempotency_key(self, key: str) -> JobRecord | None:
        """Look a job up by its idempotency key."""

    @abstractmethod
    def list(self, limit: int = 50) -> builtins.list[JobRecord]:
        """Most recent jobs first."""

    @abstractmethod
    def request_cancel(self, job_id: str) -> bool:
        """Flag a job for cancellation; returns False when it is already done."""

    @abstractmethod
    def is_cancelled(self, job_id: str) -> bool:
        """Whether cancellation was requested."""

    @abstractmethod
    def delete(self, job_id: str) -> bool:
        """Remove a job row. Returns whether a row was removed."""

    @abstractmethod
    def close(self) -> None:
        """Release resources."""


class SQLiteJobRepository(JobRepository):
    """SQLite-backed job repository."""

    def __init__(self, database_path: str | Path = ":memory:") -> None:
        self._lock = threading.RLock()
        if str(database_path) != ":memory:":
            Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(database_path), check_same_thread=False, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            # WAL keeps readers (the API) from blocking on the writer (the
            # runner) while a job is being updated.
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # -- helpers ------------------------------------------------------------ #
    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            job_id=row["job_id"],
            status=row["status"],
            request=json.loads(row["request_json"] or "{}"),
            idempotency_key=row["idempotency_key"],
            current_stage=row["current_stage"],
            progress_percent=int(row["progress_percent"] or 0),
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            input_metadata=json.loads(row["input_metadata_json"] or "{}"),
            stage_results=json.loads(row["stage_results_json"] or "[]"),
            outputs=json.loads(row["outputs_json"] or "[]"),
            error=json.loads(row["error_json"]) if row["error_json"] else None,
            cancel_requested=bool(row["cancel_requested"]),
            gpu_validation_status=row["gpu_validation_status"] or "pending",
            manifest=json.loads(row["manifest_json"] or "{}"),
        )

    def save(self, record: JobRecord) -> JobRecord:
        """Persist a modified job without a state-transition check."""
        return self._write(record)

    def _write(self, record: JobRecord) -> JobRecord:
        with self._lock:
            self._conn.execute(
                """
                UPDATE jobs SET
                    status = ?, idempotency_key = ?, request_json = ?, current_stage = ?,
                    progress_percent = ?, started_at = ?, completed_at = ?,
                    input_metadata_json = ?, stage_results_json = ?, outputs_json = ?,
                    error_json = ?, cancel_requested = ?, gpu_validation_status = ?,
                    manifest_json = ?
                WHERE job_id = ?
                """,
                (
                    record.status,
                    record.idempotency_key,
                    json.dumps(record.request, default=str),
                    record.current_stage,
                    int(record.progress_percent),
                    record.started_at,
                    record.completed_at,
                    json.dumps(record.input_metadata, default=str),
                    json.dumps(record.stage_results, default=str),
                    json.dumps(record.outputs, default=str),
                    json.dumps(record.error, default=str) if record.error else None,
                    1 if record.cancel_requested else 0,
                    record.gpu_validation_status,
                    json.dumps(record.manifest, default=str),
                    record.job_id,
                ),
            )
            self._conn.commit()
        return record

    # -- contract ------------------------------------------------------------ #
    def create(self, record: JobRecord) -> JobRecord:
        if record.idempotency_key:
            existing = self.find_by_idempotency_key(record.idempotency_key)
            if existing is not None:
                raise ConflictError(
                    "a job with this idempotency key already exists",
                    details={"job_id": existing.job_id},
                )
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO jobs (
                        job_id, status, idempotency_key, request_json, current_stage,
                        progress_percent, created_at, started_at, completed_at,
                        input_metadata_json, stage_results_json, outputs_json,
                        error_json, cancel_requested, gpu_validation_status, manifest_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.job_id,
                        record.status,
                        record.idempotency_key,
                        json.dumps(record.request, default=str),
                        record.current_stage,
                        int(record.progress_percent),
                        record.created_at,
                        record.started_at,
                        record.completed_at,
                        json.dumps(record.input_metadata, default=str),
                        json.dumps(record.stage_results, default=str),
                        json.dumps(record.outputs, default=str),
                        json.dumps(record.error, default=str) if record.error else None,
                        1 if record.cancel_requested else 0,
                        record.gpu_validation_status,
                        json.dumps(record.manifest, default=str),
                    ),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"job {record.job_id} already exists") from exc
        return record

    def get(self, job_id: str) -> JobRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return self._row_to_record(row) if row else None

    def require(self, job_id: str) -> JobRecord:
        record = self.get(job_id)
        if record is None:
            raise NotFoundError("job not found", details={"job_id": job_id})
        return record

    def update(self, record: JobRecord) -> JobRecord:
        current = self.get(record.job_id)
        if current is None:
            raise NotFoundError("job not found", details={"job_id": record.job_id})
        if current.status != record.status:
            try:
                validate_transition(JobState(current.status), JobState(record.status))
            except ValueError:
                # Unknown status strings are rejected by the state machine.
                raise
        return self._write(record)

    def set_status(self, record: JobRecord, status: JobState) -> JobRecord:
        """Move a job to a new state, validating the transition."""
        current = JobState(record.status)
        validate_transition(current, status)
        record.status = status.value
        if status == JobState.COMPLETED and not record.completed_at:
            record.completed_at = utc_now()
        return self._write(record)

    def find_by_idempotency_key(self, key: str) -> JobRecord | None:
        if not key:
            return None
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE idempotency_key = ?", (key,)).fetchone()
        return self._row_to_record(row) if row else None

    def list(self, limit: int = 50) -> builtins.list[JobRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC, job_id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def request_cancel(self, job_id: str) -> bool:
        """Flag a job for cancellation.

        Returns ``False`` when the job does not exist or already reached a
        terminal state (a finished job cannot be cancelled).
        """
        with self._lock:
            row = self._conn.execute("SELECT status FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                return False
            if JobState(row["status"]).is_terminal:
                return False
            self._conn.execute("UPDATE jobs SET cancel_requested = 1 WHERE job_id = ?", (job_id,))
            self._conn.commit()
        return True

    def is_cancelled(self, job_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT cancel_requested FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return bool(row and row["cancel_requested"])

    def delete(self, job_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
            self._conn.commit()
        return cursor.rowcount > 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def repository_from_settings(settings: Any) -> JobRepository:
    """Build the repository described by ``DATABASE_URL``."""
    url = getattr(settings, "database_url", "") or ""
    if url.startswith("postgres"):  # pragma: no cover - not implemented yet
        raise NotImplementedError("PostgreSQL storage is not implemented; use sqlite:///... for now")
    path = getattr(settings, "sqlite_path", None)
    if path is None:
        return SQLiteJobRepository(":memory:")
    return SQLiteJobRepository(path)


__all__ = [
    "JobRepository",
    "SQLiteJobRepository",
    "repository_from_settings",
]
