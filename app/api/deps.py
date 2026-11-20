# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared FastAPI dependencies."""

from __future__ import annotations

from typing import Any

from fastapi import Header, Request

from app.core.config import Settings
from app.core.errors import AuthenticationError
from app.core.security import verify_api_key


def get_settings_dep(request: Request) -> Settings:
    """The settings this process was started with."""
    return request.app.state.settings


def get_repository(request: Request) -> Any:
    """The job repository."""
    return request.app.state.repository


def get_runner(request: Request) -> Any:
    """The background job runner."""
    return request.app.state.runner


def get_storage(request: Request) -> Any:
    """The storage backend."""
    return request.app.state.storage


def require_auth(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> None:
    """Authenticate the caller.

    Authentication is mandatory outside explicit development mode. The key is
    accepted either as ``X-API-Key`` or as ``Authorization: Bearer <key>`` and
    is compared in constant time.
    """
    settings: Settings = request.app.state.settings
    if not settings.auth_required:
        return
    expected = settings.api_key.get_secret_value() if settings.api_key else ""
    provided = x_api_key
    if not provided and authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer":
            provided = token.strip()
    try:
        verify_api_key(provided, expected)
    except AuthenticationError as exc:
        raise AuthenticationError(
            "missing or invalid API key",
            details={"header": "X-API-Key"},
        ) from exc
