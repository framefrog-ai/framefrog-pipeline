# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""GPU-marked tests.

These are skipped automatically when no CUDA device is present — a skipped
test is not a passed test, and the repository never stores the result of a
GPU run that did not happen.

Run them on a GPU host with:

    pytest -m gpu -v
"""

from __future__ import annotations

import os

import pytest

from app.core import capabilities

pytestmark = pytest.mark.gpu


def test_cuda_device_is_present():
    assert capabilities.cuda_available(), "this test must run on a CUDA host"


def test_torch_reports_the_device():
    info = capabilities.torch_capability()
    assert info["cuda_available"] is True
    assert info["gpu_name"]
    assert info["total_vram_mb"]


def test_rife_runs_on_cuda(tmp_path, monkeypatch):
    """Interpolate a clip with the CUDA provider and verify the frame rate."""
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    clip = tmp_path / "clip.mp4"
    subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / "generate_test_video.py"),
            "--output",
            str(clip),
            "--duration",
            "2",
            "--scenes",
            "1",
            "--width",
            "320",
            "--height",
            "180",
        ],
        check=True,
        capture_output=True,
    )
    from app.stages import interpolate

    result = interpolate.run(
        str(clip), str(tmp_path), {"interp_model": "auto", "rife_provider": "cuda", "target_fps": 48}
    )
    assert os.path.exists(result.output_uri)
    assert result.meta.get("provider") == "CUDAExecutionProvider"
    assert result.meta["output_fps"] == pytest.approx(48.0, abs=1.0)


def test_gpu_smoke_test_script_runs(tmp_path):
    """The GPU smoke test must pass on a GPU host."""
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    proc = subprocess.run(
        [sys.executable, str(root / "scripts" / "gpu_smoke_test.py"), "--json"],
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
