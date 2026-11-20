# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""FFmpeg wrapper behaviour: command building, timeouts, output checks."""

from __future__ import annotations

import pytest

from app.media import ffmpeg


def test_safe_command_redacts_secrets():
    command = ffmpeg.safe_command(["curl", "https://host/x?X-Amz-Signature=abc123&other=1"])
    assert "abc123" not in command
    assert "other=1" in command


def test_safe_command_truncates_long_arguments():
    command = ffmpeg.safe_command(["ffmpeg", "-i", "x" * 500])
    assert len(command) < 400


def test_run_raises_on_failure(tmp_path):
    with pytest.raises(ffmpeg.FFmpegError) as excinfo:
        ffmpeg.run(["ffmpeg", "-y", "-i", str(tmp_path / "missing.mp4"), str(tmp_path / "out.mp4")])
    assert excinfo.value.command


def test_run_to_file_detects_missing_output(tmp_path):
    with pytest.raises(ffmpeg.FFmpegError) as excinfo:
        ffmpeg.run_to_file(["true"], str(tmp_path / "never-created.mp4"))
    assert "not created" in str(excinfo.value)


def test_run_to_file_detects_empty_output(tmp_path):
    target = tmp_path / "empty.mp4"
    with pytest.raises(ffmpeg.FFmpegError) as excinfo:
        ffmpeg.run_to_file(["touch", str(target)], str(target))
    assert "empty" in str(excinfo.value)


def test_run_to_file_success(tmp_path):
    target = tmp_path / "out.txt"
    ffmpeg.run_to_file(["sh", "-c", f"printf hello > {target}"], str(target))
    assert target.read_text() == "hello"


def test_encoder_selection_rejects_unknown():
    with pytest.raises(ValueError):
        ffmpeg.choose_encoder({"encoder": "magic"})


def test_encoder_auto_is_never_nvenc_without_device():
    # No CUDA device is present in CI; auto must pick the CPU encoder.
    if ffmpeg.has_nvidia_device():
        pytest.skip("this host exposes an NVIDIA device")
    assert ffmpeg.choose_encoder({"encoder": "auto"}) == "libx264"


def test_video_encode_args_for_libx264():
    args, name = ffmpeg.video_encode_args({"encoder": "libx264"}, crf=20, preset="medium")
    assert name == "libx264"
    assert "-c:v" in args and "libx264" in args
    assert "yuv420p" in args


def test_nvenc_request_without_device_fails():
    if ffmpeg.has_nvidia_device():
        pytest.skip("this host exposes an NVIDIA device")
    with pytest.raises(RuntimeError):
        ffmpeg.choose_encoder({"encoder": "nvenc"})
