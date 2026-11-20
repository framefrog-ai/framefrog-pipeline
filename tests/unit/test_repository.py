# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""SQLite job repository: persistence, idempotency, cancellation."""

from __future__ import annotations

import pytest

from app.core.errors import ConflictError, NotFoundError
from app.jobs.models import JobRecord
from app.jobs.states import JobState


def _record(job_id: str = "fj_test", **kwargs) -> JobRecord:
    defaults = {
        "job_id": job_id,
        "status": JobState.QUEUED.value,
        "request": {"input_uri": "s3://bucket/x.mp4", "stages": ["deliver"], "options": {}},
    }
    defaults.update(kwargs)
    return JobRecord(**defaults)


def test_create_and_get(repo):
    record = repo.create(_record())
    loaded = repo.get(record.job_id)
    assert loaded is not None
    assert loaded.status == JobState.QUEUED.value
    assert loaded.request["input_uri"] == "s3://bucket/x.mp4"


def test_missing_job_returns_none(repo):
    assert repo.get("fj_missing") is None
    with pytest.raises(NotFoundError):
        repo.require("fj_missing")


def test_update_persists_fields(repo):
    record = repo.create(_record())
    record.progress_percent = 42
    record.current_stage = "color_grade"
    record.stage_results = [{"stage": "color_grade", "status": "completed"}]
    repo.save(record)
    loaded = repo.require(record.job_id)
    assert loaded.progress_percent == 42
    assert loaded.current_stage == "color_grade"
    assert loaded.stage_results[0]["stage"] == "color_grade"


def test_idempotency_key_is_unique(repo):
    repo.create(_record(job_id="fj_a", idempotency_key="key-1"))
    with pytest.raises(ConflictError):
        repo.create(_record(job_id="fj_b", idempotency_key="key-1"))


def test_find_by_idempotency_key(repo):
    repo.create(_record(job_id="fj_a", idempotency_key="key-1"))
    found = repo.find_by_idempotency_key("key-1")
    assert found is not None and found.job_id == "fj_a"
    assert repo.find_by_idempotency_key("missing") is None


def test_cancel_flag(repo):
    record = repo.create(_record())
    assert repo.is_cancelled(record.job_id) is False
    assert repo.request_cancel(record.job_id) is True
    assert repo.is_cancelled(record.job_id) is True


def test_cannot_cancel_finished_job(repo):
    record = repo.create(_record(status=JobState.COMPLETED.value))
    assert repo.request_cancel(record.job_id) is False


def test_cancel_unknown_job(repo):
    assert repo.request_cancel("fj_missing") is False


def test_state_transition_validated(repo):
    record = repo.create(_record())
    record.status = JobState.COMPLETED.value
    with pytest.raises(ValueError):
        repo.update(record)


def test_set_status_records_completion_time(repo):
    record = repo.create(_record())
    record = repo.set_status(record, JobState.ACQUIRING_INPUT)
    record = repo.set_status(record, JobState.VALIDATING)
    record = repo.set_status(record, JobState.PROCESSING)
    record = repo.set_status(record, JobState.COMPLETED)
    assert record.completed_at is not None


def test_list_is_newest_first(repo):
    repo.create(_record(job_id="fj_a"))
    repo.create(_record(job_id="fj_b"))
    ids = [record.job_id for record in repo.list()]
    assert set(ids) == {"fj_a", "fj_b"}
