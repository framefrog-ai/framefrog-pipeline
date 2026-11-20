# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Integration: a real clip through real FFmpeg (skipped if FFmpeg is absent)."""

import json
import os
import subprocess

import pytest

from framefrog import ffmpeg
from framefrog.pipeline import run_job
from framefrog.types import JobRequest

pytestmark = pytest.mark.skipif(not ffmpeg.has_ffmpeg(), reason="ffmpeg not installed")


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    out = str(tmp_path_factory.mktemp("clip") / "src.mp4")
    subprocess.run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=2",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", out,
    ], check=True, capture_output=True)
    return out


def test_full_pipeline_without_interpolation(clip, tmp_path):
    manifest = run_job(
        JobRequest(
            input_uri=clip,
            stages=["audio_cleanup", "scene_detect", "color_grade", "deliver"],
            options={"lut": "cinematic_warm", "look_strength": 0.8},
            output_dir=str(tmp_path),
        )
    )
    assert manifest["status"] == "completed", manifest.get("error")
    names = [s["stage"] for s in manifest["stages"]]
    assert names == ["audio_cleanup", "scene_detect", "color_grade", "deliver"]
    master = manifest["artifacts"]["master"]
    assert os.path.exists(master) and os.path.getsize(master) > 0
    assert os.path.exists(manifest["artifacts"]["proxy"])
    assert os.path.exists(manifest["artifacts"]["poster"])
    saved = json.load(open(manifest["manifest_path"], encoding="utf-8"))
    assert saved["job_id"] == manifest["job_id"]
    assert saved["status"] == "completed"


def test_unknown_stage_is_rejected(clip):
    with pytest.raises(ValueError):
        run_job(JobRequest(input_uri=clip, stages=["audio_cleanup", "nonsense"]))


def test_audio_auto_falls_back_without_deepfilternet(clip, tmp_path, monkeypatch):
    import framefrog.stages.audio as audio_stage

    monkeypatch.setattr(audio_stage, "deepfilternet_available", lambda: False)
    manifest = run_job(
        JobRequest(
            input_uri=clip,
            stages=["audio_cleanup"],
            options={"denoise_model": "auto"},
            output_dir=str(tmp_path),
        )
    )
    assert manifest["status"] == "completed"
    meta = manifest["stages"][0]["meta"]
    assert meta["denoise_model"] == "ffmpeg"
    assert meta["fallback_reason"] == "deepfilternet not installed"


def test_audio_explicit_deepfilternet_fails_loudly(clip, tmp_path, monkeypatch):
    import framefrog.stages.audio as audio_stage

    monkeypatch.setattr(audio_stage, "deepfilternet_available", lambda: False)
    manifest = run_job(
        JobRequest(
            input_uri=clip,
            stages=["audio_cleanup"],
            options={"denoise_model": "deepfilternet"},
            output_dir=str(tmp_path),
        )
    )
    assert manifest["status"] == "failed"
    assert "deepfilternet" in manifest["error"]


# --------------------------------------------------------------------------- #
# RIFE — only runs where the graph and onnxruntime are actually present
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def short_clip(tmp_path_factory):
    out = str(tmp_path_factory.mktemp("short") / "short.mp4")
    subprocess.run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=160x96:rate=12:duration=0.5",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=0.5",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", out,
    ], check=True, capture_output=True)
    return out


@pytest.mark.skipif(
    not (ffmpeg.has_ffmpeg()), reason="ffmpeg not installed"
)
def test_rife_interpolation(short_clip, tmp_path):
    from framefrog.models import find_rife_model, onnxruntime_available

    if not (find_rife_model() and onnxruntime_available()):
        pytest.skip("RIFE graph or onnxruntime not available")

    manifest = run_job(
        JobRequest(
            input_uri=short_clip,
            stages=["interpolate"],
            options={"interp_model": "rife", "target_fps": 24, "rife_provider": "cpu"},
            output_dir=str(tmp_path),
        )
    )
    assert manifest["status"] == "completed", manifest.get("error")
    meta = manifest["stages"][0]["meta"]
    assert "RIFE" in meta["interp_model"]
    assert meta["provider"] == "CPUExecutionProvider"
    assert abs(meta["output_fps"] - 24) < 0.5
    assert os.path.exists(manifest["final_output"])


def test_webhook_secret_is_redacted_in_manifest():
    from framefrog.pipeline import _public_options

    clean = _public_options({"webhook_secret": "s3cret", "lut": "neutral_film"})
    assert clean == {"webhook_secret": "***", "lut": "neutral_film"}
