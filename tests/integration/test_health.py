# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The HTTP API, version 1.

    GET  /health              liveness
    GET  /ready               readiness
    POST /v1/jobs             create a job (202, returns immediately)
    GET  /v1/jobs/{job_id}    job state, stage results, outputs
    POST /v1/jobs/{job_id}/cancel
    GET  /v1/jobs/{job_id}/outputs
    GET  /v1/jobs/{job_id}/download
    POST /v1/jobs/upload-url  presigned upload URL

Authentication: `X-API-Key: <key>` or `Authorization: Bearer <key>`. Outside
development mode a key is mandatory; the process refuses to start in
production without one.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.jobs.repository import SQLiteJobRepository
from app.main import create_app


@pytest.fixture()
def client(tmp_path):
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
        yield test_client


def test_health_reports_the_version_and_uptime(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["version"]
    assert body["uptime_s"] >= 0
    assert body["app_env"] == "test"


def test_ready_reports_every_dependency(client):
    body = client.get("/ready").json()
    assert body["status"] == "ready"
    for check in ("ffmpeg", "ffprobe", "database", "storage", "runner"):
        assert body["checks"][check]["ok"] is True, check


def test_ready_reports_the_optional_runtimes(client):
    """The model runtimes are reported but never make the service unready."""
    body = client.get("/ready").json()
    models = body["checks"]["models"]
    assert models["ok"] is True
    assert "deepfilternet" in models
    assert "rife_available" in models
    assert "available" in body["checks"]["gpu"]


def test_ready_fails_when_a_dependency_is_broken(client, monkeypatch):
    """A broken dependency turns readiness into a 503."""
    import app.api.routes_health as routes_health

    monkeypatch.setattr(routes_health.capabilities, "ffmpeg_info", lambda *a, **k: {"available": False})
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"


def test_root_lists_the_endpoints(client):
    body = client.get("/").json()
    assert "POST /v1/jobs" in body["endpoints"]
    assert body["docs"] == "/docs"


def test_unknown_job_is_a_structured_404(client):
    response = client.get("/v1/jobs/fj_nope")
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    assert body["error"]["message"]
