# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Stage 4 — frame interpolation.

Raises the frame rate to at most 60 fps. Two backends:

* **RIFE v4.9 (ONNX Runtime)** — real optical-flow interpolation. Used when
  ``interp_model`` is ``rife``/``auto`` and a graph is found
  (``python scripts/download_models.py``). CPU works; CUDA is used when the
  provider is available.
* **FFmpeg minterpolate** — blend (fast) or full motion-compensated (high).
  Always available and the fallback when RIFE is missing.

The RIFE path is scene-aware: frames are never blended across a hard cut,
because interpolating across a cut produces a ghost frame. The scene list is
passed in through ``options["scenes"]``.

Hard limits (initial release):

* output frame rate ≤ 60 fps
* RIFE input resolution ≤ 1920x1080

If the caller explicitly asks for the CUDA provider and no CUDA device is
present, the stage raises a capability error instead of silently falling back
to the CPU: a GPU job must not report success on the CPU.

Options
-------
``interp_model`` (str, default "auto")
    ``auto`` | ``rife`` | ``ffmpeg``. Explicit ``rife`` raises if no graph is
    found; ``auto`` falls back and records why.
``rife_onnx`` (str)
    Explicit path to a RIFE ONNX graph.
``rife_provider`` (str, default "auto")
    ONNX Runtime provider: ``auto`` | ``cpu`` | ``cuda``.
``target_fps`` (int, default 60)
``interp_quality`` (str, default "fast")
    FFmpeg fallback only: ``fast`` (blend) | ``high`` (mci + bidir).
``keep_frames`` (bool, default False)
    Keep the intermediate PNG sequence for debugging.
``scenes`` (list)
    Scene boundaries from the scene-detection stage, used to avoid
    interpolating across a cut.
