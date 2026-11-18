"""Stage 4 — frame interpolation.

Raises the frame rate to 60/120 fps with motion-compensated interpolation.
Two quality levels:

* ``fast`` (default) — FFmpeg ``minterpolate`` in blend mode: cheap, stable,
  good for previews and social.
* ``high`` — full motion-compensated (``mci`` + bidir + vsbmc), GPU-friendly
  on the deployed worker, slow on a laptop.

If the source is already at or above the target rate, the stage is a no-op and
reports ``skipped: True`` instead of wasting compute.
"""

from __future__ import annotations

import os
import shutil

from framefrog import ffmpeg
from framefrog.types import StageResult


def run(input_path: str, workdir: str, options: dict) -> StageResult:
    out = os.path.join(workdir, "04_interpolated.mp4")
    target = int(options.get("target_fps", 60))
    quality = str(options.get("interp_quality", "fast")).lower()
    info = ffmpeg.probe(input_path)

    if info.fps and info.fps >= target - 0.5:
        shutil.copyfile(input_path, out)
        return StageResult(
            "interpolate",
            out,
            {
                "target_fps": target,
                "source_fps": round(info.fps, 3),
                "skipped": True,
                "reason": "source already at target rate",
            },
        )

    if quality == "high":
        vf = (
            f"minterpolate=fps={target}:mi_mode=mci:mc_mode=aobmc:"
            "me_mode=bidir:vsbmc=1"
        )
    else:
        vf = f"minterpolate=fps={target}:mi_mode=blend"

    ffmpeg.run([
        ffmpeg.ffmpeg_bin(), "-y", "-i", input_path,
        "-vf", vf,
        "-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        out,
    ])

    return StageResult(
        "interpolate",
        out,
        {
            "target_fps": target,
            "source_fps": round(info.fps, 3),
            "quality": quality,
            "skipped": False,
        },
    )
