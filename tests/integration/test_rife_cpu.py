# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""RIFE interpolation on the CPU.

The RIFE graph is not committed to the repository (21 MB). When it is present
on the machine — ``python scripts/download_models.py`` — this test runs the
real model on the CPU and checks the produced frame rate.

The CUDA path is *not* covered here: it is exercised by the GPU-marked tests
in ``tests/integration/test_gpu.py``, which are skipped without a GPU.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from app.models.discovery import find_rife_model, onnxruntime_available

MODEL = find_rife_model()

pytestmark = pytest.mark.skipif(
    MODEL is None or not onnxruntime_available(),
    reason="the RIFE ONNX graph is not available (python scripts/download_models.py)",
)


def _clip(tmp_path: Path) -> Path:
    root = Path(__file__).resolve().parents[2]
    target = tmp_path / "clip.mp4"
    subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / "generate_test_video.py"),
            "--output",
            str(target),
            "--duration",
            "1",
            "--scenes",
            "1",
            "--fps",
            "12",
            "--width",
            "160",
            "--height",
            "96",
        ],
        check=True,
        capture_output=True,
    )
    return target


def test_rife_cpu_interpolation_produces_the_target_frame_rate(tmp_path):
    from app.media.ffprobe import probe
    from app.stages import interpolate

    clip = _clip(tmp_path)
    result = interpolate.run(
        str(clip),
        str(tmp_path),
        {"interp_model": "rife", "rife_provider": "cpu", "target_fps": 24},
    )
    assert result.output_uri
    assert result.meta["interp_model"].startswith("RIFE")
    assert result.meta["provider"] == "CPUExecutionProvider"
    assert result.meta["output_fps"] == pytest.approx(24.0, abs=1.0)
    assert result.meta["gpu_validation_status"] == "pending"

    info = probe(result.output_uri)
    assert info.has_video
    assert info.fps == pytest.approx(24.0, abs=1.0)


def test_rife_does_not_blend_across_a_scene_cut(tmp_path):
    """Frames are never interpolated across a hard cut."""
    from app.stages import interpolate

    clip = _clip(tmp_path)
    result = interpolate.run(
        str(clip),
        str(tmp_path),
        {
            "interp_model": "rife",
            "rife_provider": "cpu",
            "target_fps": 24,
            "scenes": [{"start_s": 0.5, "end_s": 1.0}],
        },
    )
    assert result.meta["scene_cuts_avoided"] == 1
