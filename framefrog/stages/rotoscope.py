# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Stage — automatic rotoscoping with SAM 2.

SAM 2 tracks an object through a video. This stage turns that into a
deliverable matte:

* **auto** (default) — run SAM 2's automatic mask generator on the first frame,
  pick the largest object, seed the video predictor with its centre point and
  propagate.
* **prompted** — seed from ``rotoscope_prompt`` (points or a box on a chosen
  frame) and propagate.

The stage does not alter the picture: it returns the input video unchanged and
adds artifacts — a grayscale matte video, the per-frame PNG masks, and a
``masks.json`` summary with coverage statistics.

Requirements: the ``sam2`` package (Facebook Research) and a checkpoint. Model
weights are not bundled; point ``sam2_checkpoint`` at a downloaded ``.pt`` and
``sam2_config`` at the package config (default SAM 2.1 large). Everything is
lazy-imported, and a missing runtime fails loudly with install instructions —
a rotoscope request is never silently skipped.

Options
-------
``sam2_checkpoint`` (str, required) — path to ``sam2.1_hiera_*.pt``
``sam2_config`` (str) — ``configs/sam2.1/sam2.1_hiera_l.yaml``
``sam2_device`` (str) — ``cuda`` | ``cpu`` (default: cuda when available)
``rotoscope_mode`` (str) — ``auto`` | ``prompted``
``rotoscope_prompt`` (dict) — ``{"frame": 0, "points": [[x, y]], "labels": [1]}``
  or ``{"frame": 0, "box": [x0, y0, x1, y1]}``
