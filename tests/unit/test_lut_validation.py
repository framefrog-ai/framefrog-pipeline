# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""LUT validation and fingerprinting."""

from __future__ import annotations

import pytest

from app.media.lut import ensure_lut, lut_fingerprint, validate_lut, write_lut


def test_builtin_lut_is_valid(tmp_path):
    path = write_lut("cinematic_warm", str(tmp_path / "cinematic_warm.cube"), size=5)
    fingerprint = validate_lut(path)
    assert fingerprint["size"] == 5
    assert fingerprint["entries"] == 125
    assert len(fingerprint["sha256"]) == 64


def test_wrong_extension_rejected(tmp_path):
    path = tmp_path / "look.cube.txt"
    path.write_text("LUT_3D_SIZE 2\n")
    with pytest.raises(ValueError, match="extension"):
        validate_lut(str(path))


def test_missing_file_rejected(tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        validate_lut(str(tmp_path / "missing.cube"))


def test_empty_file_rejected(tmp_path):
    path = tmp_path / "empty.cube"
    path.write_text("")
    with pytest.raises(ValueError, match="empty"):
        validate_lut(str(path))


def test_missing_size_declaration_rejected(tmp_path):
    path = tmp_path / "bad.cube"
    path.write_text("0 0 0\n")
    with pytest.raises(ValueError, match="LUT_3D_SIZE"):
        validate_lut(str(path))


def test_truncated_data_rejected(tmp_path):
    path = tmp_path / "short.cube"
    path.write_text("LUT_3D_SIZE 3\n0.0 0.0 0.0\n")
    with pytest.raises(ValueError, match="entries"):
        validate_lut(str(path))


def test_out_of_range_row_rejected(tmp_path):
    path = tmp_path / "range.cube"
    path.write_text("LUT_3D_SIZE 2\n0.0 0.0 0.0\n" + "5.0 0.0 0.0\n" * 7)
    with pytest.raises(ValueError, match="out of range"):
        validate_lut(str(path))


def test_ensure_lut_generates_builtin(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMEFROG_LUT_DIR", str(tmp_path))
    path = ensure_lut("cinematic_warm")
    assert path.endswith(".cube")
    assert validate_lut(path)["size"] > 0


def test_ensure_lut_rejects_unknown_name(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMEFROG_LUT_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="unknown LUT"):
        ensure_lut("does-not-exist")


def test_fingerprint_is_stable(tmp_path):
    path = write_lut("neutral_film", str(tmp_path / "n.cube"), size=4)
    first = lut_fingerprint(path)
    second = lut_fingerprint(path)
    assert first["sha256"] == second["sha256"]
