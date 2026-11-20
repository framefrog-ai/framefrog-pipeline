# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Storage abstraction.

Two backends implement the same contract:

* :class:`app.storage.local.LocalStorage` — the filesystem under ``WORK_DIR``
* :class:`app.storage.s3.S3Storage` — any S3-compatible service (AWS, R2,
  MinIO) via boto3

Object keys are always built through :func:`safe_object_key`, which removes
path segments that could escape the prefix.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from app.core.config import Settings
from app.core.errors import StorageError
from app.core.security import safe_object_key


class StorageBackend(ABC):
    """Common interface for every storage backend."""

    name: str = "storage"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # -- keys -------------------------------------------------------------- #
    @property
    def input_prefix(self) -> str:
        return safe_object_key(self.settings.s3_input_prefix)

    @property
    def intermediate_prefix(self) -> str:
        return safe_object_key(self.settings.s3_intermediate_prefix)

    @property
    def output_prefix(self) -> str:
        return safe_object_key(self.settings.s3_output_prefix)

    def build_key(self, prefix: str, *parts: str) -> str:
        """Join a prefix and untrusted parts into a safe object key."""
        return safe_object_key(prefix, *parts)

    # -- contract ---------------------------------------------------------- #
    @abstractmethod
    def acquire(self, uri: str, destination: Path) -> Path:
        """Materialise the object behind ``uri`` at ``destination``."""

    @abstractmethod
    def publish(self, local_path: Path, key: str) -> str:
        """Upload ``local_path`` and return the resulting URI."""

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Whether the object exists."""

    @abstractmethod
    def delete(self, key: str) -> None:
        """Remove an object (no-op when it does not exist)."""

    def presign_get(self, key: str, expires: int | None = None) -> str | None:
        """Time-limited download URL, when the backend supports it."""
        return None

    def presign_put(self, key: str, expires: int | None = None) -> str | None:
        """Time-limited upload URL, when the backend supports it."""
        return None

    def health(self) -> dict:
        """Non-secret health snapshot for ``/ready``."""
        return {"backend": self.name, "ok": True}


def storage_from_settings(settings: Settings) -> StorageBackend:
    """Build the storage backend selected by ``STORAGE_BACKEND``."""
    if settings.storage_backend == "s3":
        from app.storage.s3 import S3Storage

        return S3Storage(settings)
    from app.storage.local import LocalStorage

    return LocalStorage(settings)


__all__ = [
    "StorageBackend",
    "StorageError",
    "safe_object_key",
    "storage_from_settings",
]
