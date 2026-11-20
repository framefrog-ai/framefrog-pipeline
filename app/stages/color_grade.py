# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Stage 3 — AI look matching and 3D LUT grading.

The "AI" part lives upstream (a look suggestion service picks a LUT + strength
for the brief); this stage is the deterministic execution: apply a .cube LUT
with FFmpeg's ``lut3d`` filter, optionally blended at a strength so the DP can
dial the look back.

Options
-------
``lut`` (str, default "cinematic_warm")
    Built-in look name or path to a .cube file.
``look_strength`` (float, 0..1, default 1.0)
    Blend between source and graded image.
``lut_dir`` — unused; use FRAMEFROG_LUT_DIR env if you need a custom cache.
"""

from __future__ import annotations

import os

from app.media import ffmpeg
from app.media.lut import lut_fingerprint
from app.media.lut_resolver import resolve_lut
from app.stages.base import StageResult


def run(input_path: str, workdir: str, options: dict) -> StageResult:
    out = os.path.join(workdir, "03_graded.mp4")
    lut_path = resolve_lut(str(options.get("lut", "cinematic_warm")))
    strength = float(options.get("look_strength", 1.0))
    strength = min(1.0, max(0.0, strength))

    if not ffmpeg.has_ffmpeg():
        # Last-resort fallback so the stage is not a hard failure on dev boxes
        # without FFmpeg: stream copy and record that grading was skipped.
        import shutil

        shutil.copyfile(input_path, out)
        return StageResult(
            "color_grade",
            out,
            {"lut": os.path.basename(lut_path), "applied": False, "reason": "ffmpeg missing"},
        )

    lut_arg = lut_path.replace("\\", "/").replace(":", "\\:")
    base = ffmpeg.ffmpeg_bin()
    enc_args, encoder = ffmpeg.video_encode_args(options, crf=18, preset="veryfast")
    if strength >= 0.999:
        cmd = [
            base,
            "-y",
            "-i",
            input_path,
            "-vf",
            f"lut3d=file='{lut_arg}':interp=trilinear",
            *enc_args,
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            out,
        ]
    else:
        graph = (
            f"[0:v]split=2[src][t];"
            f"[t]lut3d=file='{lut_arg}':interp=trilinear[g];"
            f"[src][g]blend=all_mode=normal:all_opacity={strength:.3f}[v]"
        )
        cmd = [
            base,
            "-y",
            "-i",
            input_path,
            "-filter_complex",
            graph,
            "-map",
            "[v]",
            "-map",
            "0:a?",
            *enc_args,
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            out,
        ]
    ffmpeg.run(cmd)

    return StageResult(
        "color_grade",
        out,
        {
            "lut": os.path.basename(lut_path),
            "applied": True,
            "strength": round(strength, 3),
            "lut_fingerprint": lut_fingerprint(lut_path),
            "encoder": encoder,
        },
    )
