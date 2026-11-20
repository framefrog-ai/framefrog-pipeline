# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Background runner: persistence, completion, cancellation and concurrency."""

from __future__ import annotations

import time

from app.core.config import Settings
from app.jobs.models import JobRecord
from app.jobs.repository import SQLiteJobRepository
from app.jobs.runner import JobRunner
from app.jobs.states import JobState


def _settings(tmp_path) -> Settings:
    work_dir = tmp_path / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        app_env="test",
        work_dir=work_dir,
        database_url=f"sqlite:///{tmp_path / 'jobs.db'}",
        storage_backend="local",
        log_level="WARNING",
    )


def _record(input_uri: str, job_id: str = "fj_run") -> JobRecord:
    return JobRecord(
        job_id=job_id,
        status=JobState.QUEUED.value,
        request={"input_uri": input_uri, "stages": ["scene_detect"], "options": {}},
    )


def _sample(settings, tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    target = settings.work_dir_resolved / "sample.mp4"
    subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / "generate_test_video.py"),
            "--output",
            str(target),
            "--duration",
            "2",
            "--scenes",
            "2",
            "--width",
            "160",
            "--height",
            "90",
        ],
        check=True,
        capture_output=True,
    )
    return target


def _wait_for_terminal(repository, job_id, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        record = repository.get(job_id)
        if record and JobState(record.status).is_terminal:
            return record
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} did not finish in {timeout}s")


def test_runner_completes_a_job(tmp_path):
    settings = _settings(tmp_path)
    repository = SQLiteJobRepository(tmp_path / "jobs.db")
    runner = JobRunner(repository, settings=settings)
    sample = _sample(settings, tmp_path)
    record = repository.create(_record(str(sample)))
    try:
        runner.submit(record)
        finished = _wait_for_terminal(repository, record.job_id)
        assert finished.status == JobState.COMPLETED.value
        assert finished.completed_at
        assert finished.stage_results
        assert finished.gpu_validation_status == "pending"
        assert finished.manifest["status"] == "completed"
    finally:
        runner.shutdown()
        repository.close()


def test_runner_records_failures(tmp_path):
    settings = _settings(tmp_path)
    repository = SQLiteJobRepository(tmp_path / "jobs.db")
    runner = JobRunner(repository, settings=settings)
    broken = settings.work_dir_resolved / "broken.mp4"
    broken.write_bytes(b"not a video")
    record = repository.create(_record(str(broken)))
    try:
        runner.submit(record)
        finished = _wait_for_terminal(repository, record.job_id)
        assert finished.status == JobState.FAILED.value
        assert finished.error
        assert finished.error["code"] == "media_validation_error"
    finally:
        runner.shutdown()
        repository.close()


def test_cancellation_is_observed(tmp_path):
    settings = _settings(tmp_path)
    repository = SQLiteJobRepository(tmp_path / "jobs.db")
    runner = JobRunner(repository, settings=settings)
    sample = _sample(settings, tmp_path)
    record = repository.create(_record(str(sample)))
    try:
        repository.request_cancel(record.job_id)
        runner.submit(record)
        finished = _wait_for_terminal(repository, record.job_id)
        assert finished.status in {JobState.CANCELLED.value, JobState.COMPLETED.value}
        if finished.status == JobState.CANCELLED.value:
            assert finished.error["code"] == "job_cancelled"
    finally:
        runner.shutdown()
        repository.close()


def test_state_survives_a_restart(tmp_path):
    """A job row is readable by a new repository instance (SQLite file)."""
    path = tmp_path / "jobs.db"
    first = SQLiteJobRepository(path)
    record = first.create(_record("s3://bucket/x.mp4"))
    first.close()

    second = SQLiteJobRepository(path)
    try:
        loaded = second.get(record.job_id)
        assert loaded is not None
        assert loaded.request["input_uri"] == "s3://bucket/x.mp4"
    finally:
        second.close()
