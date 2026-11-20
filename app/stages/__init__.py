# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Stage registry.

Adding a stage is: write ``app/stages/<name>.py`` with a
``run(input_path, workdir, options) -> StageResult`` function, then register it
here. The orchestrator knows nothing else about it.
"""

from __future__ import annotations

from app.stages import (
    audio_cleanup,
    color_grade,
    deliver,
    interpolate,
    rotoscope,
    scene_detect,
    upscale,
)
from app.stages.base import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PENDING_GPU_VALIDATION,
    STATUS_SKIPPED,
    StageContext,
    StageResult,
)

STAGES = {
    "audio_cleanup": audio_cleanup.run,
    "scene_detect": scene_detect.run,
    "rotoscope": rotoscope.run,
    "color_grade": color_grade.run,
    "interpolate": interpolate.run,
    "upscale": upscale.run,
    "deliver": deliver.run,
}

STAGE_NAMES = list(STAGES)

#: Stages that need a GPU to run as designed.
GPU_STAGES = ("rotoscope", "upscale")

__all__ = [
    "GPU_STAGES",
    "STAGES",
    "STAGE_NAMES",
    "STATUS_CANCELLED",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "STATUS_PENDING_GPU_VALIDATION",
    "STATUS_SKIPPED",
    "StageContext",
    "StageResult",
]
