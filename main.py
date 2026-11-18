#!/usr/bin/env python3
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
import os
import sys
from typing import Dict, List, Optional

from framefrog.pipeline import STAGE_NAMES, describe, run_job
from framefrog.types import JobRequest


def post_production(
    input_uri: str,
    stages: Optional[List[str]] = None,
    options: Optional[Dict] = None,
    output_dir: Optional[str] = None,
) -> Dict:
    """Serverless entrypoint: run a job and return the manifest."""
    req = JobRequest(
        input_uri=input_uri,
        stages=stages or list(STAGE_NAMES),
        options=options or {},
        output_dir=output_dir,
    )
    return run_job(req)


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="framefrog",
        description="AI cloud video post-production pipeline",
    )
    ap.add_argument("--input", required=True, help="local path or s3://bucket/key")
    ap.add_argument(
        "--stages",
        default=",".join(STAGE_NAMES),
        help=f"comma-separated stages (default: {','.join(STAGE_NAMES)})",
    )
    ap.add_argument("--out", default=None, help="output directory (default: temp)")
    ap.add_argument("--lut", default="cinematic_warm", help="built-in look or .cube path")
    ap.add_argument("--strength", type=float, default=1.0, help="look strength 0..1")
    ap.add_argument("--target-fps", type=int, default=60)
    ap.add_argument("--interp-quality", choices=["fast", "high"], default="fast")
    ap.add_argument("--lufs", type=float, default=-16.0)
    ap.add_argument(
        "--denoise-model", choices=["auto", "deepfilternet", "ffmpeg"], default="auto",
        help="audio denoise backend (default: auto — DeepFilterNet if installed)",
    )
    ap.add_argument(
        "--interp-model", choices=["auto", "rife", "ffmpeg"], default="auto",
        help="frame interpolation backend (default: auto — RIFE if the graph is present)",
    )
    ap.add_argument("--rife-onnx", default=None, help="path to a RIFE ONNX graph")
    ap.add_argument("--rife-provider", choices=["auto", "cpu", "cuda"], default="auto")
    ap.add_argument(
        "--webhook-url", default=None,
        help="POST the final manifest to this URL (HMAC with $FRAMEFROG_WEBHOOK_SECRET)",
    )
    ap.add_argument(
        "--gpu-rate", type=float, default=None,
        help="GPU hourly USD for cost accounting (default: $FRAMEFROG_GPU_HOURLY_USD or 0)",
    )
    ap.add_argument(
        "--cpu-rate", type=float, default=None,
        help="CPU hourly USD for cost accounting (default: $FRAMEFROG_CPU_HOURLY_USD or 0)",
    )
    ap.add_argument("--json", action="store_true", help="print the full manifest as JSON")
    return ap.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
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
    if args.rife_onnx:
        options["rife_onnx"] = args.rife_onnx
    if args.webhook_url:
        options["webhook_url"] = args.webhook_url
    if args.gpu_rate is not None:
        options["gpu_hourly_usd"] = args.gpu_rate
    if args.cpu_rate is not None:
        options["cpu_hourly_usd"] = args.cpu_rate
    manifest = post_production(args.input, stages=stages, options=options, output_dir=args.out)

    if args.json:
        print(json.dumps(manifest, indent=2, default=str))
    else:
        _print_summary(manifest)

    return 0 if manifest["status"] == "completed" else 1


def _print_summary(manifest: Dict) -> None:
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
