# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Domain models shared by the API, the runner and the stages.

Two layers live here:

* plain dataclasses (:class:`JobRequest`, :class:`StageResult`) used by the
  stage implementations and the CLI — they are cheap to build and easy to
  serialise;
* the persisted :class:`JobRecord` shape used by the job repository.

The HTTP request/response schemas live in :mod:`app.api.schemas` so that the
wire format can evolve without touching the stages.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

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

#: Every stage the application knows about.
KNOWN_STAGES = DEFAULT_STAGES + ("rotoscope", "upscale")


def new_job_id() -> str:
    """Short, sortable-enough job id: fj_<8 hex>."""
    return "fj_" + uuid.uuid4().hex[:8]


def utc_now() -> str:
    """UTC timestamp in ISO-8601 (second resolution)."""
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class JobRequest:
    """Everything a single post-production job needs.

    Attributes:
        input_uri: local path or ``s3://bucket/key`` of the source rushes.
        stages: stage names to run, in order. Defaults to the full pipeline.
        options: free-form per-stage parameters (see README "Options").
        output_dir: where local artifacts are written; ``None`` means a temp
            directory under the system temp root.
        idempotency_key: optional client key that makes job creation
            repeatable.
    """

    input_uri: str
    stages: list[str] = field(default_factory=lambda: list(DEFAULT_STAGES))
    options: dict[str, Any] = field(default_factory=dict)
    output_dir: str | None = None
    idempotency_key: str | None = None

    def validate(self, known_stages: list[str]) -> None:
        if not self.input_uri:
            raise ValueError("input_uri is required")
        unknown = [s for s in self.stages if s not in known_stages]
        if unknown:
            raise ValueError(f"unknown stage(s): {', '.join(unknown)}; known: {', '.join(known_stages)}")
        if not self.stages:
            raise ValueError("stages must not be empty")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StageResult:
    """Outcome of a single stage.

    ``output_uri`` is usually a video file; for metadata-only stages it is the
    input path unchanged. ``meta`` is JSON-serialisable and is copied verbatim
    into the job manifest.
    """

    stage: str
    output_uri: str
    meta: dict[str, Any] = field(default_factory=dict)
    duration_s: float = 0.0
    status: str = "completed"
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class JobRecord:
    """The persisted representation of a job."""

    job_id: str
    status: str
    request: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None
    current_stage: str | None = None
    progress_percent: int = 0
    created_at: str = field(default_factory=utc_now)
    started_at: str | None = None
    completed_at: str | None = None
    input_metadata: dict[str, Any] = field(default_factory=dict)
    stage_results: list[dict[str, Any]] = field(default_factory=list)
    outputs: list[dict[str, Any]] = field(default_factory=list)
    error: dict[str, Any] | None = None
    cancel_requested: bool = False
    gpu_validation_status: str = "pending"
    manifest: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
