# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Thin, dependency-free wrappers around the ffmpeg / ffprobe binaries.

Every stage goes through this module so there is exactly one place that knows
how to shell out, capture stderr and parse probe output. FFmpeg's filter
graph is the actual post-production logic and we want it visible in the stage
files, not hidden behind an abstraction.

Safety rules enforced here:

* no ``shell=True`` — commands are always argument lists
* every command has an explicit timeout
* output files are checked for existence and non-zero size
* failures raise a structured error that carries the stderr tail
* commands are logged in a safe, redacted representation
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
from collections.abc import Sequence

from app.media.ffprobe import FFprobeError, MediaInfo, parse_rational, probe

DEFAULT_TIMEOUT = 1800


class FFmpegError(RuntimeError):
    """Raised when ffmpeg exits non-zero. Carries the tail of stderr."""

    def __init__(self, message: str, *, command: Sequence[str] | None = None, stderr_tail: str = "") -> None:
        super().__init__(message)
        self.command = list(command or [])
        self.stderr_tail = stderr_tail


def ffmpeg_bin(ffmpeg_path: str = "ffmpeg") -> str:
    return shutil.which(ffmpeg_path) or ffmpeg_path


def ffprobe_binary(ffprobe_path: str = "ffprobe") -> str:
    return shutil.which(ffprobe_path) or ffprobe_path


def has_ffmpeg(ffmpeg_path: str = "ffmpeg", ffprobe_path: str = "ffprobe") -> bool:
    return shutil.which(ffmpeg_path) is not None and shutil.which(ffprobe_path) is not None


def safe_command(args: Sequence[str]) -> str:
    """A log-safe representation of a command line.

    Credentials in query strings (presigned URLs, tokens) are replaced with
    ``***``; long arguments are truncated.
    """
    import re

    sensitive = (
        "X-Amz-Signature",
        "X-Amz-Credential",
        "X-Amz-Security-Token",
        "token",
        "signature",
        "sig",
        "password",
        "secret",
    )
    pattern = re.compile(
        r"(" + "|".join(re.escape(name) for name in sensitive) + r")=([^&\s]+)",
        re.IGNORECASE,
    )
    parts = []
    for arg in args:
        text = str(arg)
        text = pattern.sub(r"\1=***", text)
        parts.append(text if len(text) <= 200 else text[:197] + "...")
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# Encoders — libx264 by default, NVENC when the box has it
# --------------------------------------------------------------------------- #
_ENCODERS: set | None = None

_ALIASES = {
    "": "auto",
    "auto": "auto",
    "cpu": "libx264",
    "x264": "libx264",
    "libx264": "libx264",
    "nvenc": "h264_nvenc",
    "gpu": "h264_nvenc",
    "h264_nvenc": "h264_nvenc",
    "hevc_nvenc": "hevc_nvenc",
    "h265": "libx265",
    "libx265": "libx265",
}

#: Approximate NVENC preset mapping from x264's names.
_NVENC_PRESETS = {"ultrafast": "p1", "veryfast": "p3", "fast": "p4", "medium": "p5", "slow": "p6"}


def available_encoders() -> set:
    """Names advertised by ``ffmpeg -encoders`` (cached per process)."""
    global _ENCODERS
    if _ENCODERS is not None:
        return _ENCODERS
    names = set()
    try:
        proc = subprocess.run(
            [ffmpeg_bin(), "-hide_banner", "-encoders"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=30,
        )
        text = proc.stdout or ""
    except Exception:
        text = ""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0][:1] in {"V", "A", "S"}:
            names.add(parts[1])
    _ENCODERS = names
    return names


def has_encoder(name: str) -> bool:
    return name in available_encoders()


def has_nvidia_device() -> bool:
    """Whether a usable NVIDIA device is actually present.

    FFmpeg builds on CI advertise NVENC without a driver, and using it fails at
    runtime with ``Cannot load libcuda.so.1``. Auto selection therefore checks
    for device nodes too. Container users whose nodes live elsewhere can force
    it with ``FRAMEFROG_FORCE_NVENC=1`` or disable with
    ``FRAMEFROG_DISABLE_NVENC=1``.
    """
    if os.environ.get("FRAMEFROG_DISABLE_NVENC", "").lower() in {"1", "true", "yes"}:
        return False
    if os.environ.get("FRAMEFROG_FORCE_NVENC", "").lower() in {"1", "true", "yes"}:
        return True
    return bool(glob.glob("/dev/nvidia[0-9]*"))


def choose_encoder(options: dict | None = None) -> str:
    """Resolve ``options["encoder"]`` to a concrete FFmpeg encoder."""
    alias = _ALIASES.get(str((options or {}).get("encoder", "auto")).lower())
    if alias is None:
        raise ValueError(
            f"unknown encoder {(options or {}).get('encoder')!r}; "
            "use auto|libx264|libx265|nvenc|h264_nvenc|hevc_nvenc"
        )
    if alias == "auto":
        usable = has_encoder("h264_nvenc") and has_nvidia_device()
        return "h264_nvenc" if usable else "libx264"
    if alias.endswith("_nvenc"):
        if not has_encoder(alias):
            raise RuntimeError(
                f"encoder {alias} requested but FFmpeg reports no NVENC support; "
                "use encoder=auto or encoder=libx264, or install an NVENC-enabled FFmpeg"
            )
        if not has_nvidia_device():
            raise RuntimeError(
                f"encoder {alias} exists in FFmpeg but no NVIDIA device/libcuda is "
                "available; use encoder=auto or set FRAMEFROG_FORCE_NVENC=1 only if "
                "the device is visible to this process"
            )
    if alias in {"libx264", "libx265"} and not has_encoder(alias):
        raise RuntimeError(f"encoder {alias} not available in this FFmpeg build")
    return alias


