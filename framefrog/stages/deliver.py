"""Stage 5 — delivery.

Pack the finished picture into deliverables: a mezzanine master, a 720p proxy,
and a poster frame. Muxing loudness and formats here (rather than in every
stage) keeps the pipeline's contract simple: stages return one video, delivery
returns a bundle.
"""

from __future__ import annotations

import os

from framefrog import ffmpeg
from framefrog.types import StageResult


def run(input_path: str, workdir: str, options: dict) -> StageResult:
    info = ffmpeg.probe(input_path)
    master = os.path.join(workdir, "05_master.mp4")
    proxy = os.path.join(workdir, "05_proxy_720p.mp4")
    poster = os.path.join(workdir, "05_poster.jpg")
    crf = int(options.get("master_crf", 18))

    audio_args = ["-c:a", "aac", "-b:a", "192k"] if info.has_audio else ["-an"]
    master_enc, master_encoder = ffmpeg.video_encode_args(options, crf=crf, preset="medium")
    ffmpeg.run([
        ffmpeg.ffmpeg_bin(), "-y", "-i", input_path,
        *master_enc,
        *audio_args,
        "-movflags", "+faststart",
        master,
    ])

    proxy_enc, proxy_encoder = ffmpeg.video_encode_args(options, crf=26, preset="veryfast")
    ffmpeg.run([
        ffmpeg.ffmpeg_bin(), "-y", "-i", input_path,
        "-vf", "scale=-2:720",
        *proxy_enc,
        *audio_args,
        "-movflags", "+faststart",
        proxy,
    ])

    poster_at = max(0.0, min(info.duration_s * 0.1, 30.0))
    ffmpeg.frame_at(input_path, poster_at, poster, width=1280)

    out_info = ffmpeg.probe(master)
    return StageResult(
        "deliver",
        master,
        {
            "master": master,
            "proxy": proxy,
            "poster": poster,
            "width": out_info.width,
            "height": out_info.height,
            "fps": round(out_info.fps, 3),
            "duration_s": round(out_info.duration_s, 3),
            "size_bytes": out_info.size_bytes,
            "faststart": True,
            "encoders": {"master": master_encoder, "proxy": proxy_encoder},
        },
    )
