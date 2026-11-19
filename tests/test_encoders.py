"""Encoder selection — NVENC only when FFmpeg actually advertises it."""

import pytest

from framefrog import ffmpeg

pytestmark = pytest.mark.skipif(not ffmpeg.has_ffmpeg(), reason="ffmpeg not installed")


def test_libx264_args():
    args, name = ffmpeg.video_encode_args({"encoder": "libx264"}, crf=20, preset="fast")
    assert name == "libx264"
    assert args[:2] == ["-c:v", "libx264"]
    assert "20" in args


def test_auto_never_picks_a_missing_gpu_encoder():
    name = ffmpeg.choose_encoder({"encoder": "auto"})
    usable = ffmpeg.has_encoder("h264_nvenc") and ffmpeg.has_nvidia_device()
    if usable:
        assert name == "h264_nvenc"
    else:
        assert name == "libx264"


def test_unknown_encoder_is_rejected():
    with pytest.raises(ValueError):
        ffmpeg.choose_encoder({"encoder": "av1_magic"})


def test_missing_nvenc_fails_with_instructions():
    if ffmpeg.has_encoder("h264_nvenc") and ffmpeg.has_nvidia_device():
        pytest.skip("this host has a usable NVENC encoder")
    with pytest.raises(RuntimeError, match="NVENC"):
        ffmpeg.choose_encoder({"encoder": "nvenc"})


def test_hevc_nvenc_request_is_validated():
    usable = ffmpeg.has_encoder("hevc_nvenc") and ffmpeg.has_nvidia_device()
    if not usable:
        with pytest.raises(RuntimeError):
            ffmpeg.choose_encoder({"encoder": "hevc_nvenc"})
    else:
        assert ffmpeg.choose_encoder({"encoder": "hevc_nvenc"}) == "hevc_nvenc"
