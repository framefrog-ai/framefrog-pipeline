# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Stage — 4K upscaling.

Three backends, chosen by ``upscale_backend``:

* **tensorrt** — run a prebuilt engine through :mod:`framefrog.tensorrt_runner`
  (fastest; requires ``upscale_engine`` and an NVIDIA GPU).
* **realesrgan** — Real-ESRGAN x4 in PyTorch (requires ``upscale_model`` and
  ``realesrgan``/``basicsr``).
* **ffmpeg** — Lanczos resample. Always available; not learned.

Every backend writes the same deliverable: a video at ``target_height``
(default 2160), with the original audio muxed once and the chosen encoder
(NVENC when ``encoder=auto`` finds it, libx264 otherwise). If the source is
already at or above the target height the stage copies through and reports
``skipped``.

Options
-------
``target_height`` (int, default 2160)
``upscale_backend`` (str, default "auto") — ``auto|tensorrt|realesrgan|ffmpeg``
``upscale_engine`` (str) — TensorRT ``.engine`` path
``upscale_model`` (str) — Real-ESRGAN ``.pth`` path
``upscale_tile`` (int, default 0) — Real-ESRGAN tiling (0 = no tiling)
``half`` (bool, default False) — fp16 for Real-ESRGAN on CUDA
``encoder`` — forwarded to the shared encoder chooser
"""

from __future__ import annotations

import importlib
import os
import shutil

from app.media import ffmpeg
from app.stages.base import StageResult


def _module(name: str) -> bool:
    try:
        importlib.import_module(name)
        return True
    except ImportError:
        return False


def _choose_backend(options: dict) -> str:
    backend = str(options.get("upscale_backend", "auto")).lower()
    if backend not in {"auto", "tensorrt", "realesrgan", "ffmpeg"}:
        raise ValueError(f"unknown upscale_backend {backend!r}; use auto|tensorrt|realesrgan|ffmpeg")
    if backend != "auto":
        return backend
    engine = options.get("upscale_engine")
    if engine and _module("tensorrt") and _module("pycuda"):
        return "tensorrt"
    model = options.get("upscale_model")
    if model and _module("realesrgan") and _module("basicsr") and _module("torch"):
        return "realesrgan"
    return "ffmpeg"


def run(input_path: str, workdir: str, options: dict) -> StageResult:
    info = ffmpeg.probe(input_path)
    target_h = int(options.get("target_height", 2160))
    target_w = _even(round(info.width * target_h / max(1, info.height)))
    out = os.path.join(workdir, "06_upscaled.mp4")

    if info.height and info.height >= target_h:
        shutil.copyfile(input_path, out)
        return StageResult(
            "upscale",
            out,
            {
                "skipped": True,
                "reason": "source already at or above target height",
                "source": {"width": info.width, "height": info.height},
                "target_height": target_h,
            },
        )

    backend = _choose_backend(options)
    enc_args, encoder = ffmpeg.video_encode_args(options, crf=14, preset="veryfast")
    source = {"width": info.width, "height": info.height, "fps": round(info.fps, 3)}
    base_meta = {
        "backend": backend,
        "encoder": encoder,
        "source": source,
        "target": {"width": target_w, "height": target_h},
    }

    if backend == "ffmpeg":
        _ffmpeg_upscale(input_path, out, target_h, enc_args, info.has_audio)
        return StageResult("upscale", out, {**base_meta, "learned": False})

    if backend == "tensorrt":
        engine = options.get("upscale_engine")
        if not engine:
            raise RuntimeError("upscale_backend=tensorrt requires options['upscale_engine'] (a .engine file)")
        from app.tensorrt_runner import TensorRTUpscaler

        with TensorRTUpscaler(engine) as runner:
            meta = _frame_upscale(input_path, out, target_w, target_h, options, runner.infer, enc_args, info)
            meta.update(base_meta)
            meta.update({"learned": True, "engine": runner.describe()})
        return StageResult("upscale", out, meta)

    # Real-ESRGAN
    model_path = options.get("upscale_model")
    if not model_path:
        raise RuntimeError(
            "upscale_backend=realesrgan requires options['upscale_model'] (e.g. RealESRGAN_x4plus.pth)"
        )
    infer, model_meta = _realesrgan_infer(model_path, options)
    meta = _frame_upscale(input_path, out, target_w, target_h, options, infer, enc_args, info)
    meta.update(base_meta)
    meta.update({"learned": True, **model_meta})
    return StageResult("upscale", out, meta)


def _even(value: int) -> int:
    return max(2, int(value) // 2 * 2)


def _ffmpeg_upscale(input_path: str, out: str, target_h: int, enc_args, has_audio: bool) -> None:
    audio = ["-c:a", "aac", "-b:a", "192k"] if has_audio else ["-an"]
    ffmpeg.run(
        [
            ffmpeg.ffmpeg_bin(),
            "-y",
            "-i",
            input_path,
            "-vf",
            f"scale=-2:{target_h}:flags=lanczos",
            *enc_args,
            *audio,
            "-movflags",
            "+faststart",
            out,
        ]
    )


def _frame_upscale(
    input_path: str,
    out: str,
    target_w: int,
    target_h: int,
    options: dict,
    infer,
    enc_args,
    info,
) -> dict:
    import cv2

    frames_dir = os.path.join(os.path.dirname(out), "06_upscale_frames")
    shutil.rmtree(frames_dir, ignore_errors=True)
    os.makedirs(frames_dir, exist_ok=True)

    cap = cv2.VideoCapture(input_path)
    if not cap.isOpened():
        raise RuntimeError(f"cannot decode {input_path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or info.fps or 24.0
    count = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        upscaled = infer(frame)
        if upscaled.shape[1] != target_w or upscaled.shape[0] != target_h:
            upscaled = cv2.resize(upscaled, (target_w, target_h), interpolation=cv2.INTER_AREA)
        cv2.imwrite(
            os.path.join(frames_dir, f"{count:06d}.png"),
            upscaled,
            [cv2.IMWRITE_PNG_COMPRESSION, 1],
        )
        count += 1
    cap.release()
    if count == 0:
        raise RuntimeError("no frames decoded for upscaling")

    audio = ["-i", input_path, "-map", "1:a:0?", "-c:a", "aac", "-b:a", "192k"] if info.has_audio else []
    ffmpeg.run(
        [
            ffmpeg.ffmpeg_bin(),
            "-y",
            "-framerate",
            f"{fps:.6f}",
            "-start_number",
            "0",
            "-i",
            os.path.join(frames_dir, "%06d.png"),
            *audio,
            "-map",
            "0:v:0",
            *enc_args,
            "-shortest" if audio else "-an",
            "-movflags",
            "+faststart",
            out,
        ]
    )
    if not options.get("upscale_keep_frames"):
        shutil.rmtree(frames_dir, ignore_errors=True)

    return {"frames": count, "fps": round(fps, 3)}


def _realesrgan_infer(model_path: str, options: dict):
    if not os.path.exists(model_path):
        raise RuntimeError(f"Real-ESRGAN model not found: {model_path}")
    import torch
    from basicsr.archs.rrdbnet_arch import RRDBNet
    from realesrgan import RealESRGANer

    use_cuda = torch.cuda.is_available()
    model = RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64, num_block=23, num_grow_ch=32, scale=4)
    upsampler = RealESRGANer(
        scale=4,
        model_path=model_path,
        model=model,
        tile=int(options.get("upscale_tile", 0)),
        tile_pad=10,
        pre_pad=0,
        half=bool(options.get("half", False)) and use_cuda,
        device=torch.device("cuda" if use_cuda else "cpu"),
    )

    def infer(frame):
        output, _ = upsampler.enhance(frame, outscale=4)
        return output

    return infer, {"model": os.path.basename(model_path), "cuda": use_cuda}
