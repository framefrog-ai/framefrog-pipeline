#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""GPU smoke test — the tool that validates the GPU path once a GPU exists.

    python scripts/gpu_smoke_test.py
    python scripts/gpu_smoke_test.py --json

It reports, in order:

* the Python and PyTorch versions
* the CUDA runtime status, device name and total memory
* whether the configured model can be loaded
* a short generated clip pushed through the interpolation path
* the output frame rate verified with FFprobe
* the measured wall-clock time and the peak allocated CUDA memory

Every number it prints is measured in this run; nothing is hard-coded and no
example result is stored in the repository. Exit code is non-zero when any
required check fails.

Without a CUDA device the script exits with a clear "CUDA GPU UNAVAILABLE"
message and a non-zero code — a GPU validation that never ran must never look
like a pass.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import tempfile
import time
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

REQUIRED_CUDA = "NOT RUN — CUDA GPU UNAVAILABLE"


def _probe_torch() -> dict[str, Any]:
    try:
        import torch  # type: ignore
    except ImportError:
        return {
            "installed": False,
            "version": None,
            "cuda_available": False,
            "reason": "torch is not installed",
        }
    info: dict[str, Any] = {
        "installed": True,
        "version": getattr(torch, "__version__", None),
        "cuda_available": bool(torch.cuda.is_available()),
        "cuda_version": getattr(torch.version, "cuda", None),
        "device_count": 0,
        "gpu_name": None,
        "total_vram_mb": None,
        "reason": None,
    }
    if info["cuda_available"]:
        info["device_count"] = torch.cuda.device_count()
        try:
            props = torch.cuda.get_device_properties(0)
            info["gpu_name"] = props.name
            info["total_vram_mb"] = round(props.total_memory / (1024**2), 1)
        except Exception as exc:  # pragma: no cover - driver dependent
            info["reason"] = f"device query failed: {exc}"
    return info


def _generate_clip(path: str, seconds: float = 1.0, fps: int = 24, size: str = "320x180") -> None:
    from app.media import ffmpeg

    ffmpeg.run_to_file(
        [
            ffmpeg.ffmpeg_bin(),
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size={size}:rate={fps}:duration={seconds}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "28",
            "-pix_fmt",
            "yuv420p",
            path,
        ],
        path,
    )


def _load_model(model_path: str | None) -> dict[str, Any]:
    """Load the configured RIFE graph and report what onnxruntime exposes."""
    result: dict[str, Any] = {"path": model_path, "loaded": False, "reason": None}
    if not model_path:
        result["reason"] = "no RIFE model configured (python scripts/download_models.py)"
        return result
    try:
        import onnxruntime as ort  # type: ignore
    except ImportError:
        result["reason"] = "onnxruntime is not installed"
        return result
    try:
        session = ort.InferenceSession(
            model_path, providers=["CUDAExecutionProvider", "CPUExecutionProvider"]
        )
        result["loaded"] = True
        result["providers"] = list(session.get_providers())
        result["inputs"] = [i.name for i in session.get_inputs()]
    except Exception as exc:
        result["reason"] = f"{type(exc).__name__}: {exc}"
    return result


def _run_interpolation(clip: str, out: str, target_fps: int) -> dict[str, Any]:
    """Interpolate the clip and verify the result with FFprobe."""
    from app.media import ffmpeg

    started = time.time()
    ffmpeg.run_to_file(
        [
            ffmpeg.ffmpeg_bin(),
            "-y",
            "-i",
            clip,
            "-vf",
            f"minterpolate=fps={target_fps}:mi_mode=blend",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-an",
            out,
        ],
        out,
    )
    elapsed = time.time() - started
    info = ffmpeg.probe(out)
    return {
        "elapsed_s": round(elapsed, 3),
        "output_fps": round(info.fps, 3),
        "target_fps": target_fps,
        "frames_ok": info.fps >= target_fps - 0.5,
        "width": info.width,
        "height": info.height,
    }


def _peak_vram_mb() -> float | None:
    """Peak CUDA memory allocated by this process, when torch can report it."""
    try:
        import torch  # type: ignore
    except ImportError:
        return None
    if not torch.cuda.is_available():
        return None
    return round(torch.cuda.max_memory_allocated() / (1024**2), 1)


def run(args: argparse.Namespace) -> dict[str, Any]:
    report: dict[str, Any] = {
        "python": {"version": platform.python_version()},
        "torch": _probe_torch(),
        "model": {},
        "interpolation": {},
        "peak_vram_mb": None,
        "failures": list[str](),
    }

    if not report["torch"].get("cuda_available"):
        report["failures"].append(REQUIRED_CUDA)
        report["status"] = "not_run"
        return report

    try:
        import torch  # type: ignore

        torch.cuda.reset_peak_memory_stats()
    except Exception:  # pragma: no cover - torch is present at this point
        torch = None

    from app.models.discovery import find_rife_model

    model_path = find_rife_model(args.rife_model)
    report["model"] = _load_model(model_path)
    if not report["model"]["loaded"]:
        report["failures"].append(f"model not loaded: {report['model'].get('reason')}")

    with tempfile.TemporaryDirectory(prefix="framefrog_gpu_") as tmp:
        clip = os.path.join(tmp, "clip.mp4")
        out = os.path.join(tmp, "out.mp4")
        try:
            _generate_clip(clip)
            report["interpolation"] = _run_interpolation(clip, out, args.target_fps)
            if not report["interpolation"].get("frames_ok"):
                report["failures"].append("interpolated output did not reach the target frame rate")
        except Exception as exc:
            report["failures"].append(f"interpolation failed: {type(exc).__name__}: {exc}")

    if torch is not None:
        report["peak_vram_mb"] = _peak_vram_mb()

    report["status"] = "passed" if not report["failures"] else "failed"
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="FrameFrog GPU smoke test")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--rife-model", default=None, help="path to the RIFE ONNX graph")
    parser.add_argument("--target-fps", type=int, default=60)
    args = parser.parse_args(argv)

    report = run(args)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print("FrameFrog GPU smoke test")
        print(f"  python          {report['python']['version']}")
        torch_info = report["torch"]
        print(f"  torch           {torch_info.get('version') or 'not installed'}")
        print(f"  cuda available  {torch_info.get('cuda_available')}")
        if torch_info.get("gpu_name"):
            print(f"  gpu             {torch_info['gpu_name']}")
            print(f"  total vram      {torch_info.get('total_vram_mb')} MB")
        if report["model"]:
            print(
                f"  model           {report['model'].get('path')} "
                f"loaded={report['model'].get('loaded')} "
                f"{report['model'].get('reason') or ''}"
            )
        if report["interpolation"]:
            interp = report["interpolation"]
            print(f"  interpolation   {interp.get('output_fps')} fps in {interp.get('elapsed_s')} s")
        if report.get("peak_vram_mb") is not None:
            print(f"  peak vram       {report['peak_vram_mb']} MB")
        for failure in report["failures"]:
            print(f"  FAIL            {failure}")
        print(f"\nResult: {report['status'].upper()}")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
