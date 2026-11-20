# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Rotoscoping guards and the opt-in stage contract."""

import pytest

from framefrog.stages import STAGE_NAMES
from framefrog.stages import rotoscope
from framefrog.types import DEFAULT_STAGES


def test_gpu_stages_are_opt_in():
    assert "rotoscope" in STAGE_NAMES
    assert "upscale" in STAGE_NAMES
    assert "rotoscope" not in DEFAULT_STAGES
    assert "upscale" not in DEFAULT_STAGES


def test_requires_a_checkpoint():
    with pytest.raises(RuntimeError, match="sam2_checkpoint"):
        rotoscope.run("anything.mp4", "/tmp", {})


def test_missing_checkpoint_file_fails(tmp_path):
    with pytest.raises(RuntimeError, match="checkpoint not found"):
        rotoscope.run("anything.mp4", str(tmp_path), {"sam2_checkpoint": "/nope.pt"})
