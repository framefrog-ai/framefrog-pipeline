#!/usr/bin/env python3
"""End-to-end demo — no footage required.

Generates a two-scene 6-second test clip with audio, then runs the full
pipeline (audio cleanup, scene detection, LUT grade, 60 fps interpolation,
delivery) and verifies the artifacts.

    python demo.py              # full run
    python demo.py --quick      # skip interpolation, still exercises the rest
    python demo.py --keep       # keep the generated sample clip

The point is that a stranger can clone the repo, run one command, and see a
master file, a proxy, a poster and a manifest appear — with FFmpeg as the only
system dependency.

Optional: `--interp-model rife --denoise-model deepfilternet` to exercise the
model-backed stages instead of the FFmpeg fallbacks.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from framefrog import ffmpeg
from framefrog.pipeline import run_job
from framefrog.types import JobRequest

ROOT = os.path.dirname(os.path.abspath(__file__))
SAMPLE_DIR = os.path.join(ROOT, "assets", "sample")
SAMPLE = os.path.join(SAMPLE_DIR, "demo_source.mp4")
OUT = os.path.join(ROOT, "outputs")


def make_sample() -> str:
    os.makedirs(SAMPLE_DIR, exist_ok=True)
    if os.path.exists(SAMPLE):
        return SAMPLE
    if not ffmpeg.has_ffmpeg():
        raise SystemExit("ffmpeg is required for the demo: apt install ffmpeg")
    print("• generating 6 s two-scene sample (testsrc2 → smptebars + 440 Hz tone)")
    ffmpeg.run([
        ffmpeg.ffmpeg_bin(), "-y",
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=3",
        "-f", "lavfi", "-i", "smptebars=size=640x360:rate=30:duration=3",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=6",
        "-filter_complex",
        "[0:v][1:v]concat=n=2:v=1:a=0[v];[2:a]volume=0.4[a]",
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        SAMPLE,
    ])
    return SAMPLE


def main() -> int:
    ap = argparse.ArgumentParser(description="FrameFrog end-to-end demo")
    ap.add_argument("--quick", action="store_true", help="skip frame interpolation")
    ap.add_argument(
        "--denoise-model", choices=["auto", "deepfilternet", "ffmpeg"], default="auto"
    )
    ap.add_argument(
        "--interp-model", choices=["auto", "rife", "ffmpeg"], default="auto"
    )
    args = ap.parse_args()

    sample = make_sample()
    stages = ["audio_cleanup", "scene_detect", "color_grade", "interpolate", "deliver"]
    if args.quick:
        stages.remove("interpolate")

    print(f"• running {len(stages)} stages on {os.path.relpath(sample, ROOT)}")
    started = time.time()
    manifest = run_job(
        JobRequest(
            input_uri=sample,
            stages=stages,
            options={
                "lut": "cinematic_warm",
                "look_strength": 1.0,
                "target_fps": 60,
                "interp_quality": "fast",
                "denoise_model": args.denoise_model,
                "interp_model": args.interp_model,
            },
            output_dir=OUT,
        )
    )
    elapsed = time.time() - started

    print(json.dumps({k: manifest[k] for k in ("job_id", "status", "elapsed_s")}, indent=2))
    for st in manifest["stages"]:
        flag = "ok" if st["status"] == "completed" else "FAIL"
        print(f"  {st['stage']:<14} {flag:<4} {st['duration_s']:>6.2f}s")
        if st["status"] == "failed":
            print(st.get("error"))

    if manifest["status"] != "completed":
        return 1

    art = manifest["artifacts"]
    checks = {
        "master": art.get("master"),
        "proxy": art.get("proxy"),
        "poster": art.get("poster"),
        "scenes": art.get("scenes"),
    }
    for name, path in checks.items():
        ok = bool(path) and os.path.exists(path)
        print(f"  {name:<8} {'✓' if ok else '✗'}  {path}")
        if not ok:
            return 1

    master = ffmpeg.probe(art["master"])
    print(
        f"\nMaster: {master.width}x{master.height} @ {master.fps:.0f} fps · "
        f"{master.duration_s:.2f} s · {master.size_bytes/1e6:.2f} MB · {elapsed:.1f}s total"
    )
    scenes = manifest["stages"][1]["meta"]
    print(f"Scenes: {scenes['scene_count']} detected via {scenes['method']} "
          f"(cuts at {scenes['cuts_s']} s)")

    print(f"\nManifest: {manifest['manifest_path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
