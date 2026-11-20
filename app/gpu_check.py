# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""GPU smoke test: NVENC, TensorRT, SAM 2 and Real-ESRGAN.

    python -m framefrog.gpu_check                 # environment + encode probe
    python -m framefrog.gpu_check --all           # auto-discover weights in assets/models
    python -m framefrog.gpu_check --engine up.engine \
        --sam2-checkpoint assets/models/sam2.1_hiera_small.pt \
        --realesrgan-model assets/models/RealESRGAN_x4plus.pth

Exit code is 0 when every *requested* check passes (environment probes and
skipped model checks do not fail the run) and 1 otherwise. ``--json`` prints a
machine-readable report; ``--verbose`` keeps the FFmpeg tail.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import tempfile
import time
from typing import Any

from app.media import ffmpeg


def _probe_environment() -> dict[str, Any]:
    env: dict[str, Any] = {
        "ffmpeg": ffmpeg.has_ffmpeg(),
        "nvenc_compiled": ffmpeg.has_encoder("h264_nvenc"),
        "nvidia_device": ffmpeg.has_nvidia_device(),
    }
    try:
        env["encoder"] = ffmpeg.choose_encoder({})
    except Exception as exc:  # pragma: no cover - environment dependent
        env["encoder"] = None
        env["encoder_error"] = f"{type(exc).__name__}: {exc}"
    try:
        import torch  # type: ignore

        env["torch"] = {
            "version": torch.__version__,
            "cuda": bool(torch.cuda.is_available()),
            "device": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
        }
    except ImportError:
        env["torch"] = {"available": False}
    env["tensorrt"] = _module_version("tensorrt")
    env["pycuda"] = _module_version("pycuda")
    env["sam2"] = _module_version("sam2")
    env["realesrgan"] = _module_version("realesrgan")
    return env


def _module_version(name: str) -> dict[str, Any]:
    try:
        mod = __import__(name)
        return {"available": True, "version": getattr(mod, "__version__", None)}
    except Exception as exc:
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"}


def _check_encode(encoder: str | None) -> dict[str, Any]:
    if not ffmpeg.has_ffmpeg() or not encoder:
        return {"status": "skipped", "reason": "no usable encoder"}
    started = time.time()
    with tempfile.TemporaryDirectory(prefix="ff_gpu_") as tmp:
        out = os.path.join(tmp, "encode.mp4")
        args, name = ffmpeg.video_encode_args({"encoder": encoder}, crf=23, preset="ultrafast")
        try:
            ffmpeg.run(
                [
                    ffmpeg.ffmpeg_bin(),
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=320x180:rate=24:duration=0.5",
                    *args,
                    "-an",
                    out,
                ]
            )
            size = os.path.getsize(out) if os.path.exists(out) else 0
        except Exception as exc:
            return {"status": "failed", "encoder": name, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "status": "ok" if size > 0 else "failed",
        "encoder": name,
        "bytes": size,
        "elapsed_s": round(time.time() - started, 3),
    }


def _check_tensorrt(engine: str) -> dict[str, Any]:
    if not engine:
        return {"status": "skipped", "reason": "no --engine"}
    started = time.time()
    try:
        from app.tensorrt_runner import TensorRTUpscaler

        with TensorRTUpscaler(engine) as runner:
            size = runner.expected_input_size() or (256, 144)
            frame = _synthetic_frame(size[0], size[1])
            result = runner.infer(frame)
            desc = runner.describe()
        return {
            "status": "ok",
            "engine": desc["engine"],
            "input": desc["input"],
            "output": desc["output"],
            "frame_in": [size[0], size[1]],
            "frame_out": [int(result.shape[1]), int(result.shape[0])],
            "elapsed_s": round(time.time() - started, 3),
        }
    except Exception as exc:
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}


def _check_realesrgan(model: str) -> dict[str, Any]:
    if not model:
        return {"status": "skipped", "reason": "no --realesrgan-model"}
    started = time.time()
    try:
        from app.stages.upscale import _realesrgan_infer

        infer, meta = _realesrgan_infer(model, {"half": True})
        frame = _synthetic_frame(160, 96)
        out = infer(frame)
        return {
            "status": "ok",
            "model": meta["model"],
            "cuda": meta["cuda"],
            "frame_in": [160, 96],
            "frame_out": [int(out.shape[1]), int(out.shape[0])],
            "elapsed_s": round(time.time() - started, 3),
        }
    except Exception as exc:
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}


def _check_sam2(checkpoint: str, config: str, device: str) -> dict[str, Any]:
    if not checkpoint:
        return {"status": "skipped", "reason": "no --sam2-checkpoint"}
    started = time.time()
    try:
        from app.stages import rotoscope

        with tempfile.TemporaryDirectory(prefix="ff_sam2_") as tmp:
            video = _synthetic_clip(tmp)
            workdir = os.path.join(tmp, "job")
            os.makedirs(workdir, exist_ok=True)
            result = rotoscope.run(
                video,
                workdir,
                {
                    "sam2_checkpoint": checkpoint,
                    "sam2_config": config,
                    "sam2_device": device,
                    "rotoscope_mode": "auto",
                },
            )
            summary = result.meta["summary"]
            matte = result.meta["matte"]
            size = os.path.getsize(matte) if os.path.exists(matte) else 0
        return {
            "status": "ok" if size > 0 else "failed",
            "frames": summary["frames"],
            "coverage": summary["coverage"],
            "seed": summary["seed"],
            "matte_bytes": size,
            "elapsed_s": round(time.time() - started, 3),
        }
    except Exception as exc:
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}


