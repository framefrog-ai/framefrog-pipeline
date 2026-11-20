# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Resolve a LUT reference to a file that may be applied.

A colour grade applies a file from disk, so the reference must come from a
trusted place. Three sources are accepted:

1. a **built-in look** (``cinematic_warm``, ``teal_orange``, ``neutral_film``);
2. a file inside a **configured LUT directory** — the repository's
   ``assets/luts``, ``$FRAMEFROG_LUT_DIR``, ``$MODEL_CACHE_DIR/luts`` or
   ``$WORK_DIR/luts``;
3. an explicit path, but only when it resolves inside one of those
   directories.

Anything else is refused: a caller must not be able to make the pipeline read
an arbitrary file from the host.
"""

from __future__ import annotations

import os
from pathlib import Path

from app.core.errors import ValidationError
from app.media.lut import BUILTIN_LOOKS, ensure_lut, lut_fingerprint, validate_lut, write_lut

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP_ROOT = os.path.dirname(_HERE)
_REPO_ROOT = os.path.dirname(_APP_ROOT)


def trusted_lut_dirs() -> list[Path]:
    """Directories a LUT file may be read from."""
    dirs: list[Path] = [
        Path(_REPO_ROOT) / "assets" / "luts",
        Path(_HERE) / "assets" / "luts",
    ]
    value = os.environ.get("FRAMEFROG_LUT_DIR")
    if value:
        dirs.insert(0, Path(value))
    model_cache = os.environ.get("MODEL_CACHE_DIR")
    if model_cache:
        dirs.append(Path(model_cache) / "luts")
    work_dir = os.environ.get("WORK_DIR")
    if work_dir:
        dirs.append(Path(work_dir) / "luts")
    return dirs


def is_trusted_lut_path(path: str) -> bool:
    """Whether ``path`` lives inside one of the trusted LUT directories."""
    try:
        resolved = Path(path).expanduser().resolve(strict=False)
    except OSError:
        return False
    for directory in trusted_lut_dirs():
        try:
            base = directory.expanduser().resolve(strict=False)
        except OSError:  # pragma: no cover - permissions
            continue
        try:
            resolved.relative_to(base)
        except ValueError:
            continue
        return True
    return False


def resolve_lut(reference: str) -> str:
    """Resolve a LUT reference to a usable file path.

    Raises :class:`ValidationError` when the reference is not a built-in look
    and not a file inside a trusted LUT directory.
    """
    raw = str(reference or "").strip()
    if not raw:
        raise ValidationError("no LUT reference was given")

    if raw.startswith("s3://"):
        raise ValidationError(
            "remote LUTs are not supported by the grade stage; "
            "fetch the .cube into a trusted LUT directory first"
        )

    name = raw
    if name.lower().endswith(".cube"):
        name = os.path.splitext(os.path.basename(name))[0]

    if name in BUILTIN_LOOKS:
        return ensure_lut(name)

    if os.path.exists(raw):
        if not is_trusted_lut_path(raw):
            raise ValidationError(
                "LUT files must live in a trusted LUT directory",
                details={"allowed": [str(directory) for directory in trusted_lut_dirs()]},
            )
        try:
            validate_lut(raw)
        except ValueError as exc:
            raise ValidationError(f"invalid LUT file: {exc}") from exc
        return os.path.abspath(raw)

    raise ValidationError(
        f"unknown LUT {reference!r}",
        details={"built_ins": sorted(BUILTIN_LOOKS)},
    )


__all__ = [
    "BUILTIN_LOOKS",
    "ensure_lut",
    "is_trusted_lut_path",
    "lut_fingerprint",
    "resolve_lut",
    "trusted_lut_dirs",
    "validate_lut",
    "write_lut",
]
