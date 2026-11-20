#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Report the runtime environment and validate the configuration.

    python scripts/check_environment.py
    python scripts/check_environment.py --json

Exit code 0 when the mandatory pieces are present (Python, FFmpeg, FFprobe
and a valid configuration), 1 otherwise. Optional runtimes (DeepFilterNet,
onnxruntime, torch/CUDA, SAM 2, TensorRT) are reported but never required.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def collect() -> dict:
    from app.core import capabilities
    from app.core.config import Settings

    report: dict = {
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "executable": sys.executable,
        },
        "platform": platform.platform(),
        "capabilities": capabilities.report(),
        "configuration": {},
        "errors": [],
    }
    try:
        settings = Settings()
        report["configuration"] = {
            "app_env": settings.app_env,
            "log_level": settings.log_level,
            "storage_backend": settings.storage_backend,
            "work_dir": str(settings.work_dir_resolved),
            "database_url": settings.database_url,
            "auth_required": settings.auth_required,
            "max_upload_bytes": settings.max_upload_bytes,
            "max_video_duration_seconds": settings.max_video_duration_seconds,
            "max_video_width": settings.max_video_width,
            "max_video_height": settings.max_video_height,
            "max_output_fps": settings.max_output_fps,
            "max_concurrent_jobs": settings.max_concurrent_jobs,
            "compute_device": settings.compute_device,
            "gpu_validation_required": settings.gpu_validation_required,
        }
    except Exception as exc:
        report["errors"].append(f"configuration invalid: {type(exc).__name__}: {exc}")

    caps = report["capabilities"]
    if not caps["ffmpeg"].get("available"):
        report["errors"].append("ffmpeg is not available")
    if not caps["ffprobe"].get("available"):
        report["errors"].append("ffprobe is not available")
    return report


def print_human(report: dict) -> None:
    caps = report["capabilities"]
    print("FrameFrog environment check")
    print(f"  python            {report['python']['version']} ({report['python']['implementation']})")
    print(f"  platform          {report['platform']}")
    ff = caps["ffmpeg"]
    print(f"  ffmpeg            {ff.get('version') or ff.get('reason')}")
    fp = caps["ffprobe"]
    print(f"  ffprobe           {fp.get('version') or fp.get('reason')}")
    encoders = ff.get("encoders") or {}
    print(
        "  encoders          "
        + ", ".join(f"{name}={'yes' if ok else 'no'}" for name, ok in sorted(encoders.items()))
    )
    print(f"  scenedetect       {'yes' if caps['scenedetect']['available'] else 'no'}")
    dfn = caps["deepfilternet"]
    print(f"  deepfilternet     {'yes' if dfn['available'] else 'no'}")
    ort = caps["onnxruntime"]
    providers = ", ".join(ort.get("providers") or []) or ort.get("reason", "")
    print(f"  onnxruntime       {'yes' if ort['available'] else 'no'} {providers}")
    torch_info = caps["torch"]
    if torch_info.get("torch_installed"):
        print(
            f"  torch             {torch_info.get('torch_version')} "
            f"(cuda available: {torch_info.get('cuda_available')})"
        )
        if torch_info.get("gpu_name"):
            print(
                f"  gpu               {torch_info['gpu_name']} "
                f"({torch_info.get('total_vram_mb')} MB, CUDA {torch_info.get('cuda_version')})"
            )
    else:
        print("  torch             not installed")
    print(f"  cuda available    {caps['cuda_available']}")
    if report["configuration"]:
        print("\nConfiguration:")
        for key, value in report["configuration"].items():
            print(f"  {key:<17} {value}")
    if report["errors"]:
        print("\nProblems:")
        for error in report["errors"]:
            print(f"  - {error}")
    print(f"\nResult: {'OK' if not report['errors'] else 'FAILED'}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="check the FrameFrog runtime environment")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    report = collect()
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print_human(report)
    return 0 if not report["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
