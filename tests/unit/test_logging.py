# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Structured logging and redaction."""

from __future__ import annotations

import json
import logging

from app.core.logging import JsonFormatter, configure_logging, log_event, redact_text, redact_url


def _record(**extra):
    record = logging.LogRecord(
        name="framefrog.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_envelope_contains_measured_fields():
    payload = json.loads(
        JsonFormatter().format(
            _record(event="job_finished", job_id="fj_1", stage="deliver", duration_seconds=1.5)
        )
    )
    assert payload["event"] == "job_finished"
    assert payload["job_id"] == "fj_1"
    assert payload["stage"] == "deliver"
    assert payload["duration_seconds"] == 1.5
    assert payload["level"] == "INFO"
    assert payload["timestamp"].endswith("Z")


def test_unknown_fields_are_omitted():
    payload = json.loads(JsonFormatter().format(_record(event="x")))
    for key in ("job_id", "stage", "duration_seconds", "error_code"):
        assert key not in payload


def test_log_event_writes_the_envelope(capsys):
    logger = configure_logging("INFO")
    log_event(logger, "job_created", job_id="fj_2", extra_field="value")
    out = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(out)
    assert payload["event"] == "job_created"
    assert payload["job_id"] == "fj_2"
    assert payload["extra_field"] == "value"


def test_redact_url_strips_query_values():
    url = "https://s3.example/bucket/key?X-Amz-Signature=deadbeef&other=1"
    redacted = redact_url(url)
    assert "deadbeef" not in redacted
    assert "other=1" in redacted


def test_redact_url_without_query():
    assert redact_url("https://framefrog.me/") == "https://framefrog.me/"


def test_redact_text_replaces_secrets():
    assert "s3cret" not in redact_text("token=s3cret", {"token": "s3cret"})
