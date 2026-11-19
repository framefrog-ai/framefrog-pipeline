# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Domain types shared by every stage.

The pipeline is deliberately data-in / data-out: a :class:`JobRequest` goes in,
a manifest dict comes out, and every intermediate artifact is a plain file on
disk (or an object key once object storage is wired in). No stage imports
another stage's internals, which is what lets each one run as its own
serverless GPU function.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

#: Stages that transform the video stream, in the order they should run.
VIDEO_STAGES = ("audio_cleanup", "color_grade", "interpolate", "upscale", "deliver")
#: Stages that only produce metadata.
META_STAGES = ("scene_detect", "rotoscope")
#: The stages a job runs when the caller does not ask for anything specific.
#: Opt-in GPU stages (rotoscope, upscale) are deliberately not here.
DEFAULT_STAGES = (
    "audio_cleanup",
    "scene_detect",
    "color_grade",
    "interpolate",
    "deliver",
)


def new_job_id() -> str:
    """Short, sortable-enough job id: fj_<8 hex>."""
    return "fj_" + uuid.uuid4().hex[:8]


@dataclass
class JobRequest:
    """Everything a single post-production job needs.

    Attributes:
        input_uri: local path or ``s3://bucket/key`` of the source rushes.
        stages: stage names to run, in order. Defaults to the full pipeline.
        options: free-form per-stage parameters (see README "Options").
        output_dir: where local artifacts are written; ``None`` means a temp
            directory under the system temp root.
    """

    input_uri: str
    stages: List[str] = field(default_factory=lambda: list(DEFAULT_STAGES))
    options: Dict[str, Any] = field(default_factory=dict)
    output_dir: Optional[str] = None

    def validate(self, known_stages: List[str]) -> None:
        if not self.input_uri:
            raise ValueError("input_uri is required")
        unknown = [s for s in self.stages if s not in known_stages]
        if unknown:
            raise ValueError(
                f"unknown stage(s): {', '.join(unknown)}; "
                f"known: {', '.join(known_stages)}"
            )
        if not self.stages:
            raise ValueError("stages must not be empty")


@dataclass
class StageResult:
    """Outcome of a single stage.

    ``output_uri`` is usually a video file; for metadata-only stages it is the
    input path unchanged. ``meta`` is JSON-serialisable and is copied verbatim
    into the job manifest.
    """

    stage: str
    output_uri: str
    meta: Dict[str, Any] = field(default_factory=dict)
    duration_s: float = 0.0
