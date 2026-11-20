# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""HTTP request and response schemas.

The API validates everything it can *before* a job is created: unknown
stages, out-of-range values, unsupported codecs, unusable LUT references and
malformed URIs are rejected with a 422 and a precise message.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.errors import ValidationError
from app.jobs.models import KNOWN_STAGES

#: Presets accepted by libx264 (the CPU delivery encoder).
ALLOWED_PRESETS = {
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "slower",
    "veryslow",
}

#: Hard ceiling for interpolated output; 120 fps is explicitly out of scope.
MAX_INTERPOLATED_FPS = 60


class SceneDetectOptions(BaseModel):
    """Shot-boundary detection."""

    model_config = ConfigDict(extra="forbid")

    threshold: float = Field(default=27.0, ge=1.0, le=255.0)
    minimum_scene_length_seconds: float = Field(default=0.5, ge=0.0, le=60.0)


class AudioCleanupOptions(BaseModel):
    """Speech cleanup and loudness normalisation."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    target_lufs: float = Field(default=-16.0, ge=-40.0, le=-5.0)
    backend: Literal["auto", "deepfilternet", "ffmpeg"] = "auto"


class InterpolateOptions(BaseModel):
    """Frame interpolation (RIFE adapter)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    target_fps: int = Field(default=60, ge=1, le=MAX_INTERPOLATED_FPS)
    device: Literal["auto", "cpu", "cuda"] = "auto"


class ColorGradeOptions(BaseModel):
    """3D LUT grading."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    lut_uri: str | None = None
    intensity: float = Field(default=1.0, ge=0.0, le=1.0)

    @field_validator("lut_uri")
    @classmethod
    def _cube_only(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not value.lower().endswith(".cube"):
            raise ValueError("lut_uri must reference a .cube file")
        if ".." in value:
            raise ValueError("lut_uri must not contain path traversal")
        return value

    @model_validator(mode="after")
    def _intensity_is_full(self) -> ColorGradeOptions:
        # Blending is only supported at full strength until it is implemented
        # and tested; anything else would silently produce a wrong grade.
        if self.enabled and abs(self.intensity - 1.0) > 1e-6:
            raise ValueError("color_grade.intensity must be 1.0 — partial blending is not implemented yet")
        return self


class FanoutOptions(BaseModel):
    """Per-shot processing of the expensive stages."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["off", "auto", "shots"] = "off"
    workers: int = Field(default=4, ge=1, le=32)
    mode_backend: Literal["inline", "queue"] = "inline"


class DeliverOptions(BaseModel):
    """Final delivery (MP4 / H.264 / AAC)."""

    model_config = ConfigDict(extra="forbid")

    container: Literal["mp4"] = "mp4"
    video_codec: Literal["h264"] = "h264"
    audio_codec: Literal["aac"] = "aac"
    crf: int = Field(default=20, ge=0, le=51)
    preset: str = "medium"

    @field_validator("preset")
    @classmethod
    def _known_preset(cls, value: str) -> str:
        if value not in ALLOWED_PRESETS:
            raise ValueError(f"preset must be one of: {', '.join(sorted(ALLOWED_PRESETS))}")
        return value


class JobOptions(BaseModel):
    """Per-stage options."""

    model_config = ConfigDict(extra="forbid")

    scene_detect: SceneDetectOptions = Field(default_factory=SceneDetectOptions)
    fanout: FanoutOptions = Field(default_factory=FanoutOptions)
    audio_cleanup: AudioCleanupOptions = Field(default_factory=AudioCleanupOptions)
    interpolate: InterpolateOptions = Field(default_factory=InterpolateOptions)
    color_grade: ColorGradeOptions = Field(default_factory=ColorGradeOptions)
    deliver: DeliverOptions = Field(default_factory=DeliverOptions)


