"""Stage registry.

Adding a stage is: write ``framefrog/stages/<name>.py`` with a
``run(input_path, workdir, options) -> StageResult`` function, then register it
here. The orchestrator knows nothing else about it.
"""

from __future__ import annotations

from framefrog.stages import audio, deliver, grade, interpolate, scenes

STAGES = {
    "audio_cleanup": audio.run,
    "scene_detect": scenes.run,
    "color_grade": grade.run,
    "interpolate": interpolate.run,
    "deliver": deliver.run,
}

STAGE_NAMES = list(STAGES)
