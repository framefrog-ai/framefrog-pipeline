# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Configuration validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.core.config import Settings, get_settings, reset_settings_cache


def test_defaults_are_safe():
    settings = Settings()
    assert settings.app_env == "development"
    assert settings.storage_backend == "local"
    assert settings.auth_required is False
    assert settings.max_output_fps == 60
    assert settings.max_concurrent_jobs >= 1


def test_production_requires_api_key():
    with pytest.raises(PydanticValidationError) as excinfo:
        Settings(app_env="production")
    assert "API_KEY" in str(excinfo.value)


def test_production_rejects_known_dev_keys():
    with pytest.raises(PydanticValidationError):
        Settings(app_env="production", api_key="changeme")


def test_production_accepts_real_key():
    settings = Settings(app_env="production", api_key="s3cret-value")
    assert settings.auth_required is True


def test_development_with_key_requires_auth():
    settings = Settings(app_env="development", api_key="s3cret-value")
    assert settings.auth_required is True


def test_s3_backend_requires_bucket():
    with pytest.raises(PydanticValidationError) as excinfo:
        Settings(storage_backend="s3")
    assert "S3_BUCKET" in str(excinfo.value)


def test_invalid_log_level_rejected():
    with pytest.raises(PydanticValidationError):
        Settings(log_level="chatty")


def test_non_positive_limits_rejected():
    with pytest.raises(PydanticValidationError):
        Settings(max_concurrent_jobs=0)
    with pytest.raises(PydanticValidationError):
        Settings(max_output_fps=0)


def test_sqlite_path_parsing():
    settings = Settings(database_url="sqlite:///./work/x.db")
    assert settings.sqlite_path is not None
    assert settings.sqlite_path.name == "x.db"
    assert Settings(database_url="sqlite:///:memory:").sqlite_path is None
    assert Settings(database_url="postgresql://host/db").sqlite_path is None


def test_extra_input_roots_parsing(tmp_path):
    settings = Settings(allowed_input_roots=f"{tmp_path}, /tmp")
    roots = settings.extra_input_roots
    assert len(roots) == 2
    assert roots[0] == tmp_path.resolve()


def test_settings_are_cached_and_resettable():
    reset_settings_cache()
    first = get_settings()
    assert get_settings() is first
    reset_settings_cache()
    assert get_settings() is not first


def test_environment_is_read(monkeypatch):
    monkeypatch.setenv("MAX_CONCURRENT_JOBS", "7")
    reset_settings_cache()
    try:
        assert get_settings().max_concurrent_jobs == 7
    finally:
        reset_settings_cache()