class JobCreateRequest(BaseModel):
    """Body of ``POST /v1/jobs``."""

    model_config = ConfigDict(extra="forbid")

    input_uri: str = Field(..., min_length=1, max_length=2048)
    stages: list[str] | None = None
    options: JobOptions = Field(default_factory=JobOptions)
    idempotency_key: str | None = Field(default=None, max_length=200)

    @field_validator("stages")
    @classmethod
    def _known_stages(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        if not value:
            raise ValueError("stages must not be empty")
        unknown = [stage for stage in value if stage not in KNOWN_STAGES]
        if unknown:
            raise ValueError(f"unknown stage(s): {', '.join(unknown)}; known: {', '.join(KNOWN_STAGES)}")
        duplicates = {s for s in value if value.count(s) > 1}
        if duplicates:
            raise ValueError(f"duplicate stage(s): {', '.join(sorted(duplicates))}")
        return value

    @field_validator("idempotency_key")
    @classmethod
    def _key_charset(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not all(ch.isalnum() or ch in "-_." for ch in value):
            raise ValueError("idempotency_key may only contain letters, digits, '-', '_' and '.'")
        return value


class UploadUrlRequest(BaseModel):
    """Body of ``POST /v1/jobs/upload-url``."""

    model_config = ConfigDict(extra="forbid")

    filename: str = Field(..., min_length=1, max_length=255)
    content_type: str = Field(default="video/mp4", max_length=128)

    @field_validator("filename")
    @classmethod
    def _safe_filename(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("filename must not be empty")
        if "/" in name or "\\" in name:
            raise ValueError("filename must not contain a path separator")
        if name in {".", ".."} or name.startswith("."):
            raise ValueError("filename is not a valid object name")
        if "\x00" in name:
            raise ValueError("filename contains a null byte")
        return name


class UploadUrlResponse(BaseModel):
    """Where to PUT the file and which URI to use as the job input."""

    model_config = ConfigDict(extra="allow")

    input_uri: str
    upload_url: str
    method: str = "PUT"
    expires_in: int
    key: str


class StageResultModel(BaseModel):
    """One stage of a job."""

    model_config = ConfigDict(extra="allow")

    stage: str
    status: str
    duration_s: float = 0.0
    output: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class OutputModel(BaseModel):
    """One artifact produced by a job."""

    model_config = ConfigDict(extra="allow")

    kind: str
    path: str | None = None
    uri: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    duration_s: float | None = None


class ErrorModel(BaseModel):
    """Structured error payload."""

    model_config = ConfigDict(extra="allow")

    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class JobResponse(BaseModel):
    """Response body for every job endpoint."""

    model_config = ConfigDict(extra="allow")

    job_id: str
    status: str
    current_stage: str | None = None
    progress_percent: int = 0
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    input_metadata: dict[str, Any] = Field(default_factory=dict)
    stage_results: list[StageResultModel] = Field(default_factory=list)
    outputs: list[OutputModel] = Field(default_factory=list)
    error: ErrorModel | None = None
    gpu_validation_status: str = "pending"


class HealthResponse(BaseModel):
    """Liveness payload."""

    model_config = ConfigDict(extra="allow")

    status: str
    name: str = "framefrog-pipeline"
    version: str
    uptime_s: float
    app_env: str


class ReadyResponse(BaseModel):
    """Readiness payload — 503 when a dependency is not usable."""

    model_config = ConfigDict(extra="allow")

    status: str
    checks: dict[str, Any] = Field(default_factory=dict)


def disabled_stages(options: JobOptions) -> list[str]:
    """Stages the caller explicitly switched off.

    Only an *explicit* ``enabled: false`` disables a stage — the default
    value of a stage option never silently removes a requested stage.
    """
    disabled: list[str] = []
    candidates = (
        ("audio_cleanup", options.audio_cleanup),
        ("interpolate", options.interpolate),
        ("color_grade", options.color_grade),
    )
    for name, model in candidates:
        if "enabled" in model.model_fields_set and not model.enabled:
            disabled.append(name)
    return disabled


def to_stage_options(options: JobOptions) -> dict[str, Any]:
    """Translate the API options into the flat option dict the stages use."""
    flat: dict[str, Any] = {
        "scene_threshold": options.scene_detect.threshold / 100.0,
        "min_scene_s": options.scene_detect.minimum_scene_length_seconds,
        "lufs": options.audio_cleanup.target_lufs,
        "denoise_model": options.audio_cleanup.backend,
        "target_fps": options.interpolate.target_fps,
        "rife_provider": "auto" if options.interpolate.device == "auto" else options.interpolate.device,
        "interp_model": "auto",
        "look_strength": options.color_grade.intensity,
        "master_crf": options.deliver.crf,
        "master_preset": options.deliver.preset,
        "encoder": "auto",
        "fanout": options.fanout.mode,
        "fanout_mode": options.fanout.mode_backend,
        "fanout_workers": options.fanout.workers,
    }
    if options.color_grade.enabled:
        flat["lut"] = options.color_grade.lut_uri or "cinematic_warm"
    disabled = disabled_stages(options)
    if disabled:
        # Private key: stripped from the manifest by the pipeline.
        flat["_disabled_stages"] = disabled
    return flat


def validate_target_fps(target_fps: int, limit: int) -> int:
    """Reject a target frame rate above the configured maximum."""
    if target_fps > limit:
        raise ValidationError(
            "target_fps exceeds the configured maximum",
            details={"target_fps": target_fps, "max_output_fps": limit},
        )
    return target_fps
