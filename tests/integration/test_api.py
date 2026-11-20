# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""HTTP API integration tests: health, readiness and the versioned job API."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.jobs.repository import SQLiteJobRepository
from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    """A test client with an isolated database and work directory."""
    settings = Settings(
        app_env="test",
        work_dir=tmp_path / "work",
        database_url=f"sqlite:///{tmp_path / 'jobs.db'}",
        storage_backend="local",
        log_level="WARNING",
    )
    repository = SQLiteJobRepository(tmp_path / "jobs.db")
    app = create_app(settings=settings, repository=repository)
    with TestClient(app) as test_client:
        test_client.settings = settings  # type: ignore[attr-defined]
        yield test_client


def _sample(tmp_path):
    """A tiny generated clip inside the work directory."""
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    target = work / "sample.mp4"
    subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / "generate_test_video.py"),
            "--output",
            str(target),
            "--duration",
            "3",
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


# --------------------------------------------------------------------------- #
# health / readiness
# --------------------------------------------------------------------------- #
def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"]
    assert body["uptime_s"] >= 0


def test_ready(client):
    response = client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["ffmpeg"]["ok"] is True
    assert body["checks"]["database"]["ok"] is True
    assert body["checks"]["storage"]["ok"] is True


def test_root_lists_endpoints(client):
    body = client.get("/").json()
    assert "POST /v1/jobs" in body["endpoints"]


# --------------------------------------------------------------------------- #
# job lifecycle
# --------------------------------------------------------------------------- #
def test_create_job_returns_immediately(client, tmp_path):
    sample = _sample(tmp_path)
    response = client.post(
        "/v1/jobs",
        json={
            "input_uri": str(sample),
            "stages": ["scene_detect", "deliver"],
        },
    )
    assert response.status_code == 202
    body = response.json()
    assert body["job_id"]
    assert body["status"] in {
        "queued",
        "acquiring_input",
        "validating",
        "processing",
        "uploading",
        "completed",
        "failed",
        "cancelled",
    }
    assert body["gpu_validation_status"] == "pending"


def test_get_job_returns_state(client, tmp_path):
    sample = _sample(tmp_path)
    created = client.post("/v1/jobs", json={"input_uri": str(sample)}).json()
    fetched = client.get(f"/v1/jobs/{created['job_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["job_id"] == created["job_id"]


def test_get_unknown_job_is_404(client):
    response = client.get("/v1/jobs/fj_missing")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_idempotency_conflict(client, tmp_path):
    sample = _sample(tmp_path)
    payload = {"input_uri": str(sample), "idempotency_key": "abc-123"}
    first = client.post("/v1/jobs", json=payload)
    assert first.status_code == 202
    second = client.post("/v1/jobs", json=payload)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "conflict"


def test_cancel_unknown_job_is_404(client):
    assert client.post("/v1/jobs/fj_missing/cancel").status_code == 404


def test_cancel_finished_job_conflicts(client, tmp_path):
    sample = _sample(tmp_path)
    created = client.post("/v1/jobs", json={"input_uri": str(sample)}).json()
    deadline = time.time() + 60
    while time.time() < deadline:
        state = client.get(f"/v1/jobs/{created['job_id']}").json()
        if state["status"] in {"completed", "failed", "cancelled"}:
            break
        time.sleep(0.2)
    response = client.post(f"/v1/jobs/{created['job_id']}/cancel")
    assert response.status_code == 409


def test_outputs_endpoint(client, tmp_path):
    sample = _sample(tmp_path)
    created = client.post("/v1/jobs", json={"input_uri": str(sample)}).json()
    response = client.get(f"/v1/jobs/{created['job_id']}/outputs")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #
def test_invalid_stage_rejected(client, tmp_path):
    sample = _sample(tmp_path)
    response = client.post("/v1/jobs", json={"input_uri": str(sample), "stages": ["nope"]})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_invalid_fps_rejected(client, tmp_path):
    sample = _sample(tmp_path)
    response = client.post(
        "/v1/jobs",
        json={"input_uri": str(sample), "options": {"interpolate": {"target_fps": 240}}},
    )
    assert response.status_code == 422


def test_invalid_uri_scheme_rejected(client):
    response = client.post("/v1/jobs", json={"input_uri": "ftp://example.com/x.mp4"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_local_path_outside_work_dir_rejected(client):
    response = client.post("/v1/jobs", json={"input_uri": "/etc/passwd"})
    assert response.status_code == 422


def test_missing_input_uri_rejected(client):
    response = client.post("/v1/jobs", json={})
    assert response.status_code == 422


def test_unknown_option_rejected(client, tmp_path):
    sample = _sample(tmp_path)
    response = client.post(
        "/v1/jobs",
        json={"input_uri": str(sample), "options": {"scene_detect": {"unknown": 1}}},
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# authentication
# --------------------------------------------------------------------------- #
def test_api_key_is_required_when_configured(tmp_path):
    from fastapi.testclient import TestClient

    settings = Settings(
        app_env="test",
        api_key="unit-test-key",
        work_dir=tmp_path / "work",
        database_url=f"sqlite:///{tmp_path / 'jobs.db'}",
    )
    repository = SQLiteJobRepository(tmp_path / "jobs.db")
    app = create_app(settings=settings, repository=repository)
    with TestClient(app) as test_client:
        assert test_client.get("/health").status_code == 200
        unauthorised = test_client.get("/v1/jobs/fj_x")
        assert unauthorised.status_code == 401
        authorised = test_client.get("/v1/jobs/fj_x", headers={"X-API-Key": "unit-test-key"})
        assert authorised.status_code == 404
        wrong = test_client.get("/v1/jobs/fj_x", headers={"X-API-Key": "nope"})
        assert wrong.status_code == 401
