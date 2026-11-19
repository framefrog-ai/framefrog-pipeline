# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Object storage handoff.

Stages exchange artifacts through files on the worker's disk; this module is
the only place that knows how to pull ``s3://`` rushes in and push results
back. boto3 is optional so local runs stay dependency-light.
"""

from __future__ import annotations

import os
import shutil
import urllib.parse
from typing import Optional


def is_remote(uri: str) -> bool:
    return uri.startswith("s3://")


def resolve_input(uri: str, workdir: str) -> str:
    """Return a local path for ``uri``, downloading from S3 if needed."""
    if not is_remote(uri):
        return os.path.abspath(uri)

    if shutil.which("aws") is None:
        try:
            import boto3  # noqa: F401
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise RuntimeError(
                "s3:// input requires boto3 or the aws CLI; "
                "install with `pip install boto3`"
            ) from exc

    parts = urllib.parse.urlparse(uri)
    bucket, key = parts.netloc, parts.path.lstrip("/")
    if not bucket or not key:
        raise ValueError(f"malformed s3 uri: {uri}")

    os.makedirs(workdir, exist_ok=True)
    dest = os.path.join(workdir, "input_" + os.path.basename(key))
    _download(bucket, key, dest)
    return dest


def publish_output(local_path: str, uri: str) -> str:
    """Upload a local artifact to ``uri`` (s3://bucket/key)."""
    if not is_remote(uri):
        return os.path.abspath(local_path)
    parts = urllib.parse.urlparse(uri)
    _upload(parts.netloc, parts.path.lstrip("/"), local_path)
    return uri


def _download(bucket: str, key: str, dest: str) -> None:  # pragma: no cover
    import boto3

    boto3.client("s3").download_file(bucket, key, dest)


def _upload(bucket: str, key: str, src: str) -> None:  # pragma: no cover
    import boto3

    boto3.client("s3").upload_file(src, bucket, key)


def output_uri_for(job_id: str, filename: str, bucket: Optional[str]) -> str:
    if bucket:
        return f"s3://{bucket}/framefrog/{job_id}/{filename}"
    return ""
