# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Typed application configuration.

Everything the process needs is declared here once, validated on import of
the settings object, and read through :func:`get_settings`. Nothing else in
the code base is allowed to call ``os.environ`` for behaviour that matters
(only the legacy ``FRAMEFROG_*`` compatibility names are still honoured by
:mod:`app.core.ops`).

The environment variable names are the public contract and are documented in
``.env.example``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: API keys that must never reach production.
INSECURE_API_KEYS = {"", "changeme", "change-me", "secret", "dev", "test", "local"}

AppEnv = Literal["development", "test", "production"]
StorageBackend = Literal["local", "s3"]
ComputeDevice = Literal["auto", "cpu", "cuda"]


class Settings(BaseSettings):
    """Validated application settings (environment driven)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        # A field with an alias may still be set by its own name, so tests and
        # code can pass `signing_secret=...` directly.
        populate_by_name=True,
    )

    # -- service ---------------------------------------------------------- #
    app_env: AppEnv = "development"
    log_level: str = "INFO"
    api_key: SecretStr | None = None

    #: HMAC key used to sign artefact download links. Without it no signed
    #: link is issued.
    signing_secret: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("SIGNING_SECRET", "FRAMEFROG_SIGNING_SECRET"),
    )
    signing_ttl_seconds: int = Field(
        default=3600,
        validation_alias=AliasChoices("SIGNING_TTL_SECONDS", "FRAMEFROG_SIGNING_TTL"),
    )

    # -- storage / persistence -------------------------------------------- #
    work_dir: Path = Path("./work")
    database_url: str = "sqlite:///./work/framefrog.db"
    storage_backend: StorageBackend = "local"

    # -- media limits ------------------------------------------------------ #
    max_upload_bytes: int = 2 * 1024**3
    max_video_duration_seconds: float = 3600.0
    max_video_width: int = 3840
    max_video_height: int = 2160
    max_output_fps: int = 60
    max_concurrent_jobs: int = 2

    # -- binaries ----------------------------------------------------------- #
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"

    # -- object storage ------------------------------------------------------ #
    s3_endpoint_url: str | None = None
    s3_region: str = "us-east-1"
    s3_bucket: str | None = None
    s3_access_key_id: SecretStr | None = None
    s3_secret_access_key: SecretStr | None = None
    s3_input_prefix: str = "inputs"
    s3_intermediate_prefix: str = "intermediate"
    s3_output_prefix: str = "outputs"
    presigned_url_expiry_seconds: int = 900

    # -- models -------------------------------------------------------------- #
    model_cache_dir: Path = Path("./models")
    deepfilternet_enabled: bool = True
    rife_enabled: bool = True
    rife_repository_path: Path | None = None
    rife_model_path: Path | None = None

    # -- compute -------------------------------------------------------------- #
    compute_device: ComputeDevice = "auto"
    gpu_validation_required: bool = False

    #: Additional directories that may be used as local job inputs, as a
    #: comma-separated list. ``WORK_DIR`` is always allowed.
    allowed_input_roots: str = ""

    #: Allow local inputs from anywhere on the filesystem. Off by default:
    #: the API only accepts inputs from ``WORK_DIR`` and the explicitly
    #: allowed roots. The local CLI turns this on for the operator.
    allow_external_local_inputs: bool = False

    # ------------------------------------------------------------------ #
    # validation
    # ------------------------------------------------------------------ #
    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        level = value.strip().upper()
        if level not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}:
            raise ValueError(f"invalid LOG_LEVEL {value!r}")
        return level

    @field_validator("max_upload_bytes", "max_concurrent_jobs", "presigned_url_expiry_seconds")
    @classmethod
    def _positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("must be greater than zero")
        return value

    @field_validator("max_video_duration_seconds", "max_video_width", "max_video_height", "max_output_fps")
    @classmethod
    def _positive_number(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("must be greater than zero")
        return value

    @model_validator(mode="after")
    def _check_consistency(self) -> Settings:
        if self.app_env == "production":
            if not self.api_key or not self.api_key.get_secret_value():
                raise ValueError("API_KEY is mandatory in production; refusing to start without it")
            if self.api_key.get_secret_value().strip().lower() in INSECURE_API_KEYS:
                raise ValueError("API_KEY is a known development value; refusing to start")
        if self.storage_backend == "s3" and not self.s3_bucket:
            raise ValueError("S3_BUCKET is required when STORAGE_BACKEND=s3")
        if self.compute_device == "cuda" and self.gpu_validation_required is False:
            # Not an error: the device may simply not be present yet. The
            # capability probe reports the truth at runtime.
            pass
        return self

    # ------------------------------------------------------------------ #
    # derived helpers
    # ------------------------------------------------------------------ #
    @property
    def auth_required(self) -> bool:
        """API key authentication is mandatory outside explicit development."""
        if self.app_env != "development":
            return True
        return bool(self.api_key and self.api_key.get_secret_value())

    @property
    def sqlite_path(self) -> Path | None:
        """Filesystem path of the SQLite database, when the URL points at one."""
        url = self.database_url
        if not url.startswith("sqlite"):
            return None
        _, _, tail = url.partition("://")
        tail = tail.lstrip("/")
        if not tail or tail == ":memory:":
            return None
        return Path(tail)

    @property
    def work_dir_resolved(self) -> Path:
        return self.work_dir.expanduser().resolve()

    @property
    def extra_input_roots(self) -> list[Path]:
        """Extra directories that may hold local inputs."""
        roots: list[Path] = []
        for chunk in (self.allowed_input_roots or "").split(","):
            text = chunk.strip()
            if not text:
                continue
            roots.append(Path(text).expanduser().resolve(strict=False))
        return roots


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton (cached)."""
    return Settings()


def operator_settings() -> Settings:
    """Settings for local operator tooling (CLI, demo).

    The operator runs on the same machine as the footage, so local inputs
    outside ``WORK_DIR`` are allowed. The HTTP API never uses this.
    """
    return get_settings().model_copy(update={"allow_external_local_inputs": True})


def reset_settings_cache() -> None:
    """Drop the cached settings — used by tests that patch the environment."""
    get_settings.cache_clear()
