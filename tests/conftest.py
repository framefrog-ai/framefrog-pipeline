# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared fixtures.

The sample media is generated locally with FFmpeg's own test sources — no
downloads, no copyrighted footage.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _ffmpeg_available() -> bool:
    from app.media import ffmpeg

    return ffmpeg.has_ffmpeg()


requires_ffmpeg = pytest.mark.skipif(
    not _ffmpeg_available(), reason="FFmpeg/FFprobe are required for this test"
)


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Repository root."""
    return ROOT


@pytest.fixture(scope="session")
def sample_video(tmp_path_factory) -> Path:
    """A 6-second, two-scene synthetic clip with a sine tone."""
    if not _ffmpeg_available():
        pytest.skip("FFmpeg is required to generate the sample clip")
    target = tmp_path_factory.mktemp("media") / "sample.mp4"
    script = ROOT / "scripts" / "generate_test_video.py"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--output",
            str(target),
            "--duration",
            "6",
            "--scenes",
            "2",
            "--fps",
            "30",
            "--width",
            "320",
            "--height",
            "180",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"could not generate the sample clip: {result.stderr[-300:]}")
    return target


@pytest.fixture(scope="session")
def settings(tmp_path_factory):
    """Settings pointing at a throw-away work directory."""
    from app.core.config import Settings

    work_dir = tmp_path_factory.mktemp("work")
    return Settings(
        app_env="test",
        work_dir=work_dir,
        database_url=f"sqlite:///{work_dir / 'jobs.db'}",
        storage_backend="local",
        log_level="WARNING",
    )


@pytest.fixture()
def repo(tmp_path):
    """A fresh SQLite job repository."""
    from app.jobs.repository import SQLiteJobRepository

    repository = SQLiteJobRepository(tmp_path / "jobs.db")
    try:
        yield repository
    finally:
        repository.close()


@pytest.fixture()
def gpu_available() -> bool:
    """Whether a CUDA GPU is present (used to skip GPU-marked tests)."""
    from app.core import capabilities

    return bool(capabilities.cuda_available())


def pytest_collection_modifyitems(config, items):
    """Skip GPU-marked tests when no CUDA device is present."""
    from app.core import capabilities

    if capabilities.cuda_available():
        return
    skip_gpu = pytest.mark.skip(reason="CUDA GPU unavailable — GPU validation not run")
    for item in items:
        if "gpu" in item.keywords:
            item.add_marker(skip_gpu)


os.environ.setdefault("FRAMEFROG_DISABLE_NVENC", "1")
