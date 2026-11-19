# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Optional model runtimes: DeepFilterNet and RIFE.

Both are optional. The pipeline works without them (FFmpeg filters), and uses
them automatically when present:

* **DeepFilterNet** — ``pip install deepfilternet`` (torch). The official
  package downloads its checkpoint on first use.
* **RIFE** — an ONNX export run with ``onnxruntime``. Fetch it with
  ``python scripts/fetch_models.py`` (21 MB, rife-v4.9 ensemble).

Resolution order for the RIFE graph:

1. ``options["rife_onnx"]`` (explicit path)
2. ``$FRAMEFROG_RIFE_ONNX``
3. ``$FRAMEFROG_MODEL_DIR/rife*.onnx`` (override for deployments)
4. ``assets/models/rife*.onnx`` next to the package

Nothing here downloads at import time — a job never unexpectedly pulls
gigabytes mid-flight.
"""

from __future__ import annotations

import glob
import importlib
import os
from typing import Dict, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.dirname(_HERE)
_MODEL_DIRS = [
    os.path.join(_PKG_ROOT, "assets", "models"),
    os.path.join(_HERE, "assets", "models"),
]


def _model_dirs() -> list[str]:
    dirs = list(_MODEL_DIRS)
    env = os.environ.get("FRAMEFROG_MODEL_DIR")
    if env:
        dirs.insert(0, env)
    return dirs


def find_rife_model(explicit: Optional[str] = None) -> Optional[str]:
    """Return the path to a RIFE ``.onnx`` graph, or None."""
    candidates = [explicit, os.environ.get("FRAMEFROG_RIFE_ONNX")]
    for cand in candidates:
        if cand and os.path.exists(cand):
            return os.path.abspath(cand)
    for d in _model_dirs():
        hits = sorted(glob.glob(os.path.join(d, "rife*.onnx")))
        if hits:
            return os.path.abspath(hits[0])
    return None


def onnxruntime_available() -> bool:
    try:
        importlib.import_module("onnxruntime")
        return True
    except ImportError:
        return False


def rife_available(explicit: Optional[str] = None) -> bool:
    return bool(find_rife_model(explicit)) and onnxruntime_available()


def deepfilternet_available() -> bool:
    """The pip package ``deepfilternet`` imports as ``df``."""
    try:
        importlib.import_module("df")
        return True
    except ImportError:
        return False


def _has(module: str) -> bool:
    try:
        importlib.import_module(module)
        return True
    except ImportError:
        return False


def model_report() -> Dict[str, object]:
    """Capability snapshot for /health and manifests."""
    rife_path = find_rife_model()
    return {
        "deepfilternet": {
            "available": deepfilternet_available(),
            "install": "pip install deepfilternet",
        },
        "rife": {
            "available": rife_available(),
            "onnxruntime": onnxruntime_available(),
            "model": os.path.basename(rife_path) if rife_path else None,
            "model_path": rife_path,
            "install": "python scripts/fetch_models.py",
        },
        "sam2": {
            "available": _has("sam2") and _has("torch"),
            "install": "pip install -r requirements-gpu.txt  # + scripts/fetch_models.py --sam2",
        },
        "upscale": {
            "realesrgan": _has("realesrgan") and _has("basicsr") and _has("torch"),
            "tensorrt": _has("tensorrt") and _has("pycuda"),
            "weights": "Real-ESRGAN .pth or a TensorRT .engine",
        },
    }
