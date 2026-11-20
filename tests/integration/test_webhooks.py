# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Job webhooks.

A job can be told to notify a URL when it finishes. The delivery is
best-effort and never changes the job's status, but every attempt is recorded
in the manifest.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from app.core.config import Settings
from app.jobs.models import JobRequest
from app.jobs.pipeline import run_job


class _Receiver(BaseHTTPRequestHandler):
    deliveries: list[dict] = []
    status_code = 200

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            payload = {"_raw": raw.decode("utf-8", "replace")}
        _Receiver.deliveries.append({"payload": payload, "headers": dict(self.headers)})
        self.send_response(self.status_code)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args) -> None:  # keep the test output quiet
        return


@pytest.fixture()
def receiver():
    _Receiver.deliveries = []
    server = HTTPServer(("127.0.0.1", 0), _Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}/hook", _Receiver
    finally:
        server.shutdown()
        server.server_close()


def _settings(tmp_path) -> Settings:
    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    return Settings(
        app_env="test",
        work_dir=work,
        database_url=f"sqlite:///{tmp_path / 'jobs.db'}",
        storage_backend="local",
        log_level="WARNING",
    )


def _sample(settings: Settings, sample_video: Path) -> Path:
    import shutil

    target = settings.work_dir_resolved / "sample.mp4"
    shutil.copyfile(sample_video, target)
    return target


def test_manifest_is_delivered_to_the_webhook(receiver, sample_video, tmp_path):
    url, receiver_class = receiver
    settings = _settings(tmp_path)
    sample = _sample(settings, sample_video)
    manifest = run_job(
        JobRequest(
            input_uri=str(sample),
            stages=["scene_detect"],
            options={"webhook_url": url, "webhook_secret": "s3cret"},
        ),
        settings=settings,
    )
    assert manifest["status"] == "completed"
    assert len(receiver_class.deliveries) == 1
    delivery = receiver_class.deliveries[0]
    assert delivery["payload"]["job_id"] == manifest["job_id"]
    assert delivery["payload"]["status"] == "completed"
    signature = {k.lower(): v for k, v in delivery["headers"].items()}.get("x-framefrog-signature", "")
    assert signature.startswith("sha256="), delivery["headers"]


def test_the_webhook_url_is_redacted_in_the_manifest(receiver, sample_video, tmp_path):
    url, _receiver = receiver
    settings = _settings(tmp_path)
    sample = _sample(settings, sample_video)
    manifest = run_job(
        JobRequest(
            input_uri=str(sample),
            stages=["scene_detect"],
            options={"webhook_url": f"{url}?token=secretvalue"},
        ),
        settings=settings,
    )
    assert "secretvalue" not in json.dumps(manifest.get("webhook") or {})
    assert manifest["webhook"]["delivered"] is True


def test_a_failing_webhook_does_not_fail_the_job(sample_video, tmp_path):
    """Delivery is best-effort: the job status must not depend on it."""
    settings = _settings(tmp_path)
    sample = _sample(settings, sample_video)
    manifest = run_job(
        JobRequest(
            input_uri=str(sample),
            stages=["scene_detect"],
            options={"webhook_url": "http://127.0.0.1:1/hook"},
        ),
        settings=settings,
    )
    assert manifest["status"] == "completed", manifest.get("error")
    assert manifest["webhook"]["delivered"] is False
    assert manifest["webhook"]["attempts"] >= 1