``rotoscope_object`` (int, default 1) — object id used by the predictor
``rotoscope_keep_frames`` (bool) — keep extracted JPEG frames
"""

from __future__ import annotations

import json
import os
import shutil

from framefrog import ffmpeg
from framefrog.types import StageResult


def run(input_path: str, workdir: str, options: dict) -> StageResult:
    checkpoint = options.get("sam2_checkpoint")
    if not checkpoint:
        raise RuntimeError(
            "rotoscope requires options['sam2_checkpoint'] pointing at a SAM 2 "
            "checkpoint; see scripts/fetch_models.py --sam2"
        )
    if not os.path.exists(checkpoint):
        raise RuntimeError(f"SAM 2 checkpoint not found: {checkpoint}")
    try:
        import numpy  # noqa: F401
        import cv2  # noqa: F401
        import torch  # noqa: F401
        from sam2.build_sam import build_sam2_video_predictor  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "SAM 2 rotoscoping requires `sam2`, `torch`, `opencv` and `numpy`; "
            "install requirements-gpu.txt and SAM 2 per its README"
        ) from exc

    import cv2
    import numpy as np
    import torch
    from sam2.build_sam import build_sam2, build_sam2_video_predictor
    from sam2.automatic_mask_generator import SAM2AutomaticMaskGenerator

    info = ffmpeg.probe(input_path)
    fps = info.fps or 24.0
    config = options.get("sam2_config", "configs/sam2.1/sam2.1_hiera_l.yaml")
    device = str(options.get(
        "sam2_device", "cuda" if torch.cuda.is_available() else "cpu"
    ))
    mode = str(options.get("rotoscope_mode", "auto")).lower()
    if mode not in {"auto", "prompted"}:
        raise ValueError("rotoscope_mode must be auto|prompted")

    frames_dir = os.path.join(workdir, "07_sam2_frames")
    masks_dir = os.path.join(workdir, "07_sam2_masks")
    shutil.rmtree(frames_dir, ignore_errors=True)
    shutil.rmtree(masks_dir, ignore_errors=True)
    os.makedirs(frames_dir, exist_ok=True)
    os.makedirs(masks_dir, exist_ok=True)

    ffmpeg.run([
        ffmpeg.ffmpeg_bin(), "-y", "-i", input_path,
        "-q:v", "2", os.path.join(frames_dir, "%05d.jpg"),
    ])

    predictor = build_sam2_video_predictor(config, checkpoint, device=device)
    state = predictor.init_state(video_path=frames_dir)
    object_id = int(options.get("rotoscope_object", 1))
    seed_meta = _seed(
        predictor, state, frames_dir, mode, options, device, config, checkpoint, object_id
    )

    masks_png = []
    areas = []
    for frame_idx, _object_ids, mask_logits in predictor.propagate_in_video(state):
        mask = mask_logits[0]
        if hasattr(mask, "cpu"):
            mask = mask.cpu().numpy()
        mask = np.asarray(mask)
        while mask.ndim > 2:
            mask = mask[0]
        binary = (mask > 0.0).astype(np.uint8) * 255
        path = os.path.join(masks_dir, f"{frame_idx:05d}.png")
        cv2.imwrite(path, binary)
        masks_png.append(path)
        areas.append(float(binary.mean()) / 255.0)

    if not masks_png:
        raise RuntimeError("SAM 2 returned no mask frames")

    matte = os.path.join(workdir, "07_matte.mp4")
    ffmpeg.run([
        ffmpeg.ffmpeg_bin(), "-y",
        "-framerate", f"{fps:.6f}", "-start_number", "0",
        "-i", os.path.join(masks_dir, "%05d.png"),
        "-c:v", "libx264", "-crf", "16", "-preset", "veryfast",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        matte,
    ])

    summary = {
        "object_id": object_id,
        "mode": mode,
        "frames": len(masks_png),
        "fps": round(fps, 3),
        "duration_s": round(len(masks_png) / fps, 3),
        "coverage": {
            "min": round(min(areas), 4),
            "mean": round(sum(areas) / len(areas), 4),
            "max": round(max(areas), 4),
        },
        "mask_dir": masks_dir,
        "matte": matte,
        "checkpoint": os.path.basename(checkpoint),
        "config": os.path.basename(config),
        "device": device,
        "seed": seed_meta,
    }
    with open(os.path.join(workdir, "07_masks.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)

    if not options.get("rotoscope_keep_frames"):
        shutil.rmtree(frames_dir, ignore_errors=True)

    return StageResult(
        "rotoscope",
        input_path,
        {"matte": matte, "masks_dir": masks_dir, "summary": summary, "applied": True},
    )


def _seed(
    predictor,
    state,
    frames_dir: str,
    mode: str,
    options: dict,
    device: str,
    config: str,
    checkpoint: str,
    object_id: int,
) -> dict:
    """Seed the video predictor; auto mode uses SAM 2's mask generator."""
    import cv2
    import numpy as np

    if mode == "prompted":
        prompt = options.get("rotoscope_prompt") or {}
        frame = int(prompt.get("frame", 0))
        if "box" in prompt:
            box = np.asarray(prompt["box"], dtype=np.float32)
            predictor.add_new_points_or_box(
                inference_state=state, frame_idx=frame, obj_id=object_id, box=box
            )
            return {"frame": frame, "box": box.tolist()}
        points = prompt.get("points") or []
        if not points:
            raise ValueError("rotoscope_prompt needs points or a box")
        pts = np.asarray(points, dtype=np.float32)
        labels = np.asarray(prompt.get("labels", [1] * len(points)), dtype=np.int32)
        predictor.add_new_points_or_box(
            inference_state=state, frame_idx=frame, obj_id=object_id,
            points=pts, labels=labels,
        )
        return {"frame": frame, "points": pts.tolist(), "labels": labels.tolist()}

    # auto: find the largest object with the image mask generator, then track it
    from sam2.automatic_mask_generator import SAM2AutomaticMaskGenerator
    from sam2.build_sam import build_sam2

    image_model = build_sam2(config, checkpoint, device=device)
    generator = SAM2AutomaticMaskGenerator(image_model)
    first = cv2.imread(os.path.join(frames_dir, "00001.jpg"))
    if first is None:
        raise RuntimeError("could not read the first extracted frame")
    rgb = cv2.cvtColor(first, cv2.COLOR_BGR2RGB)
    masks = generator.generate(rgb)
    if not masks:
        raise RuntimeError("SAM 2 automatic mask generator found no objects in frame 0")

    best = max(masks, key=lambda m: float(m.get("area", 0)))
    x, y, w, h = [float(v) for v in best["bbox"]]
    cx, cy = x + w / 2.0, y + h / 2.0
    point = np.asarray([[cx, cy]], dtype=np.float32)
    label = np.asarray([1], dtype=np.int32)
    predictor.add_new_points_or_box(
        inference_state=state, frame_idx=0, obj_id=object_id,
        points=point, labels=label,
    )
    return {
        "frame": 0,
        "mode": "auto-largest-object",
        "candidates": len(masks),
        "bbox": [round(x, 1), round(y, 1), round(w, 1), round(h, 1)],
        "point": point.tolist(),
    }
