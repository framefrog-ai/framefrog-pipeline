# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""S3-compatible object storage (AWS S3, Cloudflare R2, MinIO, …).

boto3 is imported lazily so that a CPU box without object storage installed
can still run the whole pipeline locally.

Credentials never appear in a log line, and presigned URLs are only ever
recorded in their redacted form (query string stripped) — the signed query
itself is returned to the caller and nowhere else.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.core.config import Settings
from app.core.errors import StorageError
from app.core.logging import get_logger, redact_url
from app.storage.base import StorageBackend

log = get_logger("storage.s3")


class S3Storage(StorageBackend):
    """Object storage backed by boto3."""

    name = "s3"

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        super().__init__(settings)
        self.bucket = settings.s3_bucket or ""
        self._client = client

    # -- client -------------------------------------------------------------- #
    @property
    def client(self) -> Any:
        """Lazily built boto3 client (injectable for tests)."""
        if self._client is None:
            try:
                import boto3  # type: ignore
            except ImportError as exc:
                raise StorageError("S3 storage requires boto3: pip install 'framefrog[s3]'") from exc
            kwargs: dict[str, Any] = {"region_name": self.settings.s3_region}
            if self.settings.s3_endpoint_url:
                # Cloudflare R2 and other S3-compatible endpoints.
                kwargs["endpoint_url"] = self.settings.s3_endpoint_url
            access_key = self.settings.s3_access_key_id
            secret_key = self.settings.s3_secret_access_key
            if access_key and secret_key:
                kwargs["aws_access_key_id"] = access_key.get_secret_value()
                kwargs["aws_secret_access_key"] = secret_key.get_secret_value()
            self._client = boto3.client("s3", **kwargs)
        return self._client

    def _require_bucket(self) -> str:
        if not self.bucket:
            raise StorageError("S3_BUCKET is not configured")
        return self.bucket

    # -- contract ------------------------------------------------------------- #
    def acquire(self, uri: str, destination: Path) -> Path:
        """Download an ``s3://bucket/key`` object to ``destination``."""
        parts = urlparse(uri)
        bucket = parts.netloc or self._require_bucket()
        key = parts.path.lstrip("/")
        if not key:
            raise StorageError(f"malformed s3 uri: {uri}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.client.download_file(bucket, key, str(destination))
        except Exception as exc:
            raise StorageError(
                f"failed to download s3://{bucket}/{key}: {type(exc).__name__}: {exc}"
            ) from exc
        return destination

    def publish(self, local_path: Path, key: str) -> str:
        """Upload an artifact and return its ``s3://`` URI."""
        bucket = self._require_bucket()
        safe_key = self.build_key("", key)
        try:
            self.client.upload_file(str(local_path), bucket, safe_key)
        except Exception as exc:
            raise StorageError(
                f"failed to upload s3://{bucket}/{safe_key}: {type(exc).__name__}: {exc}"
            ) from exc
        return f"s3://{bucket}/{safe_key}"

    def exists(self, key: str) -> bool:
        bucket = self._require_bucket()
        safe_key = self.build_key("", key)
        try:
            self.client.head_object(Bucket=bucket, Key=safe_key)
        except Exception:
            return False
        return True

    def delete(self, key: str) -> None:
        bucket = self._require_bucket()
        safe_key = self.build_key("", key)
        try:
            self.client.delete_object(Bucket=bucket, Key=safe_key)
        except Exception as exc:  # pragma: no cover - network dependent
            log.warning(
                "delete_failed",
                extra={"extra_fields": {"key": safe_key, "error": type(exc).__name__}},
            )

    def presign_get(self, key: str, expires: int | None = None) -> str | None:
        """Presigned download URL (S3-compatible endpoints support it)."""
        bucket = self._require_bucket()
        safe_key = self.build_key("", key)
        ttl = int(expires or self.settings.presigned_url_expiry_seconds)
        try:
            url = self.client.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": safe_key},
                ExpiresIn=ttl,
            )
        except Exception as exc:
            raise StorageError(f"failed to presign download for {safe_key}: {type(exc).__name__}") from exc
        log.info(
            "presigned_download",
            extra={"extra_fields": {"key": safe_key, "url": redact_url(url), "expires_s": ttl}},
        )
        return url

    def presign_put(self, key: str, expires: int | None = None) -> str | None:
        """Presigned upload URL."""
        bucket = self._require_bucket()
        safe_key = self.build_key("", key)
        ttl = int(expires or self.settings.presigned_url_expiry_seconds)
        try:
            url = self.client.generate_presigned_url(
                "put_object",
                Params={"Bucket": bucket, "Key": safe_key},
                ExpiresIn=ttl,
            )
        except Exception as exc:
            raise StorageError(f"failed to presign upload for {safe_key}: {type(exc).__name__}") from exc
        log.info(
            "presigned_upload",
            extra={"extra_fields": {"key": safe_key, "url": redact_url(url), "expires_s": ttl}},
        )
        return url

    def health(self) -> dict:
        """Report whether the bucket is reachable (never leaks credentials)."""
        if not self.bucket:
            return {"backend": self.name, "ok": False, "reason": "S3_BUCKET is not configured"}
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except Exception as exc:
            return {
                "backend": self.name,
                "ok": False,
                "bucket": self.bucket,
                "reason": f"{type(exc).__name__}",
            }
        return {"backend": self.name, "ok": True, "bucket": self.bucket}
