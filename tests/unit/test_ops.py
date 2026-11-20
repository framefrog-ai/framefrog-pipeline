# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Signed links, webhooks and cost accounting — pure stdlib, no FFmpeg."""

import time

import app.core.ops as ops


def test_sign_verify_roundtrip():
    expires = int(time.time()) + 60
    token = ops.sign_download("fj_1", "master", expires, "s3cret")
    assert ops.verify_download("fj_1", "master", expires, token, "s3cret") == (True, "ok")


def test_wrong_artifact_is_rejected():
    expires = int(time.time()) + 60
    token = ops.sign_download("fj_1", "master", expires, "s3cret")
    ok, reason = ops.verify_download("fj_1", "proxy", expires, token, "s3cret")
    assert not ok
    assert reason == "bad signature"


def test_expired_token_is_rejected():
    expires = int(time.time()) - 5
    token = ops.sign_download("fj_1", "master", expires, "s3cret")
    ok, reason = ops.verify_download("fj_1", "master", expires, token, "s3cret")
    assert not ok
    assert reason == "token expired"


def test_missing_secret_is_rejected():
    ok, reason = ops.verify_download("fj_1", "master", 0, "x", "")
    assert (ok, reason) == (False, "signing secret not configured")


def test_download_url_uses_env(monkeypatch):
    monkeypatch.setenv(ops.ENV_SIGNING_SECRET, "k")
    url = ops.download_url("http://h:8000/", "fj_1", ttl=30)
    assert url.startswith("http://h:8000/v1/jobs/fj_1/download?")
    assert "token=" in url and "expires=" in url


def test_download_url_disabled(monkeypatch):
    monkeypatch.delenv(ops.ENV_SIGNING_SECRET, raising=False)
    assert ops.download_url("http://h", "fj_1") is None


def test_webhook_is_signed_and_redacted(monkeypatch):
    sent = {}

    def fake_post(url, body, headers, timeout):
        sent["url"] = url
        sent["body"] = body
        sent["headers"] = headers
        return 204

    monkeypatch.setattr(ops, "_post_json", fake_post)
    manifest = {"job_id": "fj_1", "status": "completed", "manifest_path": "/secret"}
    record = ops.notify_manifest(manifest, "https://hook.example/x?token=abc", secret="k", attempts=1)
    assert record["delivered"] is True
    assert record["attempts"] == 1
    assert record["url"] == "https://hook.example/x"
    assert sent["headers"]["x-framefrog-signature"].startswith("sha256=")
    assert "manifest_path" not in sent["body"].decode()


def test_webhook_failure_is_recorded(monkeypatch):
    def boom(url, body, headers, timeout):
        raise OSError("down")

    monkeypatch.setattr(ops, "_post_json", boom)
    monkeypatch.setattr(ops.time, "sleep", lambda _s: None)
    record = ops.notify_manifest({"job_id": "fj_1"}, "https://hook", attempts=2)
    assert record["delivered"] is False
    assert record["attempts"] == 2
    assert "OSError" in record["error"]


def test_costs_follow_the_backend_actually_used():
    stages = [
        {
            "stage": "audio_cleanup",
            "status": "completed",
            "duration_s": 36.0,
            "meta": {"denoise_model": "deepfilternet"},
        },
        {"stage": "scene_detect", "status": "completed", "duration_s": 18.0, "meta": {}},
        {
            "stage": "interpolate",
            "status": "completed",
            "duration_s": 36.0,
            "meta": {"interp_model": "RIFE v4.9 (ONNX Runtime)"},
        },
    ]
    cost = ops.compute_costs(stages, gpu_hourly_usd=3.0, cpu_hourly_usd=0.3)
    assert cost["gpu_seconds"] == 72.0
    assert cost["cpu_seconds"] == 18.0
    expected = 72 / 3600 * 3.0 + 18 / 3600 * 0.3
    assert abs(cost["total_usd"] - expected) < 1e-9


def test_costs_default_to_zero_rates(monkeypatch):
    monkeypatch.delenv(ops.ENV_GPU_RATE, raising=False)
    monkeypatch.delenv(ops.ENV_CPU_RATE, raising=False)
    cost = ops.compute_costs([{"stage": "deliver", "duration_s": 10.0}])
    assert cost["total_usd"] == 0.0
    assert cost["cpu_seconds"] == 10.0
