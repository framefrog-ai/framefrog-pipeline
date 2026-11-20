# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Local filesystem storage.

The local backend stores everything under ``WORK_DIR``:

    WORK_DIR/
        inputs/<job_id>/source.mp4
        intermediate/<job_id>/...
        outputs/<job_id>/master.mp4

Every path is built from sanitised key segments, so a job id or file name can
never escape the root.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from app.core.config import Settings
from app.core.errors import StorageError
from app.core.logging import get_logger
from app.storage.base import StorageBackend

log = get_logger("storage.local")


class LocalStorage(StorageBackend):
    """Filesystem-backed storage rooted at ``WORK_DIR``."""

    name = "local"

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.root = settings.work_dir_resolved
        self.root.mkdir(parents=True, exist_ok=True)

    # -- paths -------------------------------------------------------------- #
    def path_for(self, key: str) -> Path:
        """Absolute path of an object key inside the storage root."""
        relative = self.build_key("", key)
        candidate = (self.root / relative).resolve(strict=False)
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:  # pragma: no cover - build_key prevents this
            raise StorageError(f"object key escapes the storage root: {key!r}") from exc
        return candidate

    def ensure_dir(self, key: str) -> Path:
        directory = self.path_for(key)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    # -- contract ------------------------------------------------------------ #
    def acquire(self, uri: str, destination: Path) -> Path:
        """Copy a local input file to ``destination``."""
        source = Path(uri)
        if not source.exists():
            raise StorageError(f"input not found: {source}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        return destination

    def publish(self, local_path: Path, key: str) -> str:
        """Copy an artifact into the storage root and return its path."""
        target = self.path_for(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local_path, target)
        return str(target)

    def exists(self, key: str) -> bool:
        return self.path_for(key).exists()

    def delete(self, key: str) -> None:
        target = self.path_for(key)
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            try:
                target.unlink()
            except OSError as exc:  # pragma: no cover - permissions
                log.warning("delete_failed", extra={"extra_fields": {"error": str(exc)}})

    def presign_get(self, key: str, expires: int | None = None) -> str | None:
        """Local files are not served over a signed URL."""
        return None

    def health(self) -> dict:
        writable = os.access(self.root, os.W_OK)
        return {
            "backend": self.name,
            "ok": writable,
            "root": str(self.root),
            "reason": None if writable else "storage root is not writable",
        }
