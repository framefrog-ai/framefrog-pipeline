# GPU validation plan

**Status of this document: a plan, not a report.**

No GPU execution has been performed from this repository. Every number in it
is either a *target to measure* or a *command that will produce the number*.
The repository deliberately stores no example GPU results, and every manifest
produced without a GPU keeps `gpu_validation_status: "pending"` with the GPU
metric fields absent rather than zero.

## 1. Why the GPU phase exists

The CPU path is implemented and tested. Three things cannot be answered
without a real GPU:

1. **Does the CUDA path actually run?** The RIFE ONNX graph has been
   executed on the CPU provider only. The CUDA provider, the NVENC encoder,
   SAM 2 and the TensorRT upscaler have never been executed.
2. **What does it cost?** VRAM per stage, seconds of GPU time per second of
   footage, and the resulting cost per processed video minute on the real
   rate card.
3. **Does it behave on a serverless GPU?** Cold start, scale-to-zero, image
   size, timeout behaviour and whether a job can exceed the platform limits.

Until these are measured, the product must not promise GPU performance.

## 2. What "pending" means in a manifest

```json
{
  "gpu_validation_status": "pending",
  "stages": [
    {"stage": "interpolate", "status": "completed",
     "meta": {"interp_model": "RIFE v4.9 (ONNX Runtime)", "provider": "CPUExecutionProvider"}}
  ]
}
```

The GPU fields (`gpu_name`, `gpu_seconds`, `peak_vram_mb`, `cuda_version`,
`inference_seconds`) are **absent**. An unmeasured value is not a measured
zero, and a zero would be indistinguishable from a real measurement.

A stage that *cannot* run without a GPU never reports `completed`:

```json
{"stage": "interpolate", "status": "pending_gpu_validation",
 "error": "CUDA was requested for RIFE but no CUDA device is available"}
```

## 3. Preconditions

| Requirement | Detail |
|---|---|
| GPU | NVIDIA L40S (or A100) with a driver that matches the CUDA runtime |
| Driver / CUDA | Driver ≥ 525, CUDA 12.1 (matches `Dockerfile.gpu`) |
| Host | `nvidia-smi` visible, `/dev/nvidia*` present in the container |
| Runtimes | `torch` (CUDA build), `onnxruntime-gpu`, `deepfilternet` |
| Weights | `python scripts/download_models.py` (RIFE graph, SAM 2 checkpoint, Real-ESRGAN weights) |
| Storage | An S3-compatible bucket for the input and the outputs |

## 4. Validation sequence

Run the steps in order; each one produces a number that is written into the
commit message or the release notes of the validation branch.

### 4.1 Environment

```bash
python scripts/check_environment.py --json
```

Expect: `cuda_available: true`, a GPU name, a total VRAM figure and a CUDA
version. Save the JSON output — it is the environment the numbers below were
produced in.

### 4.2 GPU smoke test

```bash
python scripts/gpu_smoke_test.py --json
```

The script reports the Python and PyTorch versions, the CUDA runtime status,
the device name, the total memory, loads the configured model, generates a
short clip, interpolates it, verifies the output frame rate with FFprobe,
measures the elapsed time and reports the peak allocated CUDA memory. It exits
non-zero if any check fails. **Its output is the first piece of real GPU
evidence this repository has never had.**

### 4.3 GPU-marked tests

```bash
pytest -m gpu -v
```

`tests/integration/test_gpu.py` asserts that a CUDA device is present, that
torch reports the device, that RIFE runs through the CUDA provider and that
the output reaches the target frame rate, and that the smoke-test script
exits zero.

### 4.4 Stage-by-stage measurements

For each stage, record: input duration, wall-clock time, GPU seconds, peak
VRAM, encoder used, output size.

| Stage | What to measure | Command |
|---|---|---|
| `interpolate` (RIFE, CUDA) | seconds per second of footage, peak VRAM | `python main.py --input clip.mp4 --stages interpolate --interp-model rife --rife-provider cuda` |
| `interpolate` (NVENC encode) | encode time vs libx264, VRAM | `python main.py --input clip.mp4 --stages deliver --encoder nvenc` |
| `rotoscope` (SAM 2) | VRAM per resolution, seconds per shot | `python main.py --input clip.mp4 --stages rotoscope --sam2-checkpoint models/sam2.1_hiera_small.pt` |
| `upscale` (TensorRT / Real-ESRGAN) | VRAM, output quality at 4K | `python main.py --input clip.mp4 --stages upscale --upscale-backend tensorrt` |
| `audio_cleanup` (DeepFilterNet on GPU) | VRAM, real-time factor | `python main.py --input clip.mp4 --stages audio_cleanup --denoise-model deepfilternet` |

### 4.5 Cost per video minute

```bash
FRAMEFROG_GPU_HOURLY_USD=<real rate> python main.py --input clip.mp4 --json
```

The manifest reports measured `gpu_seconds` and `cpu_seconds` per stage and
multiplies them by the configured rate. **Use the provider's real rate card** —
a made-up rate produces a made-up number.

### 4.6 Serverless GPU behaviour

After the Cerebrium deployment exists (`docs/CEREBRIUM_DEPLOYMENT.md`):

1. cold-start time of a container with and without cached weights;
2. time from `POST /v1/jobs` to `completed` for a one-minute clip;
3. behaviour when the job exceeds the platform timeout;
4. scale-to-zero behaviour and whether the SQLite job store survives a
   restart (it does not — a container-local database is lost, which is a known
   gap for the serverless shape).

## 5. Acceptance criteria for the GPU phase

The GPU phase is complete when **all** of the following are true and recorded:

- [ ] `scripts/gpu_smoke_test.py` exits zero on the GPU host, with its output
      attached to the validation record.
- [ ] `pytest -m gpu` passes on the GPU host (no skips).
- [ ] RIFE on CUDA produces the same frame rate as the CPU provider on the
      same clip, measured with FFprobe.
- [ ] Peak VRAM is recorded for RIFE, SAM 2 and the upscaler.
- [ ] NVENC output is verified with FFprobe and compared against libx264.
- [ ] The cost per processed video minute is computed from measured GPU
      seconds and the provider's published rate.
- [ ] A job that explicitly requests CUDA fails with a capability error on a
      CPU-only host (this is already covered by a CPU test, and must stay
      green).
- [ ] `gpu_validation_status` changes from `pending` to `passed` **only** in
      manifests produced by a run that actually used the GPU.

## 6. How the results are recorded

GPU numbers belong in:

1. the pull request that introduces the GPU validation, with the raw script
   output pasted in, and
2. `IMPLEMENTATION_STATUS.md`, where the corresponding rows move from
   `IMPLEMENTED_PENDING_GPU_VALIDATION` to `IMPLEMENTED_AND_CPU_TESTED`
   (or a GPU-specific status), with the date, the hardware and the exact
   command.

They do **not** belong in the README as marketing numbers, and no example
output is stored in the repository.
