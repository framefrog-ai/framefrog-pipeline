# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Model resolution and capability reporting — no runtimes required."""

import os

from framefrog.models import (
    find_rife_model,
    model_report,
    onnxruntime_available,
    rife_available,
)
from framefrog.stages.interpolate import _provider


def test_explicit_rife_path_wins(tmp_path):
    fake = tmp_path / "my-rife.onnx"
    fake.write_bytes(b"not really a graph")
    assert find_rife_model(str(fake)) == str(fake)


def test_env_model_dir(tmp_path, monkeypatch):
    graph = tmp_path / "rife-test.onnx"
    graph.write_bytes(b"x")
    monkeypatch.setenv("FRAMEFROG_MODEL_DIR", str(tmp_path))
    monkeypatch.delenv("FRAMEFROG_RIFE_ONNX", raising=False)
    assert find_rife_model() == str(graph)


def test_no_model_available(monkeypatch, tmp_path):
    import framefrog.models as models

    monkeypatch.setattr(models, "_model_dirs", lambda: [str(tmp_path)])
    monkeypatch.setenv("FRAMEFROG_RIFE_ONNX", str(tmp_path / "missing.onnx"))
    assert find_rife_model() is None
    assert rife_available() is False


def test_report_shape():
    report = model_report()
    assert set(report) == {"deepfilternet", "rife", "sam2", "upscale"}
    assert "available" in report["deepfilternet"]
    assert "onnxruntime" in report["rife"]


def test_providers():
    assert _provider("cpu") == ["CPUExecutionProvider"]
    assert _provider("cuda")[0] == "CUDAExecutionProvider"
    assert _provider("auto")[-1] == "CPUExecutionProvider"
