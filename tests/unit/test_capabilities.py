# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Capability-aware behaviour of the model-backed stages."""

from __future__ import annotations

import pytest

from app.core import capabilities
from app.core.errors import CapabilityError


def test_deepfilternet_capability_is_honest():
    info = capabilities.deepfilternet_capability()
    assert "available" in info
    if not info["available"]:
        assert info["reason"]


def test_onnxruntime_capability_is_honest():
    info = capabilities.onnxruntime_capability()
    assert "available" in info
    if info["available"]:
        assert isinstance(info["providers"], list)


def test_torch_capability_never_invents_a_gpu():
    info = capabilities.torch_capability()
    if not info.get("cuda_available"):
        assert info.get("gpu_name") is None
        assert info.get("total_vram_mb") is None


def test_gpu_metric_fields_are_absent_before_validation():
    fields = capabilities.gpu_metric_fields("pending")
    assert fields["gpu_validation_status"] == "pending"
    for key in ("gpu_name", "gpu_seconds", "peak_vram_mb", "cuda_version", "inference_seconds"):
        assert key not in fields


def test_cuda_available_matches_torch():
    assert capabilities.cuda_available() == bool(capabilities.torch_capability()["cuda_available"])


def test_rife_missing_model_raises_capability_error(tmp_path, monkeypatch):
    import app.models.discovery as discovery
    from app.stages import interpolate

    monkeypatch.setattr(discovery, "find_rife_model", lambda explicit=None: None)
    monkeypatch.setattr(interpolate, "find_rife_model", lambda explicit=None: None)
    monkeypatch.setattr(interpolate.ffmpeg, "probe", lambda *a, **k: _fake_info())
    with pytest.raises(CapabilityError) as excinfo:
        interpolate.run(str(tmp_path / "in.mp4"), str(tmp_path), {"interp_model": "rife"})
    assert "download_models" in str(excinfo.value) or "onnxruntime" in str(excinfo.value)


def test_rife_explicit_cuda_without_gpu_raises(tmp_path, monkeypatch):
    import app.stages.interpolate as interpolate

    monkeypatch.setattr(capabilities, "cuda_available", lambda: False)
    monkeypatch.setattr(interpolate.ffmpeg, "probe", lambda *a, **k: _fake_info())
    with pytest.raises(CapabilityError) as excinfo:
        interpolate.run(str(tmp_path / "in.mp4"), str(tmp_path), {"rife_provider": "cuda"})
    assert "CUDA" in str(excinfo.value)


def test_target_fps_above_limit_is_rejected(tmp_path):
    from app.stages import interpolate

    with pytest.raises(CapabilityError):
        interpolate.run(str(tmp_path / "in.mp4"), str(tmp_path), {"target_fps": 120})


def test_audio_cleanup_reports_missing_model(tmp_path, monkeypatch):
    """A missing DeepFilterNet must be reported, never silently "cleaned"."""
    import app.stages.audio_cleanup as audio_cleanup

    monkeypatch.setattr(audio_cleanup, "deepfilternet_available", lambda: False)
    monkeypatch.setattr(audio_cleanup.ffmpeg, "probe", lambda *a, **k: _fake_info())
    with pytest.raises(RuntimeError) as excinfo:
        audio_cleanup.run("in.mp4", str(tmp_path), {"denoise_model": "deepfilternet"})
    assert "deepfilternet" in str(excinfo.value)


def _fake_info():
    from app.media.ffprobe import MediaInfo

    return MediaInfo(
        path="in.mp4",
        has_video=True,
        has_audio=True,
        audio_codec="aac",
        fps=30.0,
        duration_s=2.0,
        width=320,
        height=180,
    )
