# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""LUT generation and fingerprinting — no FFmpeg needed."""

import os

from framefrog.lut import (
    BUILTIN_LOOKS,
    LUT_SIZE,
    ensure_lut,
    lut_fingerprint,
    write_lut,
)


def test_generates_every_builtin(tmp_path):
    for name in BUILTIN_LOOKS:
        path = os.path.join(str(tmp_path), f"{name}.cube")
        write_lut(name, path)
        assert os.path.exists(path)
        with open(path, encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln and not ln[0].isalpha()]
        assert len(lines) == LUT_SIZE ** 3, f"{name}: {len(lines)} entries"


def test_ensure_lut_reuses_existing(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMEFROG_LUT_DIR", str(tmp_path))
    path = ensure_lut("teal_orange")
    assert os.path.exists(path)
    again = ensure_lut("teal_orange")
    assert path == again


def test_fingerprint(tmp_path):
    path = os.path.join(str(tmp_path), "cinematic_warm.cube")
    write_lut("cinematic_warm", path)
    fp = lut_fingerprint(path)
    assert fp["size"] == LUT_SIZE
    assert len(fp["sha256"]) == 64


def test_unknown_lut_raises():
    import pytest

    with pytest.raises(ValueError):
        ensure_lut("does_not_exist")
