#!/usr/bin/env python3
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


def main() -> int:
    ap = argparse.ArgumentParser(description="fetch FrameFrog model weights")
    ap.add_argument("--force", action="store_true", help="re-download existing files")
    args = ap.parse_args()

    if os.path.exists(RIFE_TARGET) and not args.force:
        print(f"RIFE present: {os.path.relpath(RIFE_TARGET, ROOT)}")
    else:
        download(RIFE_URL, RIFE_TARGET)
        print(f"RIFE saved: {os.path.relpath(RIFE_TARGET, ROOT)}")

    digest = sha256(RIFE_TARGET)
    size = os.path.getsize(RIFE_TARGET)
    print(f"  size:   {size/1e6:.1f} MB")
    print(f"  sha256: {digest}")

    print("\nDeepFilterNet (audio):")
    print("  pip install deepfilternet   # the package fetches its checkpoint on first use")
    print("  optional CPU wheels:        pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu")

    print("\nCapabilities:")
    sys.path.insert(0, ROOT)
    from framefrog.models import model_report

    for name, info in model_report().items():
        print(f"  {name:<14} available={info['available']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