def video_encode_args(
    options: dict | None = None,
    crf: int = 18,
    preset: str = "veryfast",
    encoder: str | None = None,
) -> tuple:
    """Return ``(ffmpeg_args, encoder_name)`` for a video stream.

    NVENC is chosen last-minute in ``auto`` mode, so the same job definition
    runs on a CPU laptop and a GPU box. Manifest metadata records the second
    element so a reader can tell which encoder actually ran.
    """
    name = encoder or choose_encoder(options)
    if name == "libx264":
        args = ["-c:v", "libx264", "-crf", str(crf), "-preset", preset, "-pix_fmt", "yuv420p"]
    elif name == "libx265":
        args = [
            "-c:v",
            "libx265",
            "-crf",
            str(crf),
            "-preset",
            preset,
            "-pix_fmt",
            "yuv420p",
            "-tag:v",
            "hvc1",
        ]
    elif name in {"h264_nvenc", "hevc_nvenc"}:
        nv_preset = _NVENC_PRESETS.get(preset, "p4")
        args = [
            "-c:v",
            name,
            "-preset",
            nv_preset,
            "-rc",
            "vbr",
            "-cq",
            str(crf),
            "-b:v",
            "0",
            "-pix_fmt",
            "yuv420p",
        ]
        if name == "hevc_nvenc":
            args += ["-tag:v", "hvc1"]
    else:  # pragma: no cover - choose_encoder guards this
        raise ValueError(f"unsupported encoder {name}")
    return args, name


def run(args: list[str], capture: bool = True, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Run a command, raise :class:`FFmpegError` on failure, return stderr."""
    try:
        proc = subprocess.run(
            args,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.PIPE if capture else subprocess.DEVNULL,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(
            f"{args[0]} timed out after {timeout}s",
            command=args,
            stderr_tail=str(exc),
        ) from exc
    except FileNotFoundError as exc:
        raise FFmpegError(
            f"binary not found: {args[0]}",
            command=args,
        ) from exc
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-12:])
        raise FFmpegError(
            f"{args[0]} failed ({proc.returncode}):\n{tail}",
            command=args,
            stderr_tail=tail,
        )
    return proc.stderr or ""


def run_to_file(args: list[str], output_path: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Run FFmpeg and verify that ``output_path`` exists and is not empty."""
    stderr = run(args, timeout=timeout)
    if not os.path.exists(output_path):
        raise FFmpegError(
            f"expected output was not created: {output_path}",
            command=args,
            stderr_tail=stderr[-2000:],
        )
    try:
        size = os.path.getsize(output_path)
    except OSError as exc:
        raise FFmpegError(f"cannot stat output {output_path}: {exc}", command=args) from exc
    if size <= 0:
        raise FFmpegError(f"output file is empty: {output_path}", command=args)
    return stderr


def probe_media(path: str, ffprobe_path: str = "ffprobe") -> MediaInfo:
    """Probe a media file (thin wrapper kept for stage compatibility)."""
    return probe(path, ffprobe_path=ffprobe_path)


# Backwards-compatible alias used by the existing stages.
def probe_legacy(path: str) -> MediaInfo:  # pragma: no cover - compatibility shim
    return probe(path)


def extract_audio(video: str, out_wav: str, sample_rate: int = 16000, channels: int = 1) -> None:
    run(
        [
            ffmpeg_bin(),
            "-y",
            "-i",
            video,
            "-vn",
            "-ac",
            str(channels),
            "-ar",
            str(sample_rate),
            "-c:a",
            "pcm_s16le",
            out_wav,
        ]
    )


def mux(video: str, audio: str, out_path: str) -> None:
    run(
        [
            ffmpeg_bin(),
            "-y",
            "-i",
            video,
            "-i",
            audio,
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            out_path,
        ]
    )


def copy_stream(src: str, dst: str) -> None:
    """Container-level remux with no re-encode (also used as a safe fallback)."""
    run([ffmpeg_bin(), "-y", "-i", src, "-c", "copy", dst])


def frame_at(video: str, seconds: float, out_jpg: str, width: int = 640) -> None:
    run(
        [
            ffmpeg_bin(),
            "-y",
            "-ss",
            f"{max(0.0, seconds):.3f}",
            "-i",
            video,
            "-frames:v",
            "1",
            "-vf",
            f"scale={width}:-2",
            "-q:v",
            "3",
            out_jpg,
        ]
    )


__all__ = [
    "FFmpegError",
    "FFprobeError",
    "MediaInfo",
    "available_encoders",
    "choose_encoder",
    "copy_stream",
    "extract_audio",
    "ffmpeg_bin",
    "ffprobe_binary",
    "frame_at",
    "has_encoder",
    "has_ffmpeg",
    "has_nvidia_device",
    "mux",
    "parse_rational",
    "probe",
    "probe_media",
    "run",
    "run_to_file",
    "safe_command",
    "video_encode_args",
]
