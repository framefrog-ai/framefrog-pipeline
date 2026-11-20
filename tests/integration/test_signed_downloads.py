# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Signed artefact downloads.

A finished job hands out links that expire and carry an HMAC token, so a
client can fetch a master without holding any other credential.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.jobs.repository import SQLiteJobRepository
from app.main import create_app

SIGNING_KEY = "signing-key-for-tests"


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
            "1",
            "--width",
            "160",
            "--height",
            "90",
        ],
        check=True,
        capture_output=True,
    )
    return target


@pytest.fixture()
def client(tmp_path):
    settings = Settings(
        app_env="test",
        work_dir=tmp_path / "work",
        database_url=f"sqlite:///{tmp_path / 'jobs.db'}",
        storage_backend="local",
        signing_secret=SIGNING_KEY,
        log_level="WARNING",
    )
    repository = SQLiteJobRepository(tmp_path / "jobs.db")
    app = create_app(settings=settings, repository=repository)
    with TestClient(app) as test_client:
        yield test_client


def _completed_job(client: TestClient, sample: Path) -> str:
    created = client.post(
        "/v1/jobs",
        json={"input_uri": str(sample), "stages": ["scene_detect", "deliver"]},
    )
    assert created.status_code == 202
    job_id = created.json()["job_id"]
    deadline = time.time() + 120
    while time.time() < deadline:
        state = client.get(f"/v1/jobs/{job_id}").json()
        if state["status"] in {"completed", "failed", "cancelled"}:
            break
        time.sleep(0.2)
    assert state["status"] == "completed", state.get("error")
    return job_id


def test_outputs_carry_a_signed_download_url(client, tmp_path):
    sample = _sample(tmp_path)
    job_id = _completed_job(client, sample)
    outputs = client.get(f"/v1/jobs/{job_id}/outputs").json()
    master = next(entry for entry in outputs if entry["kind"] == "master")
    assert "download_url" in master
    assert master["download_url"].startswith("http://testserver/v1/jobs/")
    assert "token=" in master["download_url"]


def test_signed_link_downloads_the_artifact(client, tmp_path):
    sample = _sample(tmp_path)
    job_id = _completed_job(client, sample)
    outputs = client.get(f"/v1/jobs/{job_id}/outputs").json()
    master = next(entry for entry in outputs if entry["kind"] == "master")
    response = client.get(master["download_url"])
    assert response.status_code == 200
    assert response.content[:4] == b"\x00\x00\x00\x1c" or len(response.content) > 1000


def test_tampered_token_is_rejected(client, tmp_path):
    sample = _sample(tmp_path)
    job_id = _completed_job(client, sample)
    outputs = client.get(f"/v1/jobs/{job_id}/outputs").json()
    master = next(entry for entry in outputs if entry["kind"] == "master")
    url = master["download_url"].replace("token=", "token=x")
    response = client.get(url)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_expired_link_is_rejected(client, tmp_path):
    from app.core import ops

    sample = _sample(tmp_path)
    job_id = _completed_job(client, sample)
    expired = ops.download_url("http://testserver", job_id, artifact="master", secret=SIGNING_KEY, ttl=1)
    assert expired
    time.sleep(2)
    response = client.get(expired)
    assert response.status_code == 422


def test_unknown_artifact_is_not_found(client, tmp_path):
    """A correctly signed request for an artefact that does not exist."""
    import time as _time

    from app.core import ops

    sample = _sample(tmp_path)
    job_id = _completed_job(client, sample)
    expires = int(_time.time()) + 60
    token = ops.sign_download(job_id, "nope", expires, SIGNING_KEY)
    response = client.get(f"/v1/jobs/{job_id}/download?artifact=nope&token={token}&expires={expires}")
    assert response.status_code == 404


def test_without_a_signing_key_no_link_is_issued(tmp_path):
    settings = Settings(
        app_env="test",
        work_dir=tmp_path / "work",
        database_url=f"sqlite:///{tmp_path / 'jobs.db'}",
        storage_backend="local",
        log_level="WARNING",
    )
    repository = SQLiteJobRepository(tmp_path / "jobs.db")
    app = create_app(settings=settings, repository=repository)
    sample = _sample(tmp_path)
    with TestClient(app) as client:
        job_id = _completed_job(client, sample)
        outputs = client.get(f"/v1/jobs/{job_id}/outputs").json()
        assert all("download_url" not in entry for entry in outputs)
