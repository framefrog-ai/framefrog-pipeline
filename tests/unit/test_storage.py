# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Storage backends: local filesystem and S3 (with a fake client)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.errors import StorageError
from app.storage.local import LocalStorage
from app.storage.s3 import S3Storage


def _settings(tmp_path, **kwargs):
    from app.core.config import Settings

    defaults = {"work_dir": tmp_path, "storage_backend": "local"}
    defaults.update(kwargs)
    return Settings(**defaults)


# --------------------------------------------------------------------------- #
# local
# --------------------------------------------------------------------------- #
def test_local_key_building(tmp_path):
    storage = LocalStorage(_settings(tmp_path))
    key = storage.build_key(storage.output_prefix, "fj_1", "master.mp4")
    assert key == "outputs/fj_1/master.mp4"


def test_local_key_cannot_escape_root(tmp_path):
    storage = LocalStorage(_settings(tmp_path))
    key = storage.build_key(storage.output_prefix, "../../etc", "passwd")
    assert ".." not in key
    path = storage.path_for(key)
    assert str(path).startswith(str(tmp_path.resolve()))


def test_local_publish_and_acquire(tmp_path):
    storage = LocalStorage(_settings(tmp_path))
    source = tmp_path / "artifact.mp4"
    source.write_bytes(b"data")
    published = storage.publish(source, storage.build_key(storage.output_prefix, "fj_1", "artifact.mp4"))
    assert Path(published).exists()
    assert storage.exists(storage.build_key(storage.output_prefix, "fj_1", "artifact.mp4"))
    destination = tmp_path / "copy.mp4"
    storage.acquire(str(source), destination)
    assert destination.read_bytes() == b"data"


def test_local_delete(tmp_path):
    storage = LocalStorage(_settings(tmp_path))
    source = tmp_path / "artifact.mp4"
    source.write_bytes(b"data")
    key = storage.build_key(storage.output_prefix, "fj_1", "artifact.mp4")
    storage.publish(source, key)
    assert storage.exists(key)
    storage.delete(key)
    assert storage.exists(key) is False


def test_local_acquire_missing_file(tmp_path):
    storage = LocalStorage(_settings(tmp_path))
    with pytest.raises(StorageError):
        storage.acquire(str(tmp_path / "missing.mp4"), tmp_path / "out.mp4")


def test_local_health(tmp_path):
    storage = LocalStorage(_settings(tmp_path))
    assert storage.health()["ok"] is True


# --------------------------------------------------------------------------- #
# s3 (fake client — no credentials, no network)
# --------------------------------------------------------------------------- #
class FakeS3Client:
    def __init__(self):
        self.objects = {}
        self.downloaded = []
        self.uploaded = []

    def download_file(self, bucket, key, dest):
        self.downloaded.append((bucket, key))
        Path(dest).write_bytes(b"s3-bytes")

    def upload_file(self, src, bucket, key):
        self.uploaded.append((bucket, key))
        self.objects[(bucket, key)] = Path(src).read_bytes()

    def head_object(self, Bucket, Key):  # noqa: N803 - boto3 signature
        if (Bucket, Key) not in self.objects:
            raise KeyError("missing")
        return {}

    def delete_object(self, Bucket, Key):  # noqa: N803 - boto3 signature
        self.objects.pop((Bucket, Key), None)

    def generate_presigned_url(self, operation, Params, ExpiresIn):  # noqa: N803
        return (
            f"https://s3.example/{Params['Bucket']}/{Params['Key']}"
            f"?X-Amz-Signature=deadbeef&expires={ExpiresIn}"
        )


def _s3(tmp_path, client):
    settings = _settings(tmp_path, storage_backend="s3", s3_bucket="bucket")
    return S3Storage(settings, client=client)


def test_s3_publish_and_presign(tmp_path):
    client = FakeS3Client()
    storage = _s3(tmp_path, client)
    artifact = tmp_path / "master.mp4"
    artifact.write_bytes(b"x")
    uri = storage.publish(artifact, storage.build_key(storage.output_prefix, "fj_1", "master.mp4"))
    assert uri == "s3://bucket/outputs/fj_1/master.mp4"
    assert client.uploaded
    url = storage.presign_get("outputs/fj_1/master.mp4", expires=60)
    assert "X-Amz-Signature=deadbeef" in url


def test_s3_acquire(tmp_path):
    client = FakeS3Client()
    storage = _s3(tmp_path, client)
    destination = tmp_path / "downloaded.mp4"
    storage.acquire("s3://bucket/inputs/x.mp4", destination)
    assert destination.read_bytes() == b"s3-bytes"
    assert client.downloaded == [("bucket", "inputs/x.mp4")]


def test_s3_missing_bucket(tmp_path):
    from app.core.config import Settings

    settings = Settings(work_dir=tmp_path, storage_backend="s3", s3_bucket="bucket")
    settings.s3_bucket = None
    storage = S3Storage(settings, client=FakeS3Client())
    with pytest.raises(StorageError):
        storage.publish(tmp_path / "x.mp4", "outputs/x.mp4")


def test_s3_key_traversal_is_neutralised(tmp_path):
    client = FakeS3Client()
    storage = _s3(tmp_path, client)
    artifact = tmp_path / "artifact.mp4"
    artifact.write_bytes(b"x")
    storage.publish(artifact, "outputs/../../etc/passwd")
    assert all(".." not in key for _bucket, key in client.uploaded)
