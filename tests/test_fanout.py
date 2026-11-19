"""Shot planning, task isolation and the queue-backed fan-out path."""

import os
import subprocess

import pytest

from framefrog import fanout, ffmpeg
from framefrog.fanout import build_tasks, run_task, shots_from_scenes


def test_shots_are_normalised_and_clamped():
    scenes = [
        {"start_s": 0.0, "end_s": 1.0},
        {"start_s": 1.0, "end_s": 2.0},
        {"start_s": 1.5, "end_s": 99.0},  # overlapping/overlong tail
    ]
    shots = shots_from_scenes(scenes, duration=3.0)
    assert shots[0]["start_s"] == 0.0
    assert [s["end_s"] for s in shots] == [1.0, 2.0, 3.0]
    assert shots[-1]["end_s"] == 3.0


def test_single_scene_is_one_shot():
    assert len(shots_from_scenes([{"start_s": 0.0, "end_s": 5.0}], 5.0)) == 1


def test_build_tasks_strips_orchestration_keys(tmp_path):
    shots = [{"index": 0, "start_s": 0.0, "end_s": 1.0, "path": "a.mp4"}]
    tasks = build_tasks(
        "color_grade",
        shots,
        str(tmp_path),
        {"lut": "neutral_film", "fanout": "auto", "queue_url": "file:///q", "fanout_workers": 2},
    )
    assert len(tasks) == 1
    assert tasks[0]["options"] == {"lut": "neutral_film"}
    assert tasks[0]["task_id"] == "color_grade_000"


def test_run_task_records_failure(tmp_path):
    result = run_task({
        "task_id": "bad_000",
        "stage": "does_not_exist",
        "input": "missing.mp4",
        "workdir": str(tmp_path / "w"),
        "shot_index": 0,
        "options": {},
    })
    assert result["status"] == "failed"
    assert "KeyError" in result["error"]


def _two_shot_clip(tmp_path):
    out = str(tmp_path / "two_shots.mp4")
    subprocess.run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=160x96:rate=12:duration=2",
        "-f", "lavfi", "-i", "smptebars=size=160x96:rate=12:duration=2",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=4",
        "-filter_complex",
        "[0:v][1:v]concat=n=2:v=1:a=0[v];[2:a]volume=0.3[a]",
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-shortest", out,
    ], check=True, capture_output=True)
    return out


@pytest.mark.skipif(not ffmpeg.has_ffmpeg(), reason="ffmpeg not installed")
def test_queue_fanout_grades_each_shot_and_stitches(tmp_path):
    clip = _two_shot_clip(tmp_path)
    scenes = [{"start_s": 0.0, "end_s": 2.0}, {"start_s": 2.0, "end_s": 4.0}]

    result = fanout.fanout_stage(
        "color_grade",
        clip,
        str(tmp_path / "job"),
        scenes,
        {
            "fanout": "shots",
            "fanout_mode": "queue",
            "fanout_workers": 2,
            "lut": "neutral_film",
            "look_strength": 0.6,
        },
    )

    assert result is not None
    assert result.meta["fanout"] is True
    assert result.meta["fanout_backend"] == "file-queue"
    assert result.meta["shots"] == 2
    assert os.path.exists(result.output_uri)

    out = ffmpeg.probe(result.output_uri)
    assert out.has_video and out.has_audio
    assert abs(out.duration_s - 4.0) < 0.6
    assert result.meta["shots"] == 2
