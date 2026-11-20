# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""4K upscaling stage — FFmpeg path on CPU; GPU backends fail clearly."""

import subprocess

import pytest

from framefrog import ffmpeg
from framefrog.stages import upscale

pytestmark = pytest.mark.skipif(not ffmpeg.has_ffmpeg(), reason="ffmpeg not installed")


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    out = str(tmp_path_factory.mktemp("up") / "src.mp4")
    subprocess.run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=160x96:rate=12:duration=1",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", out,
    ], check=True, capture_output=True)
    return out


def test_lanczos_backend_doubles_height(clip, tmp_path):
    result = upscale.run(
        clip,
        str(tmp_path),
        {"target_height": 108, "upscale_backend": "ffmpeg", "encoder": "libx264"},
    )
    assert result.meta["backend"] == "ffmpeg"
    assert result.meta["learned"] is False
    assert result.meta["encoder"] == "libx264"
    info = ffmpeg.probe(result.output_uri)
    assert info.height == 108
    assert info.width == 180
    assert info.has_audio


def test_skips_when_source_is_already_tall_enough(clip, tmp_path):
    result = upscale.run(clip, str(tmp_path), {"target_height": 96})
    assert result.meta["skipped"] is True
    assert ffmpeg.probe(result.output_uri).height == 96


def test_tensorrt_requires_an_engine(clip, tmp_path):
    with pytest.raises(RuntimeError, match="upscale_engine"):
        upscale.run(
            clip,
            str(tmp_path),
            {"target_height": 108, "upscale_backend": "tensorrt"},
        )


def test_realesrgan_requires_weights(clip, tmp_path):
    with pytest.raises(RuntimeError, match="upscale_model"):
        upscale.run(
            clip,
            str(tmp_path),
            {"target_height": 108, "upscale_backend": "realesrgan"},
        )


def test_unknown_backend_is_rejected(clip, tmp_path):
    with pytest.raises(ValueError):
        upscale.run(
            clip, str(tmp_path), {"target_height": 108, "upscale_backend": "waifu"}
        )