def _synthetic_frame(width: int, height: int):
    import numpy as np

    yy, xx = np.mgrid[0:height, 0:width]
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[..., 0] = (xx * 255 // max(1, width)).astype(np.uint8)
    frame[..., 1] = (yy * 255 // max(1, height)).astype(np.uint8)
    frame[..., 2] = 64
    return frame


def _synthetic_clip(tmp: str) -> str:
    """Eight frames of a white square moving across a dark background."""
    import cv2
    import numpy as np

    frames_dir = os.path.join(tmp, "frames")
    os.makedirs(frames_dir, exist_ok=True)
    for i in range(8):
        frame = np.full((96, 160, 3), 20, dtype=np.uint8)
        x = 10 + i * 16
        cv2.rectangle(frame, (x, 32), (x + 30, 62), (240, 240, 240), -1)
        cv2.imwrite(os.path.join(frames_dir, f"{i:03d}.png"), frame)
    out = os.path.join(tmp, "synthetic.mp4")
    ffmpeg.run(
        [
            ffmpeg.ffmpeg_bin(),
            "-y",
            "-framerate",
            "8",
            "-start_number",
            "0",
            "-i",
            os.path.join(frames_dir, "%03d.png"),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-an",
            out,
        ]
    )
    return out


def _discover() -> dict[str, str | None]:
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "models")
    engines = sorted(glob.glob(os.path.join(root, "*.engine")))
    sam2 = sorted(glob.glob(os.path.join(root, "sam2*.pt")))
    esrgan = sorted(glob.glob(os.path.join(root, "RealESRGAN*.pth")))
    return {
        "engine": engines[0] if engines else None,
        "sam2_checkpoint": sam2[0] if sam2 else None,
        "realesrgan_model": esrgan[0] if esrgan else None,
    }


def run_checks(args: argparse.Namespace) -> dict[str, Any]:
    discovered = _discover() if args.all else {}
    engine = args.engine or discovered.get("engine")
    sam2_checkpoint = args.sam2_checkpoint or discovered.get("sam2_checkpoint")
    realesrgan_model = args.realesrgan_model or discovered.get("realesrgan_model")

    env = _probe_environment()
    report: dict[str, Any] = {"environment": env, "checks": {}}

    encoder = args.encoder or env.get("encoder")
    report["checks"]["encode"] = _check_encode(encoder)
    report["checks"]["tensorrt"] = _check_tensorrt(engine or "")
    report["checks"]["sam2"] = _check_sam2(
        sam2_checkpoint or "",
        args.sam2_config or "",
        args.sam2_device or ("cuda" if env.get("torch", {}).get("cuda") else "cpu"),
    )
    report["checks"]["realesrgan"] = _check_realesrgan(realesrgan_model or "")

    failed = [name for name, c in report["checks"].items() if c.get("status") == "failed"]
    ran = [name for name, c in report["checks"].items() if c.get("status") == "ok"]
    skipped = [name for name, c in report["checks"].items() if c.get("status") == "skipped"]
    report["summary"] = {
        "ok": not failed,
        "passed": ran,
        "failed": failed,
        "skipped": skipped,
    }
    report["exit_code"] = 0 if not failed else 1
    return report


def _print_report(report: dict[str, Any]) -> None:
    env = report["environment"]
    print("FrameFrog GPU smoke test")
    print(
        f"  ffmpeg {env['ffmpeg']} · nvenc_compiled={env['nvenc_compiled']} · "
        f"nvidia_device={env['nvidia_device']} · encoder={env.get('encoder')}"
    )
    torch_info = env.get("torch") or {}
    if torch_info.get("available") is False:
        print("  torch    not installed")
    else:
        print(
            f"  torch    {torch_info.get('version')} · cuda={torch_info.get('cuda')}"
            + (f" · {torch_info.get('device')}" if torch_info.get("device") else "")
        )
    print(f"  tensorrt {env['tensorrt']} · pycuda {env['pycuda']} · sam2 {env['sam2']}")

    for name, check in report["checks"].items():
        status = check.get("status")
        detail = ""
        if status == "ok":
            detail = " · ".join(
                f"{k}={v}" for k, v in check.items() if k not in {"status"} and not isinstance(v, dict)
            )
        elif status == "failed":
            detail = check.get("error", "")
        elif status == "skipped":
            detail = check.get("reason", "")
        print(f"  {name:<11} {status:<8} {detail}")

    summary = report["summary"]
    verdict = "PASS" if summary["ok"] else "FAIL"
    print(
        f"\nResult: {verdict} — {len(summary['passed'])} passed, "
        f"{len(summary['failed'])} failed, {len(summary['skipped'])} skipped"
    )
    if summary["failed"]:
        print("Failed: " + ", ".join(summary["failed"]))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="framefrog.gpu_check",
        description="Smoke-test NVENC, TensorRT, SAM 2 and Real-ESRGAN on this host",
    )
    ap.add_argument("--engine", default=None, help="TensorRT .engine to load and run")
    ap.add_argument("--sam2-checkpoint", default=None, help="SAM 2 .pt checkpoint")
    ap.add_argument(
        "--sam2-config",
        default="configs/sam2.1/sam2.1_hiera_l.yaml",
        help="SAM 2 config yaml",
    )
    ap.add_argument("--sam2-device", choices=["cuda", "cpu"], default=None)
    ap.add_argument("--realesrgan-model", default=None, help="Real-ESRGAN .pth")
    ap.add_argument("--encoder", default=None, help="force an encoder for the encode check")
    ap.add_argument("--all", action="store_true", help="auto-discover weights in assets/models")
    ap.add_argument("--json", action="store_true", help="print a JSON report")
    args = ap.parse_args(argv)

    report = run_checks(args)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_report(report)
    return int(report.get("exit_code", 1))


if __name__ == "__main__":
    sys.exit(main())
