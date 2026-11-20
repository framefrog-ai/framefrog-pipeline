# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""End-to-end pipeline integration: real FFmpeg, real output files."""

from __future__ import annotations

import json
import os

import pytest

from app.jobs.models import JobRequest
from app.jobs.pipeline import run_job
from app.media import validation
from app.media.ffprobe import probe


def _settings(tmp_path):
    from app.core.config import Settings

    work_dir = tmp_path / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        app_env="test",
        work_dir=work_dir,
        database_url=f"sqlite:///{tmp_path / 'jobs.db'}",
        storage_backend="local",
        log_level="WARNING",
    )


def _inside_workdir(settings, sample):
    """Copy the sample into ``WORK_DIR`` — the API only reads inputs from there."""
    import shutil

    target = settings.work_dir_resolved / sample.name
    shutil.copyfile(sample, target)
    return target


def test_scene_detection_finds_the_generated_cut(sample_video, tmp_path):
    from app.stages import scene_detect

    result = scene_detect.run(str(sample_video), str(tmp_path), {"scene_threshold": 0.27, "min_scene_s": 0.5})
    assert result.meta["scene_count"] >= 2
    scenes = result.meta["scenes"]
    assert scenes[0]["start_s"] == 0.0
    assert scenes[-1]["end_s"] == pytest.approx(6.0, abs=0.5)
    for scene in scenes:
        assert scene["end_s"] > scene["start_s"]
        assert "start_frame" in scene and "end_frame" in scene
        assert "start_timecode" in scene and "end_timecode" in scene
    assert os.path.exists(result.meta["scenes_path"])
    with open(result.meta["scenes_path"], encoding="utf-8") as handle:
        assert isinstance(json.load(handle), list)


def test_color_grade_produces_a_real_file(sample_video, tmp_path, monkeypatch):
    from app.media.lut import write_lut
    from app.stages import color_grade

    lut = write_lut("cinematic_warm", str(tmp_path / "look.cube"), size=9)
    result = color_grade.run(str(sample_video), str(tmp_path), {"lut": lut})
    assert os.path.exists(result.output_uri)
    assert os.path.getsize(result.output_uri) > 0
    graded = probe(result.output_uri)
    assert graded.has_video
    assert graded.duration_s == pytest.approx(6.0, abs=0.5)
    assert result.meta["applied"] is True
    assert len(result.meta["lut_fingerprint"]["sha256"]) == 64


def test_deliver_creates_a_valid_master(sample_video, tmp_path):
    from app.stages import deliver

    result = deliver.run(str(sample_video), str(tmp_path), {"master_crf": 26})
    master = result.meta["master"]
    assert os.path.exists(master)
    info = probe(master)
    assert info.has_video and info.has_audio
    assert info.video_codec == "h264"
    assert info.audio_codec == "aac"
    assert info.pix_fmt == "yuv420p"
    assert info.duration_s == pytest.approx(6.0, abs=0.5)
    assert os.path.exists(result.meta["proxy"])
    assert os.path.exists(result.meta["poster"])


def test_full_pipeline_produces_outputs(sample_video, tmp_path):
    settings = _settings(tmp_path)
    manifest = run_job(
        JobRequest(
            input_uri=str(_inside_workdir(settings, sample_video)),
            stages=["audio_cleanup", "scene_detect", "color_grade", "deliver"],
            options={"lut": "cinematic_warm", "master_crf": 28},
        ),
        settings=settings,
    )
    assert manifest["status"] == "completed", manifest.get("error")
    stages = {entry["stage"]: entry for entry in manifest["stages"]}
    assert stages["audio_cleanup"]["status"] == "completed"
    assert stages["scene_detect"]["status"] == "completed"
    assert stages["color_grade"]["status"] == "completed"
    assert stages["deliver"]["status"] == "completed"
    assert manifest["outputs"]
    kinds = {output["kind"] for output in manifest["outputs"]}
    assert {"master", "proxy", "poster"} <= kinds
    master = next(o for o in manifest["outputs"] if o["kind"] == "master")
    assert master["sha256"]
    assert master["width"] == 320
    assert os.path.exists(master["path"])


def test_pipeline_records_input_metadata(sample_video, tmp_path):
    settings = _settings(tmp_path)
    manifest = run_job(
        JobRequest(input_uri=str(_inside_workdir(settings, sample_video)), stages=["scene_detect"]),
        settings=settings,
    )
    assert manifest["status"] == "completed"
    assert manifest["input"]["width"] == 320
    assert manifest["input"]["height"] == 180
    assert manifest["input"]["fps"] == pytest.approx(30.0, abs=0.1)
    assert manifest["input"]["has_audio"] is True
    assert manifest["gpu_validation_status"] == "pending"


def test_pipeline_rejects_an_invalid_input(tmp_path):
    settings = _settings(tmp_path)
    settings.work_dir.mkdir(parents=True, exist_ok=True)
    broken = settings.work_dir / "broken.mp4"
    broken.write_bytes(b"this is not a video")
    manifest = run_job(
        JobRequest(input_uri=str(broken), stages=["scene_detect"]),
        settings=settings,
    )
    assert manifest["status"] == "failed"
    assert manifest["error"]["code"] == "media_validation_error"


def test_media_validation_rejects_oversized_files(sample_video, tmp_path):
    from app.core.errors import MediaValidationError

    settings = _settings(tmp_path)
    settings.max_upload_bytes = 10
    with pytest.raises(MediaValidationError):
        validation.validate_media_file(sample_video, settings)


def test_media_validation_rejects_long_videos(sample_video, tmp_path):
    from app.core.errors import MediaValidationError

    settings = _settings(tmp_path)
    settings.max_video_duration_seconds = 1.0
    with pytest.raises(MediaValidationError):
        validation.validate_media_file(sample_video, settings)


def test_media_validation_rejects_high_resolution(sample_video, tmp_path):
    from app.core.errors import MediaValidationError

    settings = _settings(tmp_path)
    settings.max_video_width = 100
    with pytest.raises(MediaValidationError):
        validation.validate_media_file(sample_video, settings)


def test_media_validation_rejects_empty_files(tmp_path):
    from app.core.errors import MediaValidationError

    settings = _settings(tmp_path)
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    with pytest.raises(MediaValidationError):
        validation.validate_media_file(empty, settings)
