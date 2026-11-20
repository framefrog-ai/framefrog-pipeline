# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Error taxonomy.

Every failure that can reach a client has a stable machine-readable code and
an HTTP status. Internal detail (stack traces, file system paths, subprocess
stderr) goes into the structured log, never into the response body.
"""

from __future__ import annotations

from typing import Any


class FrameFrogError(Exception):
    """Base class for every error raised by the application."""

    code: str = "internal_error"
    status_code: int = 500

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}
        self.cause = cause

    def to_payload(self) -> dict[str, Any]:
        """Client-safe representation."""
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            }
        }


class ConfigurationError(FrameFrogError):
    code = "configuration_error"
    status_code = 500


class ValidationError(FrameFrogError):
    code = "validation_error"
    status_code = 422


class AuthenticationError(FrameFrogError):
    code = "authentication_error"
    status_code = 401


class AuthorizationError(FrameFrogError):
    code = "authorization_error"
    status_code = 403


class NotFoundError(FrameFrogError):
    code = "not_found"
    status_code = 404


class ConflictError(FrameFrogError):
    code = "conflict"
    status_code = 409


class RateLimitError(FrameFrogError):
    code = "rate_limited"
    status_code = 429


class MediaValidationError(FrameFrogError):
    code = "media_validation_error"
    status_code = 422


class CapabilityError(FrameFrogError):
    """A required capability (CUDA, a model, a binary) is unavailable."""

    code = "capability_unavailable"
    status_code = 503


class StorageError(FrameFrogError):
    code = "storage_error"
    status_code = 502


class StageError(FrameFrogError):
    code = "stage_failed"
    status_code = 500


class MediaProcessingError(StageError):
    code = "media_processing_error"


class JobCancelledError(FrameFrogError):
    code = "job_cancelled"
    status_code = 409
