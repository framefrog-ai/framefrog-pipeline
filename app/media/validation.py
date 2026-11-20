# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Media validation.

Every input is inspected with FFprobe *before* any processing starts and
checked against the configured limits. A file that violates a limit is
rejected with a specific reason — the job never starts and then fails halfway
through a long FFmpeg run.
"""

from __future__ import annotations

import os
from pathlib import Path

from app.core.config import Settings
from app.core.errors import MediaValidationError
from app.media.ffprobe import FFprobeError, MediaInfo, probe


def _human_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def validate_media_file(
    path: str | Path,
    settings: Settings,
    *,
    ffprobe_path: str | None = None,
) -> MediaInfo:
    """Validate a local media file against the configured limits.

    Returns the probed :class:`MediaInfo` when the file is acceptable, and
    raises :class:`MediaValidationError` with a precise reason otherwise.
    """
    file_path = Path(path)
    if not file_path.exists():
        raise MediaValidationError("input file does not exist", details={"path": str(file_path)})
    if not file_path.is_file():
        raise MediaValidationError("input path is not a regular file", details={"path": str(file_path)})

    size = file_path.stat().st_size
    if size <= 0:
        raise MediaValidationError("input file is empty", details={"path": str(file_path)})
    if size > settings.max_upload_bytes:
        raise MediaValidationError(
            "input file exceeds the configured size limit",
            details={
                "size_bytes": size,
                "limit_bytes": settings.max_upload_bytes,
                "size_human": _human_bytes(size),
            },
        )

    try:
        info = probe(str(file_path), ffprobe_path=ffprobe_path or settings.ffprobe_path)
    except FFprobeError as exc:
        raise MediaValidationError(
            "input file could not be probed",
            details={"reason": str(exc)[:400]},
        ) from exc

    if not info.has_video:
        raise MediaValidationError(
            "input has no video stream",
            details={"path": str(file_path)},
        )
    if info.duration_s <= 0:
        raise MediaValidationError(
            "input duration could not be determined",
            details={"path": str(file_path)},
        )
    if info.duration_s > settings.max_video_duration_seconds:
        raise MediaValidationError(
            "input exceeds the configured duration limit",
            details={
                "duration_s": round(info.duration_s, 3),
                "limit_s": settings.max_video_duration_seconds,
            },
        )
    if info.width > settings.max_video_width or info.height > settings.max_video_height:
        raise MediaValidationError(
            "input resolution exceeds the configured limit",
            details={
                "width": info.width,
                "height": info.height,
                "max_width": settings.max_video_width,
                "max_height": settings.max_video_height,
            },
        )
    if info.fps <= 0:
        raise MediaValidationError(
            "input frame rate could not be determined",
            details={"path": str(file_path)},
        )
    if info.fps > settings.max_output_fps + 1e-6:
        raise MediaValidationError(
            "input frame rate exceeds the configured limit",
            details={
                "fps": round(info.fps, 3),
                "max_fps": settings.max_output_fps,
            },
        )
    return info


def collect_violations(info: MediaInfo, settings: Settings) -> list[str]:
    """Return every limit violation of an already-probed file."""
    problems: list[str] = []
    if not info.has_video:
        problems.append("no video stream")
    if info.duration_s > settings.max_video_duration_seconds:
        problems.append("duration limit exceeded")
    if info.width > settings.max_video_width or info.height > settings.max_video_height:
        problems.append("resolution limit exceeded")
    if info.fps > settings.max_output_fps + 1e-6:
        problems.append("frame rate limit exceeded")
    return problems


def file_sha256(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    """SHA-256 of a file (used for output checksums)."""
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def assert_output_exists(path: str | Path) -> int:
    """Raise unless the artifact exists and is not empty; return its size."""
    file_path = Path(path)
    if not file_path.exists():
        raise MediaValidationError(f"expected artifact is missing: {file_path}")
    size = file_path.stat().st_size
    if size <= 0:
        raise MediaValidationError(f"artifact is empty: {file_path}")
    return size


def ensure_writable_dir(path: str | Path) -> Path:
    """Create (if needed) and return a writable directory."""
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def describe_file(path: str | Path) -> dict:
    """Small file description used in manifests."""
    file_path = Path(path)
    try:
        stat = file_path.stat()
    except OSError:
        return {"path": str(file_path), "exists": False}
    return {
        "path": str(file_path),
        "exists": True,
        "size_bytes": stat.st_size,
        "size_human": _human_bytes(stat.st_size),
        "modified_at": stat.st_mtime,
    }


def absolute_path(path: str | Path) -> str:
    """Absolute path string (never used for user-facing error messages)."""
    return os.path.abspath(str(path))
