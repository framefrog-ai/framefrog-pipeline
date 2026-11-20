#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""FrameFrog entrypoint.

Two ways in:

* ``python main.py --input rushes.mov`` — run the pipeline locally, print a
  manifest summary, exit non-zero on failure.
* ``from main import post_production`` — the callable a serverless GPU platform
  invokes; it takes the same arguments as the HTTP contract.
"""

from __future__ import annotations

import argparse
import json
import sys

from app.core.config import operator_settings
from app.jobs.models import DEFAULT_STAGES, JobRequest
from app.jobs.pipeline import run_job


def post_production(
    input_uri: str,
    stages: list[str] | None = None,
    options: dict | None = None,
    output_dir: str | None = None,
) -> dict:
    """Serverless entrypoint: run a job and return the manifest."""
    req = JobRequest(
        input_uri=input_uri,
        stages=stages or list(DEFAULT_STAGES),
        options=options or {},
        output_dir=output_dir,
    )
    return run_job(req, settings=operator_settings())


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="framefrog",
        description="AI cloud video post-production pipeline",
    )
    ap.add_argument("--input", required=True, help="local path or s3://bucket/key")
    ap.add_argument(
        "--stages",
        default=",".join(DEFAULT_STAGES),
        help=f"comma-separated stages (default: {','.join(DEFAULT_STAGES)})",
    )
    ap.add_argument("--out", default=None, help="output directory (default: temp)")
    ap.add_argument("--lut", default="cinematic_warm", help="built-in look or .cube path")
    ap.add_argument("--strength", type=float, default=1.0, help="look strength 0..1")
    ap.add_argument("--target-fps", type=int, default=60)
    ap.add_argument("--interp-quality", choices=["fast", "high"], default="fast")
    ap.add_argument("--lufs", type=float, default=-16.0)
    ap.add_argument(
        "--denoise-model",
        choices=["auto", "deepfilternet", "ffmpeg"],
        default="auto",
        help="audio denoise backend (default: auto — DeepFilterNet if installed)",
    )
    ap.add_argument(
        "--interp-model",
        choices=["auto", "rife", "ffmpeg"],
        default="auto",
        help="frame interpolation backend (default: auto — RIFE if the graph is present)",
    )
    ap.add_argument("--rife-onnx", default=None, help="path to a RIFE ONNX graph")
    ap.add_argument("--rife-provider", choices=["auto", "cpu", "cuda"], default="auto")
    ap.add_argument(
        "--webhook-url",
        default=None,
        help="POST the final manifest to this URL (HMAC with $FRAMEFROG_WEBHOOK_SECRET)",
    )
    ap.add_argument(
        "--gpu-rate",
        type=float,
        default=None,
        help="GPU hourly USD for cost accounting (default: $FRAMEFROG_GPU_HOURLY_USD or 0)",
    )
    ap.add_argument(
        "--cpu-rate",
        type=float,
        default=None,
        help="CPU hourly USD for cost accounting (default: $FRAMEFROG_CPU_HOURLY_USD or 0)",
    )
    ap.add_argument(
        "--fanout",
        choices=["off", "auto", "shots"],
        default="off",
        help="per-shot fan-out for color_grade/interpolate (default: off)",
    )
    ap.add_argument(
        "--fanout-mode",
        choices=["inline", "queue"],
        default="inline",
        help="run shot tasks in a thread pool or through a queue",
    )
    ap.add_argument("--fanout-workers", type=int, default=None)
    ap.add_argument(
        "--queue-url",
        default=None,
        help="fan-out queue: file://path or sqs://url (default: file queue under the job)",
    )
    ap.add_argument(
        "--encoder",
        choices=["auto", "libx264", "libx265", "nvenc", "h264_nvenc", "hevc_nvenc"],
        default="auto",
        help="video encoder; auto picks NVENC when FFmpeg has it",
    )
    ap.add_argument("--target-height", type=int, default=2160, help="upscale target height")
    ap.add_argument(
        "--upscale-backend",
        choices=["auto", "tensorrt", "realesrgan", "ffmpeg"],
        default="auto",
    )
    ap.add_argument("--upscale-engine", default=None, help="TensorRT .engine path")
    ap.add_argument("--upscale-model", default=None, help="Real-ESRGAN .pth path")
    ap.add_argument("--sam2-checkpoint", default=None, help="SAM 2 checkpoint (.pt)")
    ap.add_argument("--sam2-config", default=None, help="SAM 2 model config yaml")
    ap.add_argument("--rotoscope-mode", choices=["auto", "prompted"], default="auto")
    ap.add_argument("--rotoscope-prompt", default=None, help="JSON seed for prompted mode")
    ap.add_argument("--json", action="store_true", help="print the full manifest as JSON")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    stages = [s.strip() for s in args.stages.split(",") if s.strip()]
    options = {
        "lut": args.lut,
        "look_strength": args.strength,
        "target_fps": args.target_fps,
        "interp_quality": args.interp_quality,
        "lufs": args.lufs,
        "denoise_model": args.denoise_model,
        "interp_model": args.interp_model,
        "rife_provider": args.rife_provider,
    }
    options["encoder"] = args.encoder
    options["target_height"] = args.target_height
    options["upscale_backend"] = args.upscale_backend
    if args.upscale_engine:
        options["upscale_engine"] = args.upscale_engine
    if args.upscale_model:
        options["upscale_model"] = args.upscale_model
    if args.sam2_checkpoint:
        options["sam2_checkpoint"] = args.sam2_checkpoint
    if args.sam2_config:
        options["sam2_config"] = args.sam2_config
    options["rotoscope_mode"] = args.rotoscope_mode
    if args.rotoscope_prompt:
        import json as _json

        options["rotoscope_prompt"] = _json.loads(args.rotoscope_prompt)
    if args.rife_onnx:
        options["rife_onnx"] = args.rife_onnx
    if args.webhook_url:
        options["webhook_url"] = args.webhook_url
    if args.gpu_rate is not None:
        options["gpu_hourly_usd"] = args.gpu_rate
    if args.cpu_rate is not None:
        options["cpu_hourly_usd"] = args.cpu_rate
    if args.fanout != "off":
        options["fanout"] = args.fanout
        options["fanout_mode"] = args.fanout_mode
        if args.fanout_workers:
            options["fanout_workers"] = args.fanout_workers
        if args.queue_url:
            options["queue_url"] = args.queue_url
    manifest = post_production(args.input, stages=stages, options=options, output_dir=args.out)

    if args.json:
        print(json.dumps(manifest, indent=2, default=str))
    else:
        _print_summary(manifest)

    return 0 if manifest["status"] == "completed" else 1


def _print_summary(manifest: dict) -> None:
    print(f"\nFrameFrog job {manifest['job_id']}  [{manifest['status']}]")
    src = manifest.get("input", {})
    if src:
        print(
            f"  source   {src.get('width')}x{src.get('height')} "
            f"@ {src.get('fps')} fps · {src.get('duration_s')} s"
        )
    for st in manifest.get("stages", []):
        mark = "ok" if st["status"] == "completed" else "FAILED"
        print(f"  {st['stage']:<14} {mark:<7} {st['duration_s']:>6.2f}s")
        if st["status"] == "failed":
            print(f"    ↳ {st.get('error', '').splitlines()[0]}")
    art = manifest.get("artifacts", {})
    for key in ("master", "proxy", "poster", "scenes"):
        if art.get(key):
            print(f"  {key:<8} {art[key]}")
    if manifest.get("error"):
        print(f"  error    {manifest['error']}")
    cost = manifest.get("cost") or {}
    if cost.get("stages"):
        print(
            f"  compute  {cost.get('gpu_seconds', 0)}s gpu · "
            f"{cost.get('cpu_seconds', 0)}s cpu · ${cost.get('total_usd', 0):.4f}"
        )
    hook = manifest.get("webhook")
    if hook:
        state = "delivered" if hook.get("delivered") else "failed"
        print(f"  webhook  {state} ({hook.get('attempts')} attempt(s)) → {hook.get('url')}")
    print(f"  elapsed  {manifest.get('elapsed_s')} s\n")


if __name__ == "__main__":
    sys.exit(main())
