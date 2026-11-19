# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Stage 2 — scene detection.

Finds shot boundaries so grading, proxies and delivery notes can be applied
per shot. Two backends:

1. **PySceneDetect** (``scenedetect[opencv]``) when installed — tuned for
   real footage and handles fades / flashes gracefully.
2. **OpenCV histogram diff** fallback — no extra dependency beyond OpenCV, and
   good enough to drive the demo and small jobs.

The output is a JSON scene list in the work directory and in the manifest:

    [{"index": 0, "start_s": 0.0, "end_s": 3.2, "start_frame": 0, "end_frame": 96}, ...]
"""

from __future__ import annotations

import json
import os
from typing import List, Optional

from framefrog import ffmpeg
from framefrog.types import StageResult


def run(input_path: str, workdir: str, options: dict) -> StageResult:
    threshold = float(options.get("scene_threshold", 0.35))
    min_len = float(options.get("min_scene_s", 0.5))
    info = ffmpeg.probe(input_path)
    duration = info.duration_s

    method = "opencv"
    scenes: Optional[List[dict]] = None
    try:
        scenes = _detect_pyscenedetect(input_path, threshold, min_len)
        method = "pyscenedetect"
    except Exception:
        scenes = None

    if not scenes:
        scenes = _detect_opencv(input_path, threshold, min_len, duration)
        method = "opencv" if method != "pyscenedetect" else "opencv"

    if not scenes:
        scenes = [{"index": 0, "start_s": 0.0, "end_s": duration, "start_frame": 0, "end_frame": 0}]

    scenes_path = os.path.join(workdir, "02_scenes.json")
    with open(scenes_path, "w", encoding="utf-8") as fh:
        json.dump(scenes, fh, indent=2)

    cuts = [round(s["start_s"], 3) for s in scenes[1:]]
    return StageResult(
        "scene_detect",
        input_path,
        {
            "method": method,
            "scene_count": len(scenes),
            "cuts_s": cuts,
            "scenes": scenes,
            "scenes_path": scenes_path,
        },
    )


# --------------------------------------------------------------------------- #
# Backends
# --------------------------------------------------------------------------- #
def _detect_pyscenedetect(path: str, threshold: float, min_len: float) -> List[dict]:
    from scenedetect import ContentDetector, detect  # type: ignore

    scene_list = detect(
        path,
        ContentDetector(threshold=max(1.0, threshold * 100.0)),
        start_in_scene=True,
    )
    out = []
    for i, (start, end) in enumerate(scene_list):
        out.append({
            "index": i,
            "start_s": round(start.get_seconds(), 3),
            "end_s": round(end.get_seconds(), 3),
            "start_frame": start.get_frames(),
            "end_frame": end.get_frames(),
        })
    # PySceneDetect can return one scene covering everything; that is still valid.
    return out


def _detect_opencv(path: str, threshold: float, min_len: float, duration: float) -> List[dict]:
    import cv2
    import numpy as np

    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return []
    fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
    step = max(1, int(round(fps / 5.0)))  # analyse 5 frames per second
    cuts: List[float] = []
    prev_hist = None
    frame_idx = 0
    last_cut = -1e9

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if frame_idx % step == 0:
            small = cv2.resize(frame, (160, 90), interpolation=cv2.INTER_AREA)
            hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
            hist = cv2.calcHist([hsv], [0, 1], None, [16, 16], [0, 180, 0, 256])
            cv2.normalize(hist, hist)
            t = frame_idx / fps
            if prev_hist is not None:
                dist = cv2.compareHist(prev_hist, hist, cv2.HISTCMP_BHATTACHARYYA)
                if dist > threshold and (t - last_cut) >= min_len:
                    cuts.append(t)
                    last_cut = t
            prev_hist = hist
        frame_idx += 1
    cap.release()

    bounds = [0.0] + cuts + [duration or (frame_idx / fps)]
    scenes = []
    for i in range(len(bounds) - 1):
        scenes.append({
            "index": i,
            "start_s": round(bounds[i], 3),
            "end_s": round(bounds[i + 1], 3),
            "start_frame": int(round(bounds[i] * fps)),
            "end_frame": int(round(bounds[i + 1] * fps)),
        })
    return scenes
