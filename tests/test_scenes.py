# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Scene detection on a synthetic hard cut — pure OpenCV, no FFmpeg needed."""

import os

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")


def _write_cut_clip(path: str) -> None:
    fps, w, h = 10, 160, 90
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"MJPG"), fps, (w, h))
    assert writer.isOpened(), "OpenCV could not open a VideoWriter"
    for i in range(30):
        colour = (0, 0, 200) if i < 15 else (200, 0, 0)
        writer.write(np.full((h, w, 3), colour, dtype=np.uint8))
    writer.release()


def test_detects_single_cut(tmp_path):
    from framefrog.stages.scenes import _detect_opencv

    clip = str(tmp_path / "cut.avi")
    _write_cut_clip(clip)
    scenes = _detect_opencv(clip, threshold=0.35, min_len=0.5, duration=3.0)
    assert len(scenes) == 2, scenes
    cut = scenes[1]["start_s"]
    assert 1.2 <= cut <= 1.8, f"cut at {cut}s, expected ~1.5s"
    assert scenes[0]["start_s"] == 0.0
    assert scenes[-1]["end_s"] == pytest.approx(3.0, abs=0.3)
