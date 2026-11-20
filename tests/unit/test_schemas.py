# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Request schema validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.schemas import JobCreateRequest, JobOptions, to_stage_options


def test_minimal_request_is_valid():
    request = JobCreateRequest(input_uri="s3://bucket/x.mp4")
    assert request.options.interpolate.target_fps == 60
    assert request.stages is None


def test_unknown_stage_rejected():
    with pytest.raises(ValidationError) as excinfo:
        JobCreateRequest(input_uri="s3://bucket/x.mp4", stages=["not_a_stage"])
    assert "unknown stage" in str(excinfo.value)


def test_duplicate_stage_rejected():
    with pytest.raises(ValidationError):
        JobCreateRequest(input_uri="s3://bucket/x.mp4", stages=["deliver", "deliver"])


def test_empty_stage_list_rejected():
    with pytest.raises(ValidationError):
        JobCreateRequest(input_uri="s3://bucket/x.mp4", stages=[])


def test_fps_above_limit_rejected():
    with pytest.raises(ValidationError):
        JobOptions(interpolate={"target_fps": 120})


def test_negative_lufs_range_rejected():
    with pytest.raises(ValidationError):
        JobOptions(audio_cleanup={"target_lufs": -80})


def test_partial_intensity_rejected():
    with pytest.raises(ValidationError) as excinfo:
        JobOptions(color_grade={"enabled": True, "intensity": 0.5})
    assert "intensity" in str(excinfo.value)


def test_lut_extension_enforced():
    with pytest.raises(ValidationError):
        JobOptions(color_grade={"enabled": True, "lut_uri": "look.png"})


def test_lut_traversal_rejected():
    with pytest.raises(ValidationError):
        JobOptions(color_grade={"enabled": True, "lut_uri": "../../etc/passwd.cube"})


def test_unknown_option_rejected():
    with pytest.raises(ValidationError):
        JobOptions(scene_detect={"nope": 1})


def test_deliver_preset_must_be_known():
    with pytest.raises(ValidationError):
        JobOptions(deliver={"preset": "turbo"})


def test_crf_range_enforced():
    with pytest.raises(ValidationError):
        JobOptions(deliver={"crf": 99})


def test_idempotency_key_charset():
    with pytest.raises(ValidationError):
        JobCreateRequest(input_uri="s3://b/x.mp4", idempotency_key="bad key/with chars")


def test_to_stage_options_maps_fields():
    options = JobOptions(
        scene_detect={"threshold": 30.0, "minimum_scene_length_seconds": 1.0},
        audio_cleanup={"target_lufs": -14.0},
        interpolate={"target_fps": 30},
        deliver={"crf": 18},
    )
    flat = to_stage_options(options)
    assert flat["scene_threshold"] == pytest.approx(0.30)
    assert flat["min_scene_s"] == 1.0
    assert flat["lufs"] == -14.0
    assert flat["target_fps"] == 30
    assert flat["master_crf"] == 18


def test_to_stage_options_sets_lut_when_enabled():
    flat = to_stage_options(JobOptions(color_grade={"enabled": True}))
    assert flat["lut"] == "cinematic_warm"
