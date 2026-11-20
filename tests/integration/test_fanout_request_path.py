# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Per-shot fan-out in the request path.

The grade and interpolation stages can run shot by shot (so a heavy stage is
parallelised over the shots of a cut) and the shots are stitched back
together. This test proves the request path actually uses it — the fan-out
module exists and is unit-tested, but it must also be reachable from a job.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.jobs.repository import SQLiteJobRepository
from app.main import create_app


def _sample(tmp_path) -> Path:
    root = Path(__file__).resolve().parents[2]
    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    target = work / "sample.mp4"
    subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / "generate_test_video.py"),
            "--output",
            str(target),
            "--duration",
            "4",
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
    return target


@pytest.fixture()
def client(tmp_path):
    settings = Settings(
        app_env="test",
        work_dir=tmp_path / "work",
        database_url=f"sqlite:///{tmp_path / 'jobs.db'}",
        storage_backend="local",
        log_level="WARNING",
    )
    repository = SQLiteJobRepository(tmp_path / "jobs.db")
    app = create_app(settings=settings, repository=repository)
    with TestClient(app) as test_client:
        yield test_client


def _run(client: TestClient, sample: Path, options: dict) -> dict:
    created = client.post(
        "/v1/jobs",
        json={
            "input_uri": str(sample),
            "stages": ["scene_detect", "color_grade", "deliver"],
            "options": options,
        },
    )
    assert created.status_code == 202, created.text
    job_id = created.json()["job_id"]
    deadline = time.time() + 300
    state: dict = {}
    while time.time() < deadline:
        state = client.get(f"/v1/jobs/{job_id}").json()
        if state["status"] in {"completed", "failed", "cancelled"}:
            break
        time.sleep(0.2)
    return state


def test_fanout_runs_each_shot_separately(client, tmp_path):
    sample = _sample(tmp_path)
    state = _run(client, sample, {"fanout": {"mode": "shots", "workers": 2}})
    assert state["status"] == "completed", state.get("error")
    grade = next(s for s in state["stage_results"] if s["stage"] == "color_grade")
    assert grade["status"] == "completed"
    meta = grade["meta"]
    assert meta["fanout"] is True
    assert meta["shots"] == 2, meta
    assert meta["fanout_backend"] == "inline"
    assert meta["cuts_s"], "the shot boundaries must be recorded"


def test_fanout_off_is_the_default(client, tmp_path):
    sample = _sample(tmp_path)
    state = _run(client, sample, {})
    assert state["status"] == "completed", state.get("error")
    grade = next(s for s in state["stage_results"] if s["stage"] == "color_grade")
    assert "fanout" not in grade["meta"]


def test_fanout_auto_falls_back_and_records_why(client, tmp_path, monkeypatch):
    """A failing fan-out must degrade to the monolithic stage and say so."""
    from app.jobs import fanout

    sample = _sample(tmp_path)

    def broken(*args, **kwargs):
        raise RuntimeError("shot worker exploded")

    monkeypatch.setattr(fanout, "fanout_stage", broken)
    state = _run(client, sample, {"fanout": {"mode": "auto"}})
    assert state["status"] == "completed", state.get("error")
    grade = next(s for s in state["stage_results"] if s["stage"] == "color_grade")
    assert grade["status"] == "completed"
    assert "shot worker exploded" in grade["meta"]["fanout_fallback_reason"]


def test_fanout_shots_fails_the_job_when_a_shot_fails(client, tmp_path, monkeypatch):
    """With mode=shots a broken fan-out is an error, not a silent downgrade."""
    from app.jobs import fanout

    sample = _sample(tmp_path)

    def broken(*args, **kwargs):
        raise RuntimeError("shot worker exploded")

    monkeypatch.setattr(fanout, "fanout_stage", broken)
    state = _run(client, sample, {"fanout": {"mode": "shots"}})
    assert state["status"] == "failed"
    assert "shot worker exploded" in (state["error"] or {}).get("message", "")
