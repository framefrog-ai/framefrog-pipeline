# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Stage 2 — scene detection.

Finds shot boundaries so grading, proxies and delivery notes can be applied
per shot. Two backends:

1. **PySceneDetect** (``scenedetect``) when installed — tuned for real
   footage and handles fades / flashes gracefully.
2. **OpenCV histogram diff** fallback — no extra dependency beyond OpenCV, and
   good enough to drive the demo and small jobs.

The output is a JSON scene list in the work directory and in the manifest:

    [{"index": 0, "start_s": 0.0, "end_s": 3.2, "duration_s": 3.2,
      "start_frame": 0, "end_frame": 96,
      "start_timecode": "00:00:00.000", "end_timecode": "00:00:03.200"}, ...]
"""

from __future__ import annotations

import json
import os
from typing import Any

from app.media import ffmpeg
from app.stages.base import StageResult


def _timecode(seconds: float, fps: float) -> str:
    """SMPTE-ish ``HH:MM:SS.mmm`` timecode for a timestamp."""
    total_ms = int(round(max(0.0, seconds) * 1000.0))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def _scene(index: int, start_s: float, end_s: float, fps: float) -> dict[str, Any]:
    start_frame = int(round(start_s * fps))
    end_frame = int(round(end_s * fps))
    return {
        "index": index,
        "start_s": round(start_s, 3),
        "end_s": round(end_s, 3),
        "duration_s": round(end_s - start_s, 3),
        "start_frame": start_frame,
        "end_frame": end_frame,
        "start_timecode": _timecode(start_s, fps),
        "end_timecode": _timecode(end_s, fps),
    }


def run(input_path: str, workdir: str, options: dict) -> StageResult:
    """Detect scenes and write ``02_scenes.json`` into the work directory."""
    threshold = float(options.get("scene_threshold", 0.27))
    min_len = float(options.get("min_scene_s", 0.5))
    info = ffmpeg.probe(input_path)
    duration = info.duration_s
    fps = info.fps or 25.0

    method = "opencv"
    scenes: list[dict] | None = None
    try:
        scenes = _detect_pyscenedetect(input_path, threshold, min_len, fps)
        method = "pyscenedetect"
    except Exception:
        scenes = None

    if not scenes:
        scenes = _detect_opencv(input_path, threshold, min_len, duration, fps)
        method = "opencv"

    if not scenes:
        scenes = [_scene(0, 0.0, duration, fps)]

    scenes_path = os.path.join(workdir, "02_scenes.json")
    with open(scenes_path, "w", encoding="utf-8") as handle:
        json.dump(scenes, handle, indent=2)

    cuts = [round(scene["start_s"], 3) for scene in scenes[1:]]
    return StageResult(
        "scene_detect",
        input_path,
        {
            "method": method,
            "scene_count": len(scenes),
            "cuts_s": cuts,
            "scenes": scenes,
            "scenes_path": scenes_path,
            "threshold": threshold,
            "minimum_scene_length_seconds": min_len,
            "fps": round(fps, 3),
        },
    )


# --------------------------------------------------------------------------- #
# Backends
# --------------------------------------------------------------------------- #
def _detect_pyscenedetect(path: str, threshold: float, min_len: float, fps: float) -> list[dict]:
    from scenedetect import ContentDetector, detect  # type: ignore

    scene_list = detect(
        path,
        ContentDetector(threshold=max(1.0, threshold * 100.0)),
        start_in_scene=True,
    )
    out: list[dict] = []
    for index, (start, end) in enumerate(scene_list):
        out.append(_scene(index, float(start.seconds), float(end.seconds), fps))
    return out


def _detect_opencv(
    path: str, threshold: float, min_len: float, duration: float, fps: float = 25.0
) -> list[dict]:
    import cv2

    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return []
    src_fps = cap.get(cv2.CAP_PROP_FPS) or fps
    step = max(1, int(round(src_fps / 5.0)))  # analyse 5 frames per second
    cuts: list[float] = []
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
            timestamp = frame_idx / src_fps
            if prev_hist is not None:
                dist = cv2.compareHist(prev_hist, hist, cv2.HISTCMP_BHATTACHARYYA)
                if dist > threshold and (timestamp - last_cut) >= min_len:
                    cuts.append(timestamp)
                    last_cut = timestamp
            prev_hist = hist
        frame_idx += 1
    cap.release()

    total = duration or (frame_idx / src_fps)
    bounds = [0.0] + cuts + [total]
    return [_scene(index, bounds[index], bounds[index + 1], src_fps) for index in range(len(bounds) - 1)]
