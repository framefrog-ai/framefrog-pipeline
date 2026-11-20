# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Stage 1 — audio cleanup.

Denoise, de-hum and loudness-normalise the production audio without touching
the picture: video is stream-copied, only the audio is re-encoded.

Two denoise backends:

* **DeepFilterNet3** (``pip install deepfilternet``) — neural speech/music
  enhancement at 48 kHz. Used when ``denoise_model`` is ``deepfilternet`` or
  ``auto`` and the package is installed.
* **FFmpeg** — high/low-pass + ``afftdn`` spectral denoise. Always available.

Either way the tail of the chain is the same: loudness-normalise to −16 LUFS
and mux back against the untouched picture.

Options
-------
``denoise_model`` (str, default "auto")
    ``auto`` | ``deepfilternet`` | ``ffmpeg``. Explicit ``deepfilternet`` raises
    if the package is missing; ``auto`` silently falls back and records why.
``lufs`` (float, default -16.0)
    Integrated loudness target.
``highpass`` / ``lowpass`` (int, default 80 / 14000)
    Band-limit rumble and hiss before denoise (FFmpeg backend).
"""

from __future__ import annotations

import os
import shutil

from app.media import ffmpeg
from app.models.discovery import deepfilternet_available
from app.stages.base import StageResult


def run(input_path: str, workdir: str, options: dict) -> StageResult:
    info = ffmpeg.probe(input_path)
    out = os.path.join(workdir, "01_audio_clean.mp4")

    if not info.has_audio:
        shutil.copyfile(input_path, out)
        return StageResult(
            "audio_cleanup",
            out,
            {"audio": False, "reason": "no audio stream", "denoise_model": None},
        )

    backend = str(options.get("denoise_model", "auto")).lower()
    lufs = float(options.get("lufs", -16.0))

    if backend not in {"auto", "deepfilternet", "ffmpeg"}:
        raise ValueError(f"unknown denoise_model {backend!r}; use auto|deepfilternet|ffmpeg")

    used = "ffmpeg"
    fallback_reason = None
    dfn_meta = {}

    want_dfn = backend in {"auto", "deepfilternet"}
    if want_dfn and deepfilternet_available():
        try:
            dfn_meta = _deepfilternet_denoise(input_path, workdir, lufs)
            used = "deepfilternet"
        except Exception as exc:  # never lose the audio because a model broke
            if backend == "deepfilternet":
                raise
            fallback_reason = f"{type(exc).__name__}: {exc}"
    elif backend == "deepfilternet":
        raise RuntimeError(
            "denoise_model=deepfilternet but the package is not installed; run `pip install deepfilternet`"
        )
    elif want_dfn:
        fallback_reason = "deepfilternet not installed"

    if used == "deepfilternet":
        return StageResult(
            "audio_cleanup",
            dfn_meta.pop("output"),
            {"audio": True, "denoise_model": used, "lufs_target": lufs, **dfn_meta},
        )

    # FFmpeg chain (also the fallback path)
    highpass = int(options.get("highpass", 80))
    lowpass = int(options.get("lowpass", 14000))
    denoise = bool(options.get("denoise", True))
    chain = [f"highpass=f={highpass}", f"lowpass=f={lowpass}"]
    if denoise:
        chain.append("afftdn=nr=12:nf=-25")
    chain.append(f"loudnorm=I={lufs}:TP=-1.5:LRA=11")

    ffmpeg.run(
        [
            ffmpeg.ffmpeg_bin(),
            "-y",
            "-i",
            input_path,
            "-map",
            "0:v:0?",
            "-map",
            "0:a:0",
            "-c:v",
            "copy",
            "-af",
            ",".join(chain),
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            out,
        ]
    )

    meta = {
        "audio": True,
        "denoise_model": used,
        "lufs_target": lufs,
        "filters": chain,
        "source_audio_codec": info.audio_codec,
    }
    if fallback_reason:
        meta["fallback_reason"] = fallback_reason
    return StageResult("audio_cleanup", out, meta)


# --------------------------------------------------------------------------- #
# DeepFilterNet backend
# --------------------------------------------------------------------------- #
_DFN: dict = {}


def _dfn_runtime():
    """Load DeepFilterNet3 once per process and reset its DSP state per job."""
    if "model" not in _DFN:
        from df.enhance import init_df  # type: ignore

        model, state, suffix = init_df()
        _DFN.update(model=model, state=state, suffix=suffix)
    state = _DFN["state"]
    reset = getattr(state, "reset", None)
    if callable(reset):
        reset()  # clear running ER/DF and normalization state between jobs
    return _DFN["model"], state


def _deepfilternet_denoise(video: str, workdir: str, lufs: float) -> dict:
    """Enhance the audio with DeepFilterNet3, then loudnorm and mux.

    The model is 48 kHz; we extract mono 48 kHz PCM, enhance, mux against a
    stream-copied picture, then run the same loudnorm tail as the FFmpeg path.
    """
    from df.enhance import enhance, load_audio, save_audio  # type: ignore

    raw_wav = os.path.join(workdir, "01_audio_raw_48k.wav")
    clean_wav = os.path.join(workdir, "01_audio_dfn_48k.wav")
    muxed = os.path.join(workdir, "01_audio_dfn_muxed.mp4")
    out = os.path.join(workdir, "01_audio_clean.mp4")

    ffmpeg.run(
        [
            ffmpeg.ffmpeg_bin(),
            "-y",
            "-i",
            video,
            "-vn",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-c:a",
            "pcm_s16le",
            raw_wav,
        ]
    )

    model, state = _dfn_runtime()
    audio, _sr = load_audio(raw_wav, sr=state.sr())
    enhanced = enhance(model, state, audio)
    save_audio(clean_wav, enhanced, state.sr())

    ffmpeg.run(
        [
            ffmpeg.ffmpeg_bin(),
            "-y",
            "-i",
            video,
            "-i",
            clean_wav,
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
            "-movflags",
            "+faststart",
            muxed,
        ]
    )
    ffmpeg.run(
        [
            ffmpeg.ffmpeg_bin(),
            "-y",
            "-i",
            muxed,
            "-c:v",
            "copy",
            "-af",
            f"loudnorm=I={lufs}:TP=-1.5:LRA=11",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            out,
        ]
    )

    return {
        "output": out,
        "model": "DeepFilterNet3",
        "sample_rate": int(state.sr()),
        "loudnorm": True,
    }
