"""Thin, dependency-free wrappers around the ffmpeg / ffprobe binaries.

Every stage goes through this module so there is exactly one place that knows
how to shell out, capture stderr, and parse probe output. Keeping it dumb is
deliberate: FFmpeg's filter graph is the actual post-production logic and we
want it visible in the stage files, not hidden behind an abstraction.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class FFmpegError(RuntimeError):
    """Raised when ffmpeg exits non-zero. Carries the tail of stderr."""


def ffmpeg_bin() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def ffprobe_bin() -> str:
    return shutil.which("ffprobe") or "ffprobe"


def has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


# --------------------------------------------------------------------------- #
# Encoders — libx264 by default, NVENC when the box has it
# --------------------------------------------------------------------------- #
_ENCODERS: Optional[set] = None

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
    "h265_nvenc": "hevc_nvenc",
    "x265": "libx265",
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
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
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


def choose_encoder(options: Optional[Dict] = None) -> str:
    """Resolve ``options["encoder"]`` to a concrete FFmpeg encoder."""
    alias = _ALIASES.get(str((options or {}).get("encoder", "auto")).lower())
    if alias is None:
        raise ValueError(
            f"unknown encoder {options.get('encoder')!r}; "
            "use auto|libx264|libx265|nvenc|h264_nvenc|hevc_nvenc"
        )
    if alias == "auto":
        return "h264_nvenc" if has_encoder("h264_nvenc") else "libx264"
    if alias.endswith("_nvenc") and not has_encoder(alias):
        raise RuntimeError(
            f"encoder {alias} requested but FFmpeg reports no NVENC support; "
            "use encoder=auto or encoder=libx264, or install an NVENC-enabled FFmpeg"
        )
    if alias in {"libx264", "libx265"} and not has_encoder(alias):
        raise RuntimeError(f"encoder {alias} not available in this FFmpeg build")
    return alias


def video_encode_args(
    options: Optional[Dict] = None,
    crf: int = 18,
    preset: str = "veryfast",
    encoder: Optional[str] = None,
) -> tuple:
    """Return ``(ffmpeg_args, encoder_name)`` for a video stream.

    NVENC is chosen last-minute in ``auto`` mode, so the same job definition
    runs on a CPU laptop and a GPU box. Manifest metadata should record the
    second element.
    """
    name = encoder or choose_encoder(options)
    if name == "libx264":
        args = ["-c:v", "libx264", "-crf", str(crf), "-preset", preset, "-pix_fmt", "yuv420p"]
    elif name == "libx265":
        args = [
            "-c:v", "libx265", "-crf", str(crf), "-preset", preset,
            "-pix_fmt", "yuv420p", "-tag:v", "hvc1",
        ]
    elif name in {"h264_nvenc", "hevc_nvenc"}:
        nv_preset = _NVENC_PRESETS.get(preset, "p4")
        args = [
            "-c:v", name, "-preset", nv_preset,
            "-rc", "vbr", "-cq", str(crf), "-b:v", "0",
            "-pix_fmt", "yuv420p",
        ]
        if name == "hevc_nvenc":
            args += ["-tag:v", "hvc1"]
    else:  # pragma: no cover - choose_encoder guards this
        raise ValueError(f"unsupported encoder {name}")
    return args, name


def run(args: List[str], capture: bool = True) -> str:
    """Run a command, raise FFmpegError on failure, return stderr tail."""
    proc = subprocess.run(
        args,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.PIPE if capture else subprocess.DEVNULL,
        text=True,
    )
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-12:])
        raise FFmpegError(f"{args[0]} failed ({proc.returncode}):\n{tail}")
    return proc.stderr or ""


@dataclass
class MediaInfo:
    path: str
    duration_s: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    has_video: bool = False
    has_audio: bool = False
    v_codec: str = ""
    a_codec: str = ""
    size_bytes: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def aspect(self) -> str:
        if not self.height:
            return "?"
        return f"{self.width/self.height:.3f}"


def _parse_fps(value: str) -> float:
    try:
        if "/" in value:
            num, den = value.split("/", 1)
            den_f = float(den)
            return float(num) / den_f if den_f else 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def probe(path: str) -> MediaInfo:
    """Probe a media file. Returns zeros rather than raising when ffprobe is
    missing so the rest of the pipeline can still produce a readable error."""
    args = [
        ffprobe_bin(), "-v", "error",
        "-show_entries",
        "format=duration,size:stream=index,codec_type,codec_name,width,height,"
        "avg_frame_rate,r_frame_rate,duration",
        "-of", "json", path,
    ]
    try:
        out = subprocess.run(args, capture_output=True, text=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        info = MediaInfo(path=path)
        try:
            import os

            info.size_bytes = os.path.getsize(path)
        except OSError:
            pass
        return info

    data = json.loads(out or "{}")
    info = MediaInfo(path=path, raw=data)
    fmt = data.get("format", {})
    try:
        info.duration_s = float(fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        pass
    try:
        info.size_bytes = int(fmt.get("size") or 0)
    except (TypeError, ValueError):
        pass

    for stream in data.get("streams", []):
        kind = stream.get("codec_type")
        if kind == "video" and not info.has_video:
            info.has_video = True
            info.width = int(stream.get("width") or 0)
            info.height = int(stream.get("height") or 0)
            info.v_codec = stream.get("codec_name") or ""
            info.fps = _parse_fps(
                stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "0"
            )
            if not info.duration_s:
                try:
                    info.duration_s = float(stream.get("duration") or 0.0)
                except (TypeError, ValueError):
                    pass
        elif kind == "audio" and not info.has_audio:
            info.has_audio = True
            info.a_codec = stream.get("codec_name") or ""
    return info


def extract_audio(video: str, out_wav: str) -> None:
    run([
        ffmpeg_bin(), "-y", "-i", video,
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", out_wav,
    ])


def mux(video: str, audio: str, out_path: str) -> None:
    run([
        ffmpeg_bin(), "-y", "-i", video, "-i", audio,
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-shortest", out_path,
    ])


def copy_stream(src: str, dst: str) -> None:
    """Container-level remux with no re-encode (also used as a safe fallback)."""
    run([ffmpeg_bin(), "-y", "-i", src, "-c", "copy", dst])


def frame_at(video: str, seconds: float, out_jpg: str, width: int = 640) -> None:
    run([
        ffmpeg_bin(), "-y", "-ss", f"{max(0.0, seconds):.3f}", "-i", video,
        "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "3", out_jpg,
    ])
