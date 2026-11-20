# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The command line entry point.

The CLI is operator tooling: it runs on the machine that holds the footage,
so it may read a local file from anywhere. The HTTP API is stricter — it only
reads from ``WORK_DIR`` and the configured roots.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _sample(tmp_path) -> Path:
    target = tmp_path / "sample.mp4"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "generate_test_video.py"),
            "--output",
            str(target),
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
    return target


def test_cli_runs_a_local_file_outside_the_repository(tmp_path):
    """`python main.py --input /somewhere/else.mp4` must work."""
    sample = _sample(tmp_path)
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "main.py"),
            "--input",
            str(sample),
            "--stages",
            "scene_detect,deliver",
        ],
        capture_output=True,
        text=True,
        timeout=600,
        cwd=str(ROOT),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "completed" in proc.stdout
    assert "scene_detect" in proc.stdout
    assert "deliver" in proc.stdout


def test_cli_reports_a_failure_with_a_non_zero_exit(tmp_path):
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"not a video")
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "main.py"),
            "--input",
            str(broken),
            "--stages",
            "scene_detect",
        ],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(ROOT),
    )
    assert proc.returncode == 1
    assert "failed" in proc.stdout
