"""TensorRT inference for the 4K upscaler.

This is deliberately a *thin*, contract-driven runner rather than a bundle of
pretrained engines. You bring an engine built from an upscaling network; the
runner expects:

* one input  — ``float32`` RGB, NCHW, values in ``[0, 1]``
* one output — RGB, NCHW, ``float32`` in ``[0, 1]`` or ``uint8``

Dynamic spatial axes are supported: the runner sets ``(1, 3, H, W)`` on the
input and reads the output shape back. Engines with dynamic **batch** or
channel axes are rejected.

Requires ``tensorrt`` and ``pycuda`` (``requirements-gpu.txt``). The same
chapter of the README explains how to build an engine with ``trtexec``.
"""

from __future__ import annotations

import os
from typing import Dict, Optional, Tuple

import numpy as np


class TensorRTRunnerError(RuntimeError):
    """Engine missing, incompatible, or the runtime is not installed."""


class TensorRTUpscaler:
    def __init__(self, engine_path: str, device: int = 0) -> None:
        if not os.path.exists(engine_path):
            raise TensorRTRunnerError(f"TensorRT engine not found: {engine_path}")
        try:
            import tensorrt as trt  # type: ignore
            import pycuda.driver as cuda  # type: ignore
        except ImportError as exc:
            raise TensorRTRunnerError(
                "TensorRT upscaling requires `tensorrt` and `pycuda`; "
                "install requirements-gpu.txt on an NVIDIA box"
            ) from exc

        self._trt = trt
        self._cuda = cuda
        cuda.init()
        self._device_context = cuda.Device(device).make_context()
        try:
            logger = trt.Logger(trt.Logger.WARNING)
            with open(engine_path, "rb") as fh:
                engine = trt.Runtime(logger).deserialize_cuda_engine(fh.read())
            if engine is None:
                raise TensorRTRunnerError(f"could not deserialize engine: {engine_path}")
            self._engine = engine
            self._context = engine.create_execution_context()
            self._stream = cuda.Stream()
            self._engine_path = engine_path

            self._input: Optional[Tuple[str, tuple]] = None
            self._output: Optional[Tuple[str, tuple]] = None
            for i in range(engine.num_io_tensors):
                name = engine.get_tensor_name(i)
                mode = engine.get_tensor_mode(name)
                shape = tuple(engine.get_tensor_shape(name))
                if mode == trt.TensorIOMode.INPUT:
                    if self._input is not None:
                        raise TensorRTRunnerError("engine must have exactly one input")
                    self._input = (name, shape)
                else:
                    if self._output is not None:
                        raise TensorRTRunnerError("engine must have exactly one output")
                    self._output = (name, shape)
            if not self._input or not self._output:
                raise TensorRTRunnerError("engine must expose one input and one output")
            self._buffers: Dict[tuple, dict] = {}
        except Exception:
            self.close()
            raise

    # -- lifecycle --------------------------------------------------------- #
    def close(self) -> None:
        for buf in getattr(self, "_buffers", {}).values():
            for key in ("in_dev", "out_dev"):
                ptr = buf.get(key)
                if ptr is not None:
                    try:
                        ptr.free()
                    except Exception:
                        pass
        self._buffers = {}
        ctx = getattr(self, "_device_context", None)
        if ctx is not None:
            try:
                ctx.pop()
            except Exception:
                pass
            self._device_context = None

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- inference --------------------------------------------------------- #
    def _buffers_for(self, height: int, width: int) -> dict:
        key = (height, width)
        if key in self._buffers:
            return self._buffers[key]

        trt = self._trt
        cuda = self._cuda
        in_name, _ = self._input
        self._context.set_input_shape(in_name, (1, 3, height, width))
        in_shape = tuple(self._context.get_tensor_shape(in_name))
        out_name, _ = self._output
        out_shape = tuple(self._context.get_tensor_shape(out_name))
        if any(dim < 1 for dim in in_shape) or any(dim < 1 for dim in out_shape):
            raise TensorRTRunnerError(
                f"engine did not resolve concrete shapes for {height}x{width}: "
                f"in={in_shape} out={out_shape}"
            )

        in_dtype = trt.nptype(self._engine.get_tensor_dtype(in_name))
        out_dtype = trt.nptype(self._engine.get_tensor_dtype(out_name))
        in_host = np.empty(in_shape, dtype=in_dtype)
        out_host = np.empty(out_shape, dtype=out_dtype)
        in_dev = cuda.mem_alloc(in_host.nbytes)
        out_dev = cuda.mem_alloc(out_host.nbytes)
        buf = {
            "in_name": in_name,
            "out_name": out_name,
            "in_shape": in_shape,
            "out_shape": out_shape,
            "in_host": in_host,
            "out_host": out_host,
            "in_dev": in_dev,
            "out_dev": out_dev,
        }
        self._buffers[key] = buf
        return buf

    def infer(self, bgr: "np.ndarray") -> "np.ndarray":
        """Upscale one BGR uint8 frame; returns BGR uint8."""
        if bgr.ndim != 3 or bgr.shape[2] != 3:
            raise TensorRTRunnerError(f"expected HxWx3 frame, got {bgr.shape}")
        height, width = int(bgr.shape[0]), int(bgr.shape[1])
        buf = self._buffers_for(height, width)
        cuda = self._cuda

        rgb = bgr[:, :, ::-1].astype(np.float32) / 255.0
        np.copyto(buf["in_host"], rgb.transpose(2, 0, 1)[None, ...])
        cuda.memcpy_htod_async(buf["in_dev"], buf["in_host"], self._stream)
        self._context.set_tensor_address(buf["in_name"], int(buf["in_dev"]))
        self._context.set_tensor_address(buf["out_name"], int(buf["out_dev"]))
        self._context.execute_async_v3(stream_handle=self._stream.handle)
        cuda.memcpy_dtoh_async(buf["out_host"], buf["out_dev"], self._stream)
        self._stream.synchronize()

        out = np.asarray(buf["out_host"]).reshape(buf["out_shape"])[0]
        out = out.transpose(1, 2, 0)
        if out.dtype != np.uint8:
            out = np.clip(out * 255.0, 0, 255).astype(np.uint8)
        return out[:, :, ::-1].copy()

    def expected_input_size(self) -> Optional[Tuple[int, int]]:
        """Return ``(width, height)`` for a static engine, else ``None``."""
        shape = self._input[1] if self._input else ()
        if len(shape) == 4 and shape[2] > 0 and shape[3] > 0:
            return (int(shape[3]), int(shape[2]))
        return None

    def describe(self) -> dict:
        return {
            "engine": os.path.basename(self._engine_path),
            "input": {"name": self._input[0], "shape": list(self._input[1])},
            "output": {"name": self._output[0], "shape": list(self._output[1])},
        }
