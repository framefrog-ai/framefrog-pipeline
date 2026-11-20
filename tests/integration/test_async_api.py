# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The API under an async client.

The service is FastAPI/ASGI, so it must behave correctly when many requests
arrive concurrently. These tests drive the app through an ASGI transport with
``httpx.AsyncClient`` (no server, no lifespan) and submit jobs in parallel.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from app.core.config import Settings
from app.jobs.repository import SQLiteJobRepository
from app.main import create_app, initialise_runtime, shutdown_runtime

pytestmark = pytest.mark.asyncio


@pytest.fixture()
def async_app(tmp_path):
    """An app with an isolated database, initialised without the lifespan."""
    settings = Settings(
        app_env="test",
        work_dir=tmp_path / "work",
        database_url=f"sqlite:///{tmp_path / 'jobs.db'}",
        storage_backend="local",
        log_level="WARNING",
    )
    repository = SQLiteJobRepository(tmp_path / "jobs.db")
    app = create_app(settings=settings, repository=repository)
    initialise_runtime(app, settings, repository)
    try:
        yield app
    finally:
        shutdown_runtime(app)


def _sample(tmp_path) -> Path:
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


async def test_health_and_readiness_are_reachable_concurrently(async_app):
    transport = httpx.ASGITransport(app=async_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        health, ready = await asyncio.gather(client.get("/health"), client.get("/ready"))
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"


async def test_jobs_can_be_submitted_in_parallel(async_app, tmp_path):
    sample = _sample(tmp_path)
    transport = httpx.ASGITransport(app=async_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        responses = await asyncio.gather(
            *[
                client.post(
                    "/v1/jobs",
                    json={"input_uri": str(sample), "stages": ["scene_detect"]},
                )
                for _ in range(4)
            ]
        )
    assert all(response.status_code == 202 for response in responses)
    job_ids = {response.json()["job_id"] for response in responses}
    assert len(job_ids) == 4, "every job must get its own id"

    # every job is persisted and retrievable
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        fetched = await asyncio.gather(*[client.get(f"/v1/jobs/{job_id}") for job_id in job_ids])
    assert all(response.status_code == 200 for response in fetched)
    assert {response.json()["job_id"] for response in fetched} == job_ids


async def test_concurrent_requests_reject_invalid_input_without_crashing(async_app):
    transport = httpx.ASGITransport(app=async_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        responses = await asyncio.gather(
            *[client.post("/v1/jobs", json={"input_uri": f"/etc/passwd-{index}"}) for index in range(5)]
        )
    assert [response.status_code for response in responses] == [422] * 5
