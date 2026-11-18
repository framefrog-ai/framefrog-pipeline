"""Stage 1 — audio cleanup.

Denoise, de-hum and loudness-normalise the production audio without touching
the picture: video is stream-copied, only the audio is re-encoded.

The filter chain is intentionally conservative. Broadcast-safe delivery is
-16 LUFS / -1.5 dBTP for web; options can raise it for cinema or social.

Options
-------
``lufs`` (float, default -16.0)
    Integrated loudness target.
``denoise`` (bool, default True)
    Enable FFT denoise (``afftdn``).
``highpass`` / ``lowpass`` (int, default 80 / 14000)
    Band-limit rumble and hiss before denoise.
"""

from __future__ import annotations

import os
import shutil

from framefrog import ffmpeg
from framefrog.types import StageResult


def run(input_path: str, workdir: str, options: dict) -> StageResult:
    info = ffmpeg.probe(input_path)
    out = os.path.join(workdir, "01_audio_clean.mp4")

    if not info.has_audio:
        # Nothing to clean; pass the picture through untouched.
        shutil.copyfile(input_path, out)
        return StageResult(
            "audio_cleanup",
            out,
            {"audio": False, "reason": "no audio stream"},
        )

    lufs = float(options.get("lufs", -16.0))
    highpass = int(options.get("highpass", 80))
    lowpass = int(options.get("lowpass", 14000))
    denoise = bool(options.get("denoise", True))

    chain = [f"highpass=f={highpass}", f"lowpass=f={lowpass}"]
    if denoise:
        chain.append("afftdn=nr=12:nf=-25")
    chain.append(f"loudnorm=I={lufs}:TP=-1.5:LRA=11")

    cmd = [
        ffmpeg.ffmpeg_bin(), "-y", "-i", input_path,
        "-map", "0:v:0?", "-map", "0:a:0",
        "-c:v", "copy",
        "-af", ",".join(chain),
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        out,
    ]
    ffmpeg.run(cmd)
    return StageResult(
        "audio_cleanup",
        out,
        {
            "audio": True,
            "lufs_target": lufs,
            "filters": chain,
            "source_audio_codec": info.a_codec,
        },
    )
