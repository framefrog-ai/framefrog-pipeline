# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The GPU smoke-test CLI runs on a CPU-only host without failing."""

import json

from app import gpu_check


def test_probe_runs_and_skips_unrequested_models(capsys):
    rc = gpu_check.main(["--json"])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["checks"]["encode"]["status"] in {"ok", "skipped"}
    assert report["checks"]["tensorrt"]["status"] == "skipped"
    assert report["checks"]["sam2"]["status"] == "skipped"
    assert report["summary"]["ok"] is True


def test_a_failed_check_fails_the_command(monkeypatch):
    monkeypatch.setattr(gpu_check, "_check_encode", lambda encoder: {"status": "failed", "error": "boom"})
    assert gpu_check.main(["--json"]) == 1


def test_environment_report_has_expected_keys():
    report = gpu_check._probe_environment()
    for key in ("ffmpeg", "nvenc_compiled", "nvidia_device", "torch", "tensorrt", "sam2"):
        assert key in report
