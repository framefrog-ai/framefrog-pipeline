# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Storage backends: local filesystem and S3-compatible object storage."""

from app.storage.base import StorageBackend, storage_from_settings

__all__ = ["StorageBackend", "storage_from_settings"]
