# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Job retention: finished jobs are reclaimed, running ones are never touched."""

from __future__ import annotations

import time

from app.core.config import Settings
from app.jobs.models import JobRecord
from app.jobs.repository import SQLiteJobRepository
from app.jobs.retention import cleanup_finished_jobs, disk_usage, job_directory
from app.jobs.states import JobState


def _settings(tmp_path) -> Settings:
    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    return Settings(
        app_env="test",
        work_dir=work,
        database_url=f"sqlite:///{tmp_path / 'jobs.db'}",
        storage_backend="local",
        log_level="WARNING",
    )


def _job(settings: Settings, repository: SQLiteJobRepository, job_id: str, status: str) -> JobRecord:
    directory = job_directory(settings, job_id)
    (directory / "work").mkdir(parents=True, exist_ok=True)
    (directory / "work" / "05_master.mp4").write_bytes(b"x" * 100)
    record = JobRecord(
        job_id=job_id,
        status=status,
        request={"input_uri": "s3://bucket/in.mp4", "stages": ["deliver"], "options": {}},
        completed_at=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - 7200)),
    )
    repository.create(record)
    return record


def test_finished_jobs_are_removed_after_the_window(tmp_path):
    settings = _settings(tmp_path)
    repository = SQLiteJobRepository(tmp_path / "jobs.db")
    record = _job(settings, repository, "fj_old", JobState.COMPLETED.value)
    assert job_directory(settings, record.job_id).exists()

    removed = cleanup_finished_jobs(settings, repository, older_than_s=3600)
    assert removed == ["fj_old"]
    assert not job_directory(settings, record.job_id).exists()
    assert repository.get(record.job_id) is None
    repository.close()


def test_running_jobs_are_never_removed(tmp_path):
    settings = _settings(tmp_path)
    repository = SQLiteJobRepository(tmp_path / "jobs.db")
    for status in (
        JobState.QUEUED.value,
        JobState.PROCESSING.value,
        JobState.CANCELLED.value,
    ):
        _job(settings, repository, f"fj_{status}", status)
    removed = cleanup_finished_jobs(settings, repository, older_than_s=0)
    assert removed == []
    assert repository.get("fj_queued") is not None
    repository.close()


def test_a_zero_window_keeps_everything(tmp_path):
    settings = _settings(tmp_path)
    repository = SQLiteJobRepository(tmp_path / "jobs.db")
    _job(settings, repository, "fj_kept", JobState.COMPLETED.value)
    settings.job_retention_hours = 0
    assert cleanup_finished_jobs(settings, repository) == []
    assert repository.get("fj_kept") is not None
    repository.close()


def test_a_recent_job_is_kept(tmp_path):
    settings = _settings(tmp_path)
    repository = SQLiteJobRepository(tmp_path / "jobs.db")
    record = _job(settings, repository, "fj_recent", JobState.COMPLETED.value)
    record.completed_at = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    repository.save(record)
    assert cleanup_finished_jobs(settings, repository, older_than_s=3600) == []
    assert repository.get(record.job_id) is not None
    repository.close()


def test_disk_usage_reports_the_job_directory(tmp_path):
    settings = _settings(tmp_path)
    repository = SQLiteJobRepository(tmp_path / "jobs.db")
    _job(settings, repository, "fj_usage", JobState.COMPLETED.value)
    usage = disk_usage(settings)
    assert usage["jobs"] == 1
    assert usage["bytes"] >= 100
    repository.close()