"""

from __future__ import annotations

import math
import os
import shutil
from typing import Any

from app.core.errors import CapabilityError
from app.media import ffmpeg
from app.models.discovery import find_rife_model, onnxruntime_available
from app.stages.base import StageResult

_SESSIONS: dict[Any, Any] = {}
_PROVIDERS: list[str] | None = None

#: Initial output limit. 120 fps is explicitly out of scope for this release.
MAX_TARGET_FPS = 60
#: Initial resolution limit for the neural path.
MAX_INTERPOLATE_WIDTH = 1920
MAX_INTERPOLATE_HEIGHT = 1080
#: Frames processed per chunk before the intermediate buffer is flushed.
CHUNK_SIZE = 240


def run(input_path: str, workdir: str, options: dict) -> StageResult:
    out = os.path.join(workdir, "04_interpolated.mp4")
    target = int(options.get("target_fps", 60))
    if target <= 0:
        raise ValueError("target_fps must be positive")
    if target > MAX_TARGET_FPS:
        raise CapabilityError(
            f"target_fps {target} exceeds the supported maximum of {MAX_TARGET_FPS}",
            details={"target_fps": target, "max_target_fps": MAX_TARGET_FPS},
        )
    quality = str(options.get("interp_quality", "fast")).lower()
    info = ffmpeg.probe(input_path)

    if info.fps and info.fps >= target - 0.5:
        shutil.copyfile(input_path, out)
        return StageResult(
            "interpolate",
            out,
            {
                "target_fps": target,
                "source_fps": round(info.fps, 3),
                "skipped": True,
                "reason": "source already at target rate",
                "gpu_validation_status": "pending",
            },
        )

    backend = str(options.get("interp_model", "auto")).lower()
    if backend not in {"auto", "rife", "ffmpeg"}:
        raise ValueError(f"unknown interp_model {backend!r}; use auto|rife|ffmpeg")

    provider = str(options.get("rife_provider", "auto")).lower()
    if provider not in {"auto", "cpu", "cuda"}:
        raise ValueError(f"unknown rife_provider {provider!r}; use auto|cpu|cuda")
    if provider == "cuda":
        # An explicit CUDA request must not silently run on the CPU.
        from app.core import capabilities

        if not capabilities.cuda_available():
            raise CapabilityError(
                "CUDA was requested for RIFE but no CUDA device is available",
                details={
                    "requested_provider": "cuda",
                    "hint": "run on a GPU host, or use device=cpu / device=auto",
                },
            )

    model_path = find_rife_model(options.get("rife_onnx"))
    have_rife = bool(model_path) and onnxruntime_available()
    if have_rife and model_path is None:  # pragma: no cover - defensive
        raise CapabilityError("RIFE model path could not be resolved")

    if backend == "rife" and not have_rife:
        reason = (
            "RIFE ONNX graph not found — run `python scripts/download_models.py`"
            if not model_path
            else "onnxruntime not installed — `pip install onnxruntime`"
        )
        raise CapabilityError(reason, details={"interp_model": "rife"})

    if backend in {"auto", "rife"} and have_rife and model_path is not None:
        try:
            meta = _rife_interpolate(input_path, out, target, workdir, model_path, options)
            return StageResult("interpolate", out, meta)
        except CapabilityError:
            raise
        except Exception as exc:
            if backend == "rife":
                raise
            fallback_reason = f"{type(exc).__name__}: {exc}"
    elif backend == "ffmpeg":
        fallback_reason = "interp_model=ffmpeg"
    else:
        fallback_reason = "RIFE ONNX graph not found" if not model_path else "onnxruntime not installed"

    meta = _ffmpeg_interpolate(input_path, out, target, quality, options)
    meta["fallback_reason"] = fallback_reason
    return StageResult("interpolate", out, meta)


# --------------------------------------------------------------------------- #
# RIFE backend
# --------------------------------------------------------------------------- #
def _available_providers() -> list[str]:
    """Providers this ONNX Runtime build actually offers (cached)."""
    global _PROVIDERS
    if _PROVIDERS is None:
        try:
            import onnxruntime as ort  # type: ignore

            _PROVIDERS = list(ort.get_available_providers())
        except Exception:  # pragma: no cover - onnxruntime missing
            _PROVIDERS = []
    return _PROVIDERS


def _provider(pref: str) -> list:
    """Provider list for an ONNX Runtime session.

    ``auto`` only asks for the CUDA provider when the installed runtime
    offers it — asking for a provider that is not compiled in produces a
    warning on every session.
    """
    if pref == "cpu":
        return ["CPUExecutionProvider"]
    if pref == "cuda":
        # The caller has already verified that a CUDA device exists.
        return ["CUDAExecutionProvider", "CPUExecutionProvider"]
    if "CUDAExecutionProvider" in _available_providers():
        return ["CUDAExecutionProvider", "CPUExecutionProvider"]
    return ["CPUExecutionProvider"]


def _session(model_path: str, provider: str):
    key = (os.path.abspath(model_path), provider)
    if key not in _SESSIONS:
        try:
            import onnxruntime as ort
        except ImportError as exc:  # pragma: no cover - guarded by the caller
            raise CapabilityError(
                "onnxruntime is not installed",
                details={"install": "pip install onnxruntime"},
            ) from exc
        _SESSIONS[key] = ort.InferenceSession(model_path, providers=_provider(provider))
    return _SESSIONS[key]


def _cut_frames(scenes: list[dict[str, Any]] | None, src_fps: float) -> list[int]:
    """Scene boundaries expressed in source frame indices."""
    if not scenes:
        return []
    cuts: list[int] = []
    for scene in scenes:
        try:
            start = float(scene.get("start_s", 0.0))
        except (TypeError, ValueError):
            continue
        frame = int(round(start * src_fps))
        if frame > 0:
            cuts.append(frame)
    return sorted(set(cuts))


def _rife_interpolate(
    input_path: str,
    out: str,
    target: int,
    workdir: str,
    model_path: str,
    options: dict,
) -> dict:
    import cv2

    keep = bool(options.get("keep_frames", False))
    provider = str(options.get("rife_provider", "auto")).lower()
    cancel_check = options.get("_cancel_check")
    session = _session(model_path, provider)

    source_info = ffmpeg.probe(input_path)
    if source_info.width > MAX_INTERPOLATE_WIDTH or source_info.height > MAX_INTERPOLATE_HEIGHT:
        raise CapabilityError(
            "RIFE input resolution exceeds the supported maximum",
            details={
                "width": source_info.width,
                "height": source_info.height,
                "max_width": MAX_INTERPOLATE_WIDTH,
                "max_height": MAX_INTERPOLATE_HEIGHT,
            },
        )

    cap = cv2.VideoCapture(input_path)
    if not cap.isOpened():
        raise RuntimeError(f"cannot decode {input_path}")
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
    n_src = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    ratio = target / src_fps
    if ratio <= 1.0:
        cap.release()
        raise RuntimeError(f"target fps {target} <= source fps {src_fps}")

    if n_src <= 0:
        n_src = max(1, int(round(source_info.duration_s * src_fps)))
    out_count = max(1, int(round(n_src * ratio)))

    cuts = _cut_frames(options.get("scenes"), src_fps)

    frames_dir = os.path.join(workdir, "04_rife_frames")
    os.makedirs(frames_dir, exist_ok=True)

    written = 0
    chunks = 0

    def write_frame(img) -> None:
        nonlocal written
        cv2.imwrite(
            os.path.join(frames_dir, f"{written:06d}.png"),
            img,
            [cv2.IMWRITE_PNG_COMPRESSION, 1],
        )
        written += 1

    ok, frame_a = cap.read()
    if not ok:
        cap.release()
        raise RuntimeError("no frames decoded")
    ok, frame_b = cap.read()
    if not ok:
        frame_b = frame_a.copy()

    write_frame(frame_a)
    i = 0
    for k in range(1, out_count):
        if cancel_check is not None and callable(cancel_check) and cancel_check():
            cap.release()
            shutil.rmtree(frames_dir, ignore_errors=True)
            raise RuntimeError("interpolation cancelled")
        pos = k / ratio
        j = int(math.floor(pos))
        frac = pos - j
        if j >= n_src - 1:
            j = n_src - 2
            frac = 1.0
        while i < j:
            ok2, nxt = cap.read()
            frame_a = frame_b
            if ok2 and nxt is not None:
                frame_b = nxt
            i += 1
        # Never blend across a hard cut: duplicate the first frame of the shot.
        crosses_cut = any(j < cut <= j + 1 for cut in cuts)
        if frac <= 1e-4 or crosses_cut:
            out_frame = frame_a
        elif frac >= 1.0 - 1e-4:
            out_frame = frame_b
        else:
            out_frame = _infer(session, frame_a, frame_b, frac)
        write_frame(out_frame)
        if written % CHUNK_SIZE == 0:
            chunks += 1
    cap.release()
    produced = written

    in_info = ffmpeg.probe(input_path)
    audio_args = ["-c:a", "aac", "-b:a", "192k"] if in_info.has_audio else ["-an"]
    enc_args, encoder = ffmpeg.video_encode_args(options, crf=18, preset="veryfast")
    ffmpeg.run_to_file(
        [
            ffmpeg.ffmpeg_bin(),
            "-y",
            "-framerate",
            str(target),
            "-start_number",
            "0",
            "-i",
            os.path.join(frames_dir, "%06d.png"),
            "-i",
            input_path,
            "-map",
            "0:v:0",
            "-map",
            "1:a:0?",
            *enc_args,
            *audio_args,
            "-shortest",
            "-movflags",
            "+faststart",
            out,
        ],
        out,
    )

    if not keep:
        shutil.rmtree(frames_dir, ignore_errors=True)

    out_info = ffmpeg.probe(out)
    return {
        "interp_model": "RIFE v4.9 (ONNX Runtime)",
        "model": os.path.basename(model_path),
        "provider": session.get_providers()[0],
        "encoder": encoder,
        "target_fps": target,
        "source_fps": round(src_fps, 3),
        "frames_in": n_src,
        "frames_out": produced,
        "output_fps": round(out_info.fps, 3),
        "chunks": chunks,
        "scene_cuts_avoided": len(cuts),
        "skipped": False,
        # The CPU execution above is real; the CUDA path is not validated yet.
        "gpu_validation_status": "pending",
    }


def _infer(session, frame_a, frame_b, t: float):
    import cv2
    import numpy as np

    a = cv2.cvtColor(frame_a, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    b = cv2.cvtColor(frame_b, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    img0 = a.transpose(2, 0, 1)[None]
    img1 = b.transpose(2, 0, 1)[None]
    timestep = np.array([t], dtype=np.float32)
    out = session.run(None, {"img0": img0, "img1": img1, "timestep": timestep})[0]
    out = np.clip(out[0].transpose(1, 2, 0), 0.0, 1.0) * 255.0
    return cv2.cvtColor(out.astype(np.uint8), cv2.COLOR_RGB2BGR)


# --------------------------------------------------------------------------- #
# FFmpeg backend
# --------------------------------------------------------------------------- #
def _ffmpeg_interpolate(input_path: str, out: str, target: int, quality: str, options: dict) -> dict:
    if quality == "high":
        vf = f"minterpolate=fps={target}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1"
    else:
        vf = f"minterpolate=fps={target}:mi_mode=blend"

    enc_args, encoder = ffmpeg.video_encode_args(options, crf=18, preset="veryfast")
    ffmpeg.run_to_file(
        [
            ffmpeg.ffmpeg_bin(),
            "-y",
            "-i",
            input_path,
            "-vf",
            vf,
            *enc_args,
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            out,
        ],
        out,
    )
    info = ffmpeg.probe(input_path)
    return {
        "interp_model": "ffmpeg minterpolate",
        "encoder": encoder,
        "quality": quality,
        "target_fps": target,
        "source_fps": round(info.fps, 3),
        "skipped": False,
        "gpu_validation_status": "pending",
    }
