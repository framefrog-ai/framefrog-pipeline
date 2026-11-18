"""Stage 4 — frame interpolation.

Raises the frame rate to 60/120 fps. Two backends:

* **RIFE v4.9 (ONNX Runtime)** — real optical-flow interpolation. Used when
  `interp_model` is ``rife``/``auto`` and a graph is found
  (``python scripts/fetch_models.py``). CPU works; CUDA is used automatically
  when available.
* **FFmpeg minterpolate** — blend (fast) or full motion-compensated (high).
  Always available and the fallback when RIFE is missing.

If the source is already at or above the target rate, the stage is a no-op and
reports ``skipped: True`` instead of burning compute.

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
"""

from __future__ import annotations

import math
import os
import shutil

from framefrog import ffmpeg
from framefrog.models import find_rife_model, onnxruntime_available
from framefrog.types import StageResult

_SESSIONS = {}


def run(input_path: str, workdir: str, options: dict) -> StageResult:
    out = os.path.join(workdir, "04_interpolated.mp4")
    target = int(options.get("target_fps", 60))
    if target <= 0:
        raise ValueError("target_fps must be positive")
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
            },
        )

    backend = str(options.get("interp_model", "auto")).lower()
    if backend not in {"auto", "rife", "ffmpeg"}:
        raise ValueError(f"unknown interp_model {backend!r}; use auto|rife|ffmpeg")

    model_path = find_rife_model(options.get("rife_onnx"))
    have_rife = bool(model_path) and onnxruntime_available()

    if backend == "rife" and not have_rife:
        reason = (
            "RIFE ONNX graph not found — run `python scripts/fetch_models.py`"
            if not model_path
            else "onnxruntime not installed — `pip install onnxruntime`"
        )
        raise RuntimeError(reason)

    if backend in {"auto", "rife"} and have_rife:
        try:
            meta = _rife_interpolate(
                input_path, out, target, workdir, model_path, options
            )
            return StageResult("interpolate", out, meta)
        except Exception as exc:
            if backend == "rife":
                raise
            fallback_reason = f"{type(exc).__name__}: {exc}"
    elif backend == "ffmpeg":
        fallback_reason = "interp_model=ffmpeg"
    else:
        fallback_reason = (
            "RIFE ONNX graph not found" if not model_path
            else "onnxruntime not installed"
        )

    meta = _ffmpeg_interpolate(input_path, out, target, quality)
    meta["fallback_reason"] = fallback_reason
    return StageResult("interpolate", out, meta)


# --------------------------------------------------------------------------- #
# RIFE backend
# --------------------------------------------------------------------------- #
def _provider(pref: str) -> list:
    if pref == "cuda":
        return ["CUDAExecutionProvider", "CPUExecutionProvider"]
    if pref == "cpu":
        return ["CPUExecutionProvider"]
    return ["CUDAExecutionProvider", "CPUExecutionProvider"]


def _session(model_path: str, provider: str):
    key = (os.path.abspath(model_path), provider)
    if key not in _SESSIONS:
        import onnxruntime as ort

        _SESSIONS[key] = ort.InferenceSession(model_path, providers=_provider(provider))
    return _SESSIONS[key]


def _rife_interpolate(
    input_path: str,
    out: str,
    target: int,
    workdir: str,
    model_path: str,
    options: dict,
) -> dict:
    import cv2
    import numpy as np

    keep = bool(options.get("keep_frames", False))
    provider = str(options.get("rife_provider", "auto")).lower()
    session = _session(model_path, provider)

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
        # Frame count is unreliable in some containers; derive from duration.
        n_src = max(1, int(round(ffmpeg.probe(input_path).duration_s * src_fps)))
    out_count = max(1, int(round(n_src * ratio)))

    frames_dir = os.path.join(workdir, "04_rife_frames")
    os.makedirs(frames_dir, exist_ok=True)

    def write_frame(img) -> None:
        idx = write_frame.idx
        cv2.imwrite(
            os.path.join(frames_dir, f"{idx:06d}.png"),
            img,
            [cv2.IMWRITE_PNG_COMPRESSION, 1],
        )
        write_frame.idx += 1

    write_frame.idx = 0

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
        if frac <= 1e-4:
            out_frame = frame_a
        elif frac >= 1.0 - 1e-4:
            out_frame = frame_b
        else:
            out_frame = _infer(session, frame_a, frame_b, frac)
        write_frame(out_frame)
    cap.release()
    produced = write_frame.idx

    in_info = ffmpeg.probe(input_path)
    audio_args = ["-c:a", "aac", "-b:a", "192k"] if in_info.has_audio else ["-an"]
    ffmpeg.run([
        ffmpeg.ffmpeg_bin(), "-y",
        "-framerate", str(target), "-start_number", "0",
        "-i", os.path.join(frames_dir, "%06d.png"),
        "-i", input_path,
        "-map", "0:v:0", "-map", "1:a:0?",
        "-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        *audio_args,
        "-shortest", "-movflags", "+faststart",
        out,
    ])

    if not keep:
        shutil.rmtree(frames_dir, ignore_errors=True)

    out_info = ffmpeg.probe(out)
    return {
        "interp_model": "RIFE v4.9 (ONNX Runtime)",
        "model": os.path.basename(model_path),
        "provider": session.get_providers()[0],
        "target_fps": target,
        "source_fps": round(src_fps, 3),
        "frames_in": n_src,
        "frames_out": produced,
        "output_fps": round(out_info.fps, 3),
        "skipped": False,
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
def _ffmpeg_interpolate(input_path: str, out: str, target: int, quality: str) -> dict:
    if quality == "high":
        vf = (
            f"minterpolate=fps={target}:mi_mode=mci:mc_mode=aobmc:"
            "me_mode=bidir:vsbmc=1"
        )
    else:
        vf = f"minterpolate=fps={target}:mi_mode=blend"

    ffmpeg.run([
        ffmpeg.ffmpeg_bin(), "-y", "-i", input_path,
        "-vf", vf,
        "-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        out,
    ])
    info = ffmpeg.probe(input_path)
    return {
        "interp_model": "ffmpeg minterpolate",
        "quality": quality,
        "target_fps": target,
        "source_fps": round(info.fps, 3),
        "skipped": False,
    }
