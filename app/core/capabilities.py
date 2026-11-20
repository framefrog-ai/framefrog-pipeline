# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Runtime capability probes.

Everything here answers "what can this process actually do right now?" by
asking the machine — never by guessing. A probe either observed the fact or
reports ``available: false`` with the reason.

GPU facts (device name, CUDA version, memory) are only ever populated when
``torch`` reports them. Before GPU validation the manifest keeps
``gpu_validation_status = "pending"`` and the GPU metric fields are absent.
"""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
from typing import Any

#: The only honest value for GPU metrics before a GPU run.
GPU_VALIDATION_PENDING = "pending"
GPU_VALIDATION_PASSED = "passed"
GPU_VALIDATION_FAILED = "failed"


def _module_available(name: str) -> bool:
    try:
        importlib.import_module(name)
        return True
    except Exception:
        return False


def ffmpeg_info(ffmpeg_path: str = "ffmpeg") -> dict[str, Any]:
    """Version and encoder availability of the local FFmpeg."""
    binary = shutil.which(ffmpeg_path) or (ffmpeg_path if os.path.isfile(ffmpeg_path) else None)
    if not binary:
        return {"available": False, "reason": f"{ffmpeg_path} not found on PATH"}
    try:
        proc = subprocess.run([binary, "-version"], capture_output=True, text=True, timeout=20, check=False)
    except Exception as exc:
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
    first = (proc.stdout or "").splitlines()[0] if proc.stdout else ""
    encoders: dict[str, bool] = {}
    try:
        enc_proc = subprocess.run(
            [binary, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=20, check=False
        )
        text = enc_proc.stdout or ""
    except Exception:
        text = ""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0][:1] in {"V", "A"}:
            encoders[parts[1]] = True
    return {
        "available": True,
        "path": binary,
        "version": first.strip(),
        "encoders": {
            "libx264": "libx264" in encoders,
            "libx265": "libx265" in encoders,
            "aac": "aac" in encoders,
            "h264_nvenc": "h264_nvenc" in encoders,
            "hevc_nvenc": "hevc_nvenc" in encoders,
        },
    }


def ffprobe_info(ffprobe_path: str = "ffprobe") -> dict[str, Any]:
    binary = shutil.which(ffprobe_path) or (ffprobe_path if os.path.isfile(ffprobe_path) else None)
    if not binary:
        return {"available": False, "reason": f"{ffprobe_path} not found on PATH"}
    try:
        proc = subprocess.run([binary, "-version"], capture_output=True, text=True, timeout=20, check=False)
        first = (proc.stdout or "").splitlines()[0] if proc.stdout else ""
    except Exception as exc:
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
    return {"available": True, "path": binary, "version": first.strip()}


def deepfilternet_capability() -> dict[str, Any]:
    """Whether the DeepFilterNet runtime can be imported."""
    if not _module_available("df"):
        return {
            "available": False,
            "package": "deepfilternet",
            "reason": "deepfilternet is not installed (pip install 'framefrog[audio]')",
        }
    return {"available": True, "package": "deepfilternet", "reason": None}


def onnxruntime_capability() -> dict[str, Any]:
    """Whether onnxruntime can be imported (and which providers it offers)."""
    if not _module_available("onnxruntime"):
        return {
            "available": False,
            "package": "onnxruntime",
            "reason": "onnxruntime is not installed (pip install 'framefrog[models]')",
        }
    providers: list[str] = []
    try:
        import onnxruntime  # type: ignore

        providers = list(onnxruntime.get_available_providers())
    except Exception as exc:  # pragma: no cover - import worked, call failed
        return {"available": False, "package": "onnxruntime", "reason": str(exc)}
    return {
        "available": True,
        "package": "onnxruntime",
        "providers": providers,
        "cuda_provider": "CUDAExecutionProvider" in providers,
        "reason": None,
    }


def torch_capability() -> dict[str, Any]:
    """Torch / CUDA facts, reported exactly as torch sees them."""
    if not _module_available("torch"):
        return {
            "available": False,
            "torch_installed": False,
            "cuda_available": False,
            "reason": "torch is not installed",
        }
    try:
        import torch  # type: ignore
    except Exception as exc:  # pragma: no cover
        return {"available": False, "torch_installed": True, "cuda_available": False, "reason": str(exc)}

    cuda_available = bool(torch.cuda.is_available())
    info: dict[str, Any] = {
        "available": True,
        "torch_installed": True,
        "torch_version": getattr(torch, "__version__", None),
        "cuda_available": cuda_available,
        "reason": None,
    }
    if cuda_available:
        try:
            props = torch.cuda.get_device_properties(0)
            info.update(
                {
                    "gpu_name": props.name,
                    "cuda_version": torch.version.cuda,
                    "total_vram_mb": round(props.total_memory / (1024**2), 1),
                    "device_count": torch.cuda.device_count(),
                }
            )
        except Exception as exc:  # pragma: no cover - driver dependent
            info["reason"] = f"CUDA present but device query failed: {exc}"
    return info


def nvidia_smi_info() -> dict[str, Any]:
    """Raw ``nvidia-smi`` facts when the binary exists."""
    binary = shutil.which("nvidia-smi")
    if not binary:
        return {"available": False, "reason": "nvidia-smi not found"}
    try:
        proc = subprocess.run(
            [binary, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except Exception as exc:
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
    if proc.returncode != 0:
        return {"available": False, "reason": (proc.stderr or "").strip()[:200] or "nvidia-smi failed"}
    gpus = []
    for line in (proc.stdout or "").splitlines():
        if not line.strip():
            continue
        name, _, memory = line.partition(",")
        gpus.append({"name": name.strip(), "memory_total_mb": memory.strip()})
    return {"available": bool(gpus), "gpus": gpus, "reason": None if gpus else "no GPU reported"}


def cuda_available() -> bool:
    """True only when torch reports a usable CUDA device."""
    return bool(torch_capability().get("cuda_available"))


def report(ffmpeg_path: str = "ffmpeg", ffprobe_path: str = "ffprobe") -> dict[str, Any]:
    """Full capability snapshot used by ``/health``, ``/ready`` and manifests."""
    ff = ffmpeg_info(ffmpeg_path)
    fp = ffprobe_info(ffprobe_path)
    torch_info = torch_capability()
    return {
        "ffmpeg": ff,
        "ffprobe": fp,
        "scenedetect": {
            "available": _module_available("scenedetect"),
            "package": "scenedetect",
            "reason": None if _module_available("scenedetect") else "scenedetect is not installed",
        },
        "deepfilternet": deepfilternet_capability(),
        "onnxruntime": onnxruntime_capability(),
        "torch": torch_info,
        "nvidia_smi": nvidia_smi_info(),
        "cuda_available": bool(torch_info.get("cuda_available")),
        "sam2": {"available": _module_available("sam2"), "package": "sam2"},
        "tensorrt": {"available": _module_available("tensorrt"), "package": "tensorrt"},
    }


def gpu_metric_fields(status: str = GPU_VALIDATION_PENDING) -> dict[str, Any]:
    """GPU metric fields for a manifest.

    Before GPU validation the measurement fields are *absent* rather than
    zero: an unmeasured value is not a measured zero.
    """
    fields: dict[str, Any] = {"gpu_validation_status": status}
    if status == GPU_VALIDATION_PASSED:
        fields.update(
            {
                "gpu_name": None,
                "gpu_seconds": None,
                "peak_vram_mb": None,
                "cuda_version": None,
                "inference_seconds": None,
            }
        )
    return fields
