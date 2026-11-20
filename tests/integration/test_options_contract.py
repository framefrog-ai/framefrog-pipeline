# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The API options contract: explicit stage switches and delivery settings."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.schemas import JobOptions, disabled_stages, to_stage_options
from app.core.config import Settings
from app.jobs.repository import SQLiteJobRepository
from app.main import create_app


def test_only_an_explicit_flag_disables_a_stage():
    # Default options: nothing is disabled.
    assert disabled_stages(JobOptions()) == []

    # An explicit `enabled: false` disables the stage.
    options = JobOptions(color_grade={"enabled": False})
    assert disabled_stages(options) == ["color_grade"]

    # An explicit `enabled: true` does not.
    options = JobOptions(color_grade={"enabled": True})
    assert disabled_stages(options) == []


def test_disabled_stages_are_recorded_in_the_stage_options():
    options = JobOptions(interpolate={"enabled": False})
    flat = to_stage_options(options)
    assert flat["_disabled_stages"] == ["interpolate"]


def test_delivery_options_reach_the_stage_options():
    flat = to_stage_options(JobOptions(deliver={"crf": 23, "preset": "slow"}))
    assert flat["master_crf"] == 23
    assert flat["master_preset"] == "slow"


def test_disabled_stage_is_skipped_not_run(tmp_path):
    """A stage switched off in the request must not execute."""
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    sample = work / "sample.mp4"
    subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / "generate_test_video.py"),
            "--output",
            str(sample),
            "--duration",
            "2",
            "--scenes",
            "2",
            "--width",
            "160",
            "--height",
            "90",
        ],
        check=True,
        capture_output=True,
    )

    settings = Settings(
        app_env="test",
        work_dir=work,
        database_url=f"sqlite:///{tmp_path / 'jobs.db'}",
        storage_backend="local",
        log_level="WARNING",
    )
    repository = SQLiteJobRepository(tmp_path / "jobs.db")
    app = create_app(settings=settings, repository=repository)
    with TestClient(app) as client:
        created = client.post(
            "/v1/jobs",
            json={
                "input_uri": str(sample),
                "stages": ["scene_detect", "color_grade", "deliver"],
                "options": {"color_grade": {"enabled": False}},
            },
        )
        assert created.status_code == 202
        job_id = created.json()["job_id"]

        import time

        deadline = time.time() + 120
        state = {}
        while time.time() < deadline:
            state = client.get(f"/v1/jobs/{job_id}").json()
            if state["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.2)

    assert state["status"] == "completed", state.get("error")
    stages = {entry["stage"]: entry for entry in state["stage_results"]}
    assert stages["color_grade"]["status"] == "skipped"
    assert stages["scene_detect"]["status"] == "completed"
    assert stages["deliver"]["status"] == "completed"


def test_delivery_preset_is_recorded(sample_video, tmp_path):
    from app.jobs.models import JobRequest
    from app.jobs.pipeline import run_job

    settings = Settings(
        app_env="test",
        work_dir=tmp_path / "work",
        database_url=f"sqlite:///{tmp_path / 'jobs.db'}",
        storage_backend="local",
        log_level="WARNING",
    )
    settings.work_dir_resolved.mkdir(parents=True, exist_ok=True)
    import shutil

    sample = settings.work_dir_resolved / "sample.mp4"
    shutil.copyfile(sample_video, sample)

    manifest = run_job(
        JobRequest(
            input_uri=str(sample),
            stages=["deliver"],
            options={"master_crf": 26, "master_preset": "veryfast"},
        ),
        settings=settings,
    )
    assert manifest["status"] == "completed", manifest.get("error")
    deliver = [entry for entry in manifest["stages"] if entry["stage"] == "deliver"][0]
    assert deliver["meta"]["preset"] == "veryfast"
    assert deliver["meta"]["crf"] == 26
