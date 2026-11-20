# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""FFprobe integration: real media inspection.

Only FFprobe is used to learn about a file — nothing is guessed from the file
name or the container extension.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from typing import Any

DEFAULT_TIMEOUT = 120


class FFprobeError(RuntimeError):
    """Raised when ffprobe cannot be executed or returns unusable output."""


@dataclass
class MediaInfo:
    """Everything the pipeline knows about a media file."""

    path: str
    duration_s: float = 0.0
    container: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    fps_rational: str = ""
    video_codec: str = ""
    pix_fmt: str = ""
    has_video: bool = False
    has_audio: bool = False
    audio_codec: str = ""
    audio_sample_rate: int = 0
    audio_channels: int = 0
    size_bytes: int = 0
    bit_rate: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def aspect(self) -> str:
        if not self.height:
            return "?"
        return f"{self.width / self.height:.3f}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "duration_s": round(self.duration_s, 3),
            "container": self.container,
            "width": self.width,
            "height": self.height,
            "fps": round(self.fps, 3),
            "fps_rational": self.fps_rational,
            "video_codec": self.video_codec,
            "pix_fmt": self.pix_fmt,
            "has_video": self.has_video,
            "has_audio": self.has_audio,
            "audio_codec": self.audio_codec,
            "audio_sample_rate": self.audio_sample_rate,
            "audio_channels": self.audio_channels,
            "size_bytes": self.size_bytes,
            "bit_rate": self.bit_rate,
        }


def parse_rational(value: str | None) -> float:
    """Parse an FFmpeg rational such as ``30000/1001`` into a float."""
    if not value:
        return 0.0
    text = str(value).strip()
    if text in {"0/0", "N/A", ""}:
        return 0.0
    try:
        if "/" in text:
            num, _, den = text.partition("/")
            denominator = float(den)
            if denominator == 0:
                return 0.0
            return float(num) / denominator
        return float(text)
    except (TypeError, ValueError):
        return 0.0


def ffprobe_bin(ffprobe_path: str = "ffprobe") -> str:
    import shutil

    return shutil.which(ffprobe_path) or ffprobe_path


def probe(path: str, ffprobe_path: str = "ffprobe", timeout: int = DEFAULT_TIMEOUT) -> MediaInfo:
    """Inspect ``path`` with FFprobe and return a :class:`MediaInfo`.

    Raises :class:`FFprobeError` when the binary is missing or the file cannot
    be read — callers decide whether that is fatal.
    """
    import os

    args = [
        ffprobe_bin(ffprobe_path),
        "-v",
        "error",
        "-show_entries",
        (
            "format=duration,size,format_name,bit_rate:"
            "stream=index,codec_type,codec_name,width,height,avg_frame_rate,"
            "r_frame_rate,duration,pix_fmt,sample_rate,channels"
        ),
        "-of",
        "json",
        path,
    ]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise FFprobeError(f"ffprobe binary not found: {ffprobe_path}") from exc
    except subprocess.TimeoutExpired as exc:
        raise FFprobeError(f"ffprobe timed out after {timeout}s") from exc

    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-6:])
        raise FFprobeError(f"ffprobe failed ({proc.returncode}): {tail}")

    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise FFprobeError(f"ffprobe returned invalid JSON: {exc}") from exc

    info = MediaInfo(path=path, raw=data)
    fmt = data.get("format", {}) or {}
    info.container = str(fmt.get("format_name") or "")
    try:
        info.duration_s = float(fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        info.duration_s = 0.0
    try:
        info.size_bytes = int(fmt.get("size") or 0)
    except (TypeError, ValueError):
        info.size_bytes = 0
    try:
        info.bit_rate = int(fmt.get("bit_rate") or 0)
    except (TypeError, ValueError):
        info.bit_rate = 0

    for stream in data.get("streams", []) or []:
        kind = stream.get("codec_type")
        if kind == "video" and not info.has_video:
            info.has_video = True
            info.width = int(stream.get("width") or 0)
            info.height = int(stream.get("height") or 0)
            info.video_codec = str(stream.get("codec_name") or "")
            info.pix_fmt = str(stream.get("pix_fmt") or "")
            rational = stream.get("avg_frame_rate") or stream.get("r_frame_rate") or ""
            info.fps_rational = str(rational)
            info.fps = parse_rational(rational)
            if not info.duration_s:
                try:
                    info.duration_s = float(stream.get("duration") or 0.0)
                except (TypeError, ValueError):
                    info.duration_s = 0.0
        elif kind == "audio" and not info.has_audio:
            info.has_audio = True
            info.audio_codec = str(stream.get("codec_name") or "")
            try:
                info.audio_sample_rate = int(stream.get("sample_rate") or 0)
            except (TypeError, ValueError):
                info.audio_sample_rate = 0
            try:
                info.audio_channels = int(stream.get("channels") or 0)
            except (TypeError, ValueError):
                info.audio_channels = 0

    if not info.size_bytes:
        try:
            info.size_bytes = os.path.getsize(path)
        except OSError:
            info.size_bytes = 0

    return info
