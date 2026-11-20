# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Structured logging.

Log lines are JSON objects with a fixed envelope:

    {"timestamp", "level", "event", "job_id", "stage",
     "duration_seconds", "error_code", ...}

``job_id``, ``stage``, ``duration_seconds`` and ``error_code`` are present
only when they are known — they are never filled with a placeholder zero.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any

LOGGER_NAME = "framefrog"

#: Keys that carry measured values. They are omitted when unknown.
ENVELOPE_KEYS = ("event", "job_id", "stage", "duration_seconds", "error_code")

#: Query parameters that must never be written to a log line.
SENSITIVE_QUERY_KEYS = (
    "X-Amz-Signature",
    "X-Amz-Credential",
    "X-Amz-Security-Token",
    "token",
    "signature",
    "sig",
    "key",
    "password",
    "secret",
)


class JsonFormatter(logging.Formatter):
    """Render log records as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        event = getattr(record, "event", None)
        if event:
            payload["event"] = event
        for key in ("job_id", "stage", "duration_seconds", "error_code"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        for key, value in getattr(record, "extra_fields", {}).items():
            if key not in payload:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> logging.Logger:
    """Install the JSON formatter on the FrameFrog logger."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Return the application logger (or a child of it)."""
    base = logging.getLogger(LOGGER_NAME)
    if name:
        return base.getChild(name)
    return base


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    job_id: str | None = None,
    stage: str | None = None,
    duration_seconds: float | None = None,
    error_code: str | None = None,
    **fields: Any,
) -> None:
    """Emit one structured event."""
    extra: dict[str, Any] = {
        "event": event,
        "extra_fields": {k: v for k, v in fields.items() if v is not None},
    }
    if job_id is not None:
        extra["job_id"] = job_id
    if stage is not None:
        extra["stage"] = stage
    if duration_seconds is not None:
        extra["duration_seconds"] = round(float(duration_seconds), 6)
    if error_code is not None:
        extra["error_code"] = error_code
    logger.log(level, event, extra=extra)


def redact_url(url: str) -> str:
    """Strip query parameters (and their values) from a URL for logging."""
    if not url:
        return url
    base, sep, query = url.partition("?")
    if not sep:
        return base
    kept = []
    for pair in query.split("&"):
        key = pair.split("=", 1)[0]
        if key in SENSITIVE_QUERY_KEYS:
            kept.append(f"{key}=***")
        else:
            kept.append(pair)
    return f"{base}?{'&'.join(kept)}"


def redact_text(text: str, secrets: dict[str, str] | None = None) -> str:
    """Replace known secret values with ``***``."""
    if not text:
        return text
    out = text
    for value in (secrets or {}).values():
        if value:
            out = out.replace(value, "***")
    return out


def env_flag(name: str, default: bool = False) -> bool:
    """Read a boolean environment flag."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
