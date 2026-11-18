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
    }
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
    print(f"  elapsed  {manifest.get('elapsed_s')} s\n")


if __name__ == "__main__":
    sys.exit(main())
