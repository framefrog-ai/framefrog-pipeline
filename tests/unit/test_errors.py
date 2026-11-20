# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Error taxonomy: stable codes and client-safe payloads."""

from __future__ import annotations

from app.core.errors import (
    AuthenticationError,
    CapabilityError,
    ConflictError,
    FrameFrogError,
    MediaValidationError,
    NotFoundError,
    ValidationError,
)


def test_error_payload_shape():
    error = ValidationError("bad request", details={"field": "input_uri"})
    payload = error.to_payload()
    assert payload["error"]["code"] == "validation_error"
    assert payload["error"]["message"] == "bad request"
    assert payload["error"]["details"] == {"field": "input_uri"}


def test_status_codes():
    assert ValidationError("x").status_code == 422
    assert AuthenticationError("x").status_code == 401
    assert NotFoundError("x").status_code == 404
    assert ConflictError("x").status_code == 409
    assert CapabilityError("x").status_code == 503
    assert MediaValidationError("x").status_code == 422
    assert FrameFrogError("x").status_code == 500


def test_error_keeps_the_cause():
    cause = ValueError("root cause")
    error = FrameFrogError("wrapper", cause=cause)
    assert error.cause is cause


def test_capability_error_carries_details():
    error = CapabilityError("no gpu", details={"required": "cuda"})
    assert error.details["required"] == "cuda"
