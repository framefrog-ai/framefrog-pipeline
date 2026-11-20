# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Output links for a job.

With object storage the outputs carry a presigned URL; with the local
backend they carry an HMAC-signed link to the download route — and with
neither configured, no link at all is issued.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.jobs.models import JobRecord
from app.jobs.repository import SQLiteJobRepository
from app.jobs.states import JobState
from app.main import create_app


class FakeObjectStorage:
    """An object store that signs downloads."""

    name = "fake-objects"

    def __init__(self) -> None:
        self.signed: list[tuple[str, int]] = []

    @property
    def input_prefix(self) -> str:
        return "inputs"

    @property
    def output_prefix(self) -> str:
        return "outputs"

    @property
    def intermediate_prefix(self) -> str:
        return "intermediate"

    def build_key(self, prefix: str, *parts: str) -> str:
        return "/".join([prefix, *[p for p in parts if p]])

    def presign_get(self, key: str, expires: int | None = None) -> str | None:
        self.signed.append((key, int(expires or 0)))
        return f"https://s3.example/{key}?X-Amz-Signature=deadbeef"

    def presign_put(self, key: str, expires: int | None = None) -> str | None:
        return f"https://s3.example/{key}?X-Amz-Signature=put"

    def acquire(self, uri: str, destination: Path) -> Path:
        raise FileNotFoundError(uri)

    def publish(self, local_path: Path, key: str) -> str:
        return f"s3://bucket/{key}"

    def exists(self, key: str) -> bool:
        return False

    def delete(self, key: str) -> None:
        return None

    def health(self) -> dict:
        return {"backend": self.name, "ok": True}


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


def _settings(tmp_path, **overrides) -> Settings:
    defaults = {
        "app_env": "test",
        "work_dir": tmp_path / "work",
        "database_url": f"sqlite:///{tmp_path / 'jobs.db'}",
        "storage_backend": "s3",
        "s3_bucket": "bucket",
        "log_level": "WARNING",
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _completed(client: TestClient, job_id: str) -> dict:
    deadline = time.time() + 180
    state: dict = {}
    while time.time() < deadline:
        state = client.get(f"/v1/jobs/{job_id}").json()
        if state["status"] in {"completed", "failed", "cancelled"}:
            break
        time.sleep(0.2)
    return state


def test_outputs_carry_a_presigned_object_store_url(tmp_path):
    """A published artifact gets a presigned URL from the object store."""
    storage = FakeObjectStorage()
    settings = _settings(tmp_path)
    repository = SQLiteJobRepository(tmp_path / "jobs.db")
    app = create_app(settings=settings, repository=repository)
    with TestClient(app) as client:
        app.state.storage = storage
        repository.create(
            JobRecord(
                job_id="fj_stored",
                status=JobState.COMPLETED.value,
                request={"input_uri": "s3://bucket/in.mp4", "stages": ["deliver"], "options": {}},
                outputs=[
                    {
                        "kind": "master",
                        "path": str(tmp_path / "work" / "jobs" / "fj_stored" / "work" / "05_master.mp4"),
                        "key": "outputs/fj_stored/05_master.mp4",
                        "uri": "s3://bucket/outputs/fj_stored/05_master.mp4",
                        "size_bytes": 1024,
                    }
                ],
            )
        )
        outputs = client.get("/v1/jobs/fj_stored/outputs").json()

    assert outputs[0]["download_url"].startswith("https://s3.example/outputs/fj_stored/05_master.mp4")
    assert outputs[0]["expires_in"] == settings.presigned_url_expiry_seconds
    assert storage.signed == [("outputs/fj_stored/05_master.mp4", settings.presigned_url_expiry_seconds)]


def test_download_route_serves_the_artifact(tmp_path):
    """A signed link downloads the artefact; a tampered one does not."""
    from app.core import ops

    settings = _settings(tmp_path, storage_backend="local", signing_secret="signing-key")
    repository = SQLiteJobRepository(tmp_path / "jobs.db")
    app = create_app(settings=settings, repository=repository)
    sample = _sample(tmp_path)
    with TestClient(app) as client:
        created = client.post(
            "/v1/jobs",
            json={"input_uri": str(sample), "stages": ["deliver"]},
        )
        job_id = created.json()["job_id"]
        state = _completed(client, job_id)
        assert state["status"] == "completed", state.get("error")

        outputs = client.get(f"/v1/jobs/{job_id}/outputs").json()
        master = next(entry for entry in outputs if entry["kind"] == "master")
        assert master["download_url"].startswith("http://testserver/v1/jobs/")

        response = client.get(master["download_url"])
        assert response.status_code == 200
        assert len(response.content) > 1000

        tampered = master["download_url"].replace("token=", "token=x")
        assert client.get(tampered).status_code == 422

        expires = int(time.time()) + 60
        token = ops.sign_download(job_id, "master", expires, "signing-key")
        ok = client.get(f"/v1/jobs/{job_id}/download?artifact=master&token={token}&expires={expires}")
        assert ok.status_code == 200


def test_no_link_without_a_key_or_object_store(tmp_path):
    """No signing key and no object store means no link at all."""
    settings = _settings(tmp_path, storage_backend="local")
    repository = SQLiteJobRepository(tmp_path / "jobs.db")
    app = create_app(settings=settings, repository=repository)
    with TestClient(app) as client:
        repository.create(
            JobRecord(
                job_id="fj_plain",
                status=JobState.COMPLETED.value,
                request={"input_uri": "s3://bucket/in.mp4", "stages": ["deliver"], "options": {}},
                outputs=[{"kind": "master", "path": str(tmp_path / "master.mp4")}],
            )
        )
        outputs = client.get("/v1/jobs/fj_plain/outputs").json()
    assert "download_url" not in outputs[0]
