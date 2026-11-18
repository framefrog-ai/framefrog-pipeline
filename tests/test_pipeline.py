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
