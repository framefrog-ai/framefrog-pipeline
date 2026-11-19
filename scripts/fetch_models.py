#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Fetch the optional model weights used by the model-backed stages.

    python scripts/fetch_models.py            # RIFE v4.9 ONNX (21 MB)
    python scripts/fetch_models.py --force    # re-download

DeepFilterNet is not a file download — it is the `deepfilternet` pip package,
which fetches its own checkpoint on first use:

    pip install deepfilternet

We deliberately do not vendor model weights into git; this script is the
reproducible fetch step. Every download is written atomically and its SHA-256
is printed so a manifest can record exactly which graph ran.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tempfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_DIR = os.path.join(ROOT, "assets", "models")

RIFE_URL = (
    "https://huggingface.co/yuvraj108c/rife-onnx/resolve/main/"
    "rife49_ensemble_True_scale_1_sim.onnx"
)
RIFE_TARGET = os.path.join(MODEL_DIR, "rife-v4.9-ensemble.onnx")

REALESRGAN_URL = (
    "https://github.com/xinntao/Real-ESRGAN/releases/download/"
    "v0.2.5.0/RealESRGAN_x4plus.pth"
)
REALESRGAN_TARGET = os.path.join(MODEL_DIR, "RealESRGAN_x4plus.pth")

SAM2_BASE = "https://dl.fbaipublicfiles.com/segment_anything_2/092824/"
SAM2_URLS = {
    "tiny": SAM2_BASE + "sam2.1_hiera_tiny.pt",
    "small": SAM2_BASE + "sam2.1_hiera_small.pt",
    "base_plus": SAM2_BASE + "sam2.1_hiera_base_plus.pt",
    "large": SAM2_BASE + "sam2.1_hiera_large.pt",
}


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, target: str) -> str:
    os.makedirs(os.path.dirname(target), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target), suffix=".part")
    os.close(fd)
    print(f"↓ {url}")
    try:
        with urllib.request.urlopen(url, timeout=120) as resp, open(tmp, "wb") as out:
            total = int(resp.headers.get("content-length") or 0)
            done = 0
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if total:
                    pct = 100 * done / total
                    print(f"\r  {done/1e6:6.1f} / {total/1e6:.1f} MB  ({pct:5.1f}%)", end="")
        print()
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return target


def _fetch(label: str, url: str, target: str, force: bool) -> None:
    if os.path.exists(target) and not force:
        print(f"{label} present: {os.path.relpath(target, ROOT)}")
    else:
        download(url, target)
        print(f"{label} saved: {os.path.relpath(target, ROOT)}")
    print(f"  size:   {os.path.getsize(target)/1e6:.1f} MB")
    print(f"  sha256: {sha256(target)}")


def main() -> int:
    ap = argparse.ArgumentParser(description="fetch FrameFrog model weights")
    ap.add_argument("--force", action="store_true", help="re-download existing files")
    ap.add_argument("--rife", action="store_true", default=False, help="also fetch RIFE (default with no flags)")
    ap.add_argument("--realesrgan", action="store_true", help="fetch Real-ESRGAN x4 weights (~64 MB)")
    ap.add_argument(
        "--sam2", choices=["none", "tiny", "small", "base_plus", "large"], default="none",
        help="fetch a SAM 2.1 checkpoint (small ~150 MB, large ~900 MB)",
    )
    args = ap.parse_args()
    any_flag = args.rife or args.realesrgan or args.sam2 != "none"
    fetch_rife = args.rife or not any_flag

    if fetch_rife:
        _fetch("RIFE v4.9", RIFE_URL, RIFE_TARGET, args.force)
    if args.realesrgan:
        _fetch("Real-ESRGAN x4plus", REALESRGAN_URL, REALESRGAN_TARGET, args.force)
    if args.sam2 != "none":
        target = os.path.join(MODEL_DIR, f"sam2.1_hiera_{args.sam2}.pt")
        _fetch(f"SAM 2.1 {args.sam2}", SAM2_URLS[args.sam2], target, args.force)

    print("\nDeepFilterNet (audio):")
    print("  pip install deepfilternet   # the package fetches its checkpoint on first use")
    print("\nGPU stages:")
    print("  pip install -r requirements-gpu.txt")
    print("  pip install git+https://github.com/facebookresearch/sam2.git")
    print("  rotoscope:  --sam2-checkpoint assets/models/sam2.1_hiera_small.pt")
    print("  upscale:    --upscale-model assets/models/RealESRGAN_x4plus.pth")
    print("  TensorRT:   trtexec --onnx=<model.onnx> --saveEngine=upscale.engine --fp16")
    print("  optional CPU wheels:        pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu")

    print("\nCapabilities:")
    sys.path.insert(0, ROOT)
    from framefrog.models import model_report

    for name, info in model_report().items():
        print(f"  {name:<14} available={info.get('available', 'n/a')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
