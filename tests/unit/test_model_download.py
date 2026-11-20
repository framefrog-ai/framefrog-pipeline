# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Model download verification.

A model whose digest does not match the pinned value must never be used, so
the download script removes it and fails.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_download_module():
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "framefrog_download_models", root / "scripts" / "download_models.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def download_models():
    return _load_download_module()


def test_rife_graph_has_a_pinned_digest(download_models):
    pins = download_models.PINNED_SHA256
    assert "rife-v4.9-ensemble.onnx" in pins
    assert len(pins["rife-v4.9-ensemble.onnx"]) == 64


def test_matching_file_verifies(download_models, tmp_path, capsys):
    target = tmp_path / "rife-v4.9-ensemble.onnx"
    target.write_bytes(b"not the real graph")
    # Pin the digest of this file so the verification has something to compare.
    download_models.PINNED_SHA256["rife-v4.9-ensemble.onnx"] = download_models.sha256(str(target))
    digest = download_models.verify(str(target))
    assert digest == download_models.sha256(str(target))
    assert target.exists()
    assert "matches the pinned digest" in capsys.readouterr().out


def test_mismatching_file_is_removed_and_fails(download_models, tmp_path, capsys):
    target = tmp_path / "rife-v4.9-ensemble.onnx"
    target.write_bytes(b"tampered")
    download_models.PINNED_SHA256["rife-v4.9-ensemble.onnx"] = "0" * 64
    with pytest.raises(SystemExit) as excinfo:
        download_models.verify(str(target))
    assert excinfo.value.code == 1
    assert not target.exists(), "an unverified artifact must be removed"
    assert "EXPECTED" in capsys.readouterr().out


def test_artifact_without_a_pin_is_reported_but_kept(download_models, tmp_path, capsys):
    target = tmp_path / "something-else.bin"
    target.write_bytes(b"data")
    digest = download_models.verify(str(target))
    assert digest == download_models.sha256(str(target))
    assert target.exists()
    assert "no pinned digest" in capsys.readouterr().out
