# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Publishing job outputs to object storage.

The S3 backend is exercised through a fake storage object: no credentials, no
network, no live bucket.
"""

from __future__ import annotations

import os
from pathlib import Path

from app.core.config import Settings
from app.jobs.models import JobRequest
from app.jobs.pipeline import run_job


class FakeStorage:
    """Records what the pipeline publishes."""

    name = "fake"

    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    @property
    def output_prefix(self) -> str:
        return "outputs"

    def build_key(self, prefix: str, *parts: str) -> str:
        return "/".join([prefix, *parts])

    def acquire(self, uri: str, destination: Path) -> Path:
        import shutil

        shutil.copyfile(uri, destination)
        return destination

    def publish(self, local_path: Path, key: str) -> str:
        self.published.append((str(local_path), key))
        return f"s3://bucket/{key}"

    def exists(self, key: str) -> bool:
        return False

    def delete(self, key: str) -> None:
        return None

    def health(self) -> dict:
        return {"backend": self.name, "ok": True}


def _settings(tmp_path) -> Settings:
    work_dir = tmp_path / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        app_env="test",
        work_dir=work_dir,
        database_url=f"sqlite:///{tmp_path / 'jobs.db'}",
        storage_backend="s3",
        s3_bucket="bucket",
        log_level="WARNING",
    )


def test_outputs_are_published_to_object_storage(sample_video, tmp_path):
    settings = _settings(tmp_path)
    sample = settings.work_dir_resolved / "sample.mp4"
    import shutil

    shutil.copyfile(sample_video, sample)

    storage = FakeStorage()
    manifest = run_job(
        JobRequest(input_uri=str(sample), stages=["scene_detect", "deliver"]),
        settings=settings,
        storage=storage,
    )
    assert manifest["status"] == "completed", manifest.get("error")
    assert storage.published, "nothing was published"
    for _local, key in storage.published:
        assert key.startswith("outputs/")
    kinds = {entry["kind"] for entry in manifest["outputs"]}
    assert "master" in kinds
    master = next(entry for entry in manifest["outputs"] if entry["kind"] == "master")
    assert master["uri"].startswith("s3://bucket/outputs/")
    assert os.path.exists(master["path"])


def test_local_backend_does_not_publish(sample_video, tmp_path):
    settings = _settings(tmp_path)
    settings.storage_backend = "local"
    sample = settings.work_dir_resolved / "sample.mp4"
    import shutil

    shutil.copyfile(sample_video, sample)

    storage = FakeStorage()
    manifest = run_job(
        JobRequest(input_uri=str(sample), stages=["deliver"]),
        settings=settings,
        storage=storage,
    )
    assert manifest["status"] == "completed"
    assert storage.published == []
    assert all("uri" not in entry for entry in manifest["outputs"])
