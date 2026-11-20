#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Generate synthetic test media with FFmpeg.

    python scripts/generate_test_video.py                      # default sample
    python scripts/generate_test_video.py --output /tmp/a.mp4 --duration 8
    python scripts/generate_test_video.py --scenes 3 --no-audio

The clip is built from FFmpeg's own test sources (``testsrc2`` and
``smptebars``) concatenated with a sine tone, so it contains real hard cuts
for the scene detector to find and never depends on downloaded footage.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DEFAULT_OUTPUT = os.path.join(ROOT, "assets", "sample", "demo_source.mp4")

#: Visual sources cycled through to create scene cuts.
SOURCES = ("testsrc2", "smptebars", "rgbtestsrc", "mandelbrot")


def build_command(
    output: str,
    duration: float,
    scenes: int,
    fps: int,
    width: int,
    height: int,
    with_audio: bool,
) -> list:
    scene_len = max(0.5, duration / max(1, scenes))
    args = [os.environ.get("FFMPEG_PATH", "ffmpeg"), "-y"]
    for index in range(scenes):
        source = SOURCES[index % len(SOURCES)]
        args += [
            "-f",
            "lavfi",
            "-i",
            f"{source}=size={width}x{height}:rate={fps}:duration={scene_len:.3f}",
        ]
    if with_audio:
        args += [
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate=48000:duration={duration:.3f}",
        ]
    concat = "".join(f"[{i}:v]" for i in range(scenes))
    graph = f"{concat}concat=n={scenes}:v=1:a=0[v]"
    args += ["-filter_complex", graph]
    maps = ["-map", "[v]"]
    if with_audio:
        graph += f";[{scenes}:a]volume=0.4[a]"
        args[args.index("-filter_complex") + 1] = graph
        maps += ["-map", "[a]"]
    args += maps
    args += [
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-g",
        str(fps * 2),
    ]
    if with_audio:
        args += ["-c:a", "aac", "-b:a", "128k"]
    else:
        args += ["-an"]
    args += ["-movflags", "+faststart", output]
    return args


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="generate synthetic test media")
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--duration", type=float, default=6.0)
    parser.add_argument("--scenes", type=int, default=2)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=360)
    parser.add_argument("--no-audio", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    output = os.path.abspath(args.output)
    os.makedirs(os.path.dirname(output), exist_ok=True)

    from app.media import ffmpeg

    command = build_command(
        output,
        args.duration,
        max(1, args.scenes),
        args.fps,
        args.width,
        args.height,
        not args.no_audio,
    )
    started = time.time()
    ffmpeg.run_to_file(command, output)
    elapsed = round(time.time() - started, 3)

    info = ffmpeg.probe(output)
    summary = {
        "path": output,
        "size_bytes": os.path.getsize(output),
        "duration_s": round(info.duration_s, 3),
        "width": info.width,
        "height": info.height,
        "fps": round(info.fps, 3),
        "video_codec": info.video_codec,
        "audio_codec": info.audio_codec,
        "has_audio": info.has_audio,
        "elapsed_s": elapsed,
    }
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"Generated {output}")
        print(
            f"  {summary['width']}x{summary['height']} @ {summary['fps']} fps · "
            f"{summary['duration_s']} s · {summary['size_bytes'] / 1e6:.2f} MB"
        )
        print(f"  video: {summary['video_codec']} · audio: {summary['audio_codec'] or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
