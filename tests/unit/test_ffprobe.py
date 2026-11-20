# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""FFprobe parsing: rational frame rates and real file inspection."""

from __future__ import annotations

import pytest

from app.media.ffprobe import FFprobeError, parse_rational, probe


@pytest.mark.parametrize(
    "value,expected",
    [
        ("30000/1001", 29.97002997),
        ("25/1", 25.0),
        ("24", 24.0),
        ("0/0", 0.0),
        ("N/A", 0.0),
        ("", 0.0),
        (None, 0.0),
        ("not/a/number", 0.0),
    ],
)
def test_parse_rational(value, expected):
    assert parse_rational(value) == pytest.approx(expected, rel=1e-6, abs=1e-6)


def test_probe_rejects_missing_file(tmp_path):
    with pytest.raises(FFprobeError):
        probe(str(tmp_path / "nope.mp4"))


def test_probe_reads_real_metadata(sample_video):
    info = probe(str(sample_video))
    assert info.has_video is True
    assert info.width == 320
    assert info.height == 180
    assert info.fps == pytest.approx(30.0, abs=0.1)
    assert info.duration_s == pytest.approx(6.0, abs=0.5)
    assert info.video_codec == "h264"
    assert info.has_audio is True
    assert info.audio_codec == "aac"
    assert info.audio_sample_rate == 48000
    assert info.size_bytes > 0
    assert info.container
    assert info.pix_fmt


def test_media_info_to_dict_is_json_safe(sample_video):
    import json

    info = probe(str(sample_video))
    json.dumps(info.to_dict())
