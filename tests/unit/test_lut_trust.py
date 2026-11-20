# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The LUT trust boundary.

A colour grade applies a file from disk, so only a built-in look or a file
inside a configured LUT directory may be used. A caller must not be able to
make the pipeline read an arbitrary file from the host.
"""

from __future__ import annotations

import pytest

from app.core.errors import ValidationError
from app.media.lut import write_lut
from app.media.lut_resolver import is_trusted_lut_path, resolve_lut, trusted_lut_dirs


def test_builtin_look_resolves():
    path = resolve_lut("cinematic_warm")
    assert path.endswith(".cube")


def test_unknown_look_is_rejected():
    with pytest.raises(ValidationError) as excinfo:
        resolve_lut("does-not-exist")
    assert "built_ins" in excinfo.value.details


def test_remote_lut_is_rejected():
    with pytest.raises(ValidationError):
        resolve_lut("s3://bucket/looks/mine.cube")


def test_file_outside_a_trusted_directory_is_rejected(tmp_path):
    outside = tmp_path / "evil.cube"
    write_lut("cinematic_warm", str(outside), size=4)
    with pytest.raises(ValidationError) as excinfo:
        resolve_lut(str(outside))
    assert "trusted" in str(excinfo.value)


def test_file_inside_a_configured_directory_is_accepted(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMEFROG_LUT_DIR", str(tmp_path))
    look = tmp_path / "mine.cube"
    write_lut("neutral_film", str(look), size=4)
    assert is_trusted_lut_path(str(look))
    assert resolve_lut(str(look)) == str(look)


def test_committed_look_is_trusted():
    committed = trusted_lut_dirs()[0] / "cinematic_warm.cube"
    assert committed.exists(), "the repository must ship the built-in look"
    assert is_trusted_lut_path(str(committed))
    assert resolve_lut(str(committed)).endswith("cinematic_warm.cube")


def test_empty_reference_is_rejected():
    with pytest.raises(ValidationError):
        resolve_lut("")


def test_a_directory_is_not_a_lut(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMEFROG_LUT_DIR", str(tmp_path))
    with pytest.raises(ValidationError):
        resolve_lut(str(tmp_path))
