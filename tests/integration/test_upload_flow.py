# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The presigned upload flow.

A client asks for an upload URL, PUTs the file to object storage and then
creates a job with the returned ``input_uri``. No credentials are handed to the
client and no file travels through the API.

The object store is a fake in these tests: no credentials, no network.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.errors import StorageError
from app.jobs.repository import SQLiteJobRepository
from app.main import create_app


class FakeSigningStorage:
    """Local backend extended with presigned uploads."""

    name = "fake-signing"

    def __init__(self) -> None:
        self.presigned: list[tuple[str, int]] = []

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

    def presign_put(self, key: str, expires: int | None = None) -> str | None:
        self.presigned.append((key, int(expires or 0)))
        return f"https://s3.example/{key}?X-Amz-Signature=fake"

    def presign_get(self, key: str, expires: int | None = None) -> str | None:
        return None

    def acquire(self, uri: str, destination: Path) -> Path:
        raise StorageError("fake storage cannot download")

    def publish(self, local_path: Path, key: str) -> str:
        return f"s3://bucket/{key}"

    def exists(self, key: str) -> bool:
        return False

    def delete(self, key: str) -> None:
        return None

    def health(self) -> dict:
        return {"backend": self.name, "ok": True}


@pytest.fixture()
def signing_client(tmp_path):
    settings = Settings(
        app_env="test",
        work_dir=tmp_path / "work",
        database_url=f"sqlite:///{tmp_path / 'jobs.db'}",
        storage_backend="s3",
        s3_bucket="bucket",
        log_level="WARNING",
    )
    repository = SQLiteJobRepository(tmp_path / "jobs.db")
    app = create_app(settings=settings, repository=repository)
    storage = FakeSigningStorage()
    with TestClient(app) as client:
        app.state.storage = storage
        client.app_state = app.state  # type: ignore[attr-defined]
        yield client, storage


def test_upload_url_is_created(signing_client):
    client, storage = signing_client
    response = client.post("/v1/jobs/upload-url", json={"filename": "rushes.mp4"})
    assert response.status_code == 200
    body = response.json()
    assert body["method"] == "PUT"
    assert body["upload_url"].startswith("https://s3.example/inputs/uploads/")
    assert body["input_uri"].startswith("s3://bucket/inputs/uploads/")
    assert body["expires_in"] == 900
    assert storage.presigned  # the storage backend was asked to sign


def test_upload_url_rejects_traversal(signing_client):
    client, _storage = signing_client
    response = client.post("/v1/jobs/upload-url", json={"filename": "../../etc/passwd"})
    assert response.status_code == 422


def test_upload_url_rejects_a_path_separator(signing_client):
    client, _storage = signing_client
    response = client.post("/v1/jobs/upload-url", json={"filename": "a/b.mp4"})
    assert response.status_code == 422


def test_local_backend_reports_that_uploads_are_unavailable(tmp_path):
    """The local backend has nothing to sign — it says so instead of guessing."""
    settings = Settings(
        app_env="test",
        work_dir=tmp_path / "work",
        database_url=f"sqlite:///{tmp_path / 'jobs.db'}",
        storage_backend="local",
        log_level="WARNING",
    )
    repository = SQLiteJobRepository(tmp_path / "jobs.db")
    app = create_app(settings=settings, repository=repository)
    with TestClient(app) as client:
        response = client.post("/v1/jobs/upload-url", json={"filename": "rushes.mp4"})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "conflict"
