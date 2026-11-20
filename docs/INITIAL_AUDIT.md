# Initial audit — `framefrog-ai/framefrog-pipeline`

Audit date: 2026-11-19
Branch: `feat/gpu-ready-pre-mvp`
Audited commit: `13d7ef1` (`chore(spdx): add reproducible SPDX verification and enforce it in CI`)

This document records the state of the repository *before* the GPU-ready pre-MVP
work started. It is a snapshot, not a live report: later commits change many
of the findings, and `IMPLEMENTATION_STATUS.md` at the repository root is the
authoritative status document.

---

## 1. Repository shape at audit time

```
50 tracked files, ~5.9k lines of Python
app code:      framefrog/ (package), main.py, demo.py
stages:        framefrog/stages/{audio,scenes,grade,interpolate,upscale,rotoscope,deliver}.py
support:       framefrog/{ffmpeg,models,ops,storage,fanout,queue,worker,lut,tensorrt_runner}.py
http:          framefrog/api.py  (FastAPI, unversioned routes)
tests:         tests/  (11 files, 56 tests, all passing on CPU)
ci:            .github/workflows/ci.yml (pytest + SPDX check only)
deploy:        Dockerfile (CPU), cerebrium.toml
docs:          README.md, docs/nvidia-host.md
```

Git history: 15 commits on `main`, from the original scaffold (`bbb4dcd`) to
GPU-path work (SAM 2, TensorRT, NVENC, fan-out, cost accounting, SPDX hygiene).
Working tree was clean; `origin/main` was up to date.

## 2. What was genuinely working

Verified by reading the code and by running the existing suite
(`56 passed in 6.21s`):

| Area | Evidence |
|---|---|
| FFmpeg/FFprobe execution | `framefrog/ffmpeg.py` builds argument lists and runs them with `subprocess.run` — no `shell=True` anywhere in the repository |
| Scene detection | `framefrog/stages/scenes.py` runs PySceneDetect `ContentDetector` with an OpenCV histogram fallback |
| LUT generation | `framefrog/lut.py` generates deterministic Adobe `.cube` files; `assets/luts/cinematic_warm.cube` is committed |
| LUT application | `framefrog/stages/grade.py` applies `lut3d` through FFmpeg |
| Delivery | `framefrog/stages/deliver.py` writes an H.264/AAC master, a 720p proxy and a poster frame |
| Audio cleanup | `framefrog/stages/audio.py` extracts WAV, runs DeepFilterNet3 when installed, loudnorms to −16 LUFS, remuxes; FFmpeg fallback otherwise |
| Interpolation | `framefrog/stages/interpolate.py` drives a RIFE v4.9 ONNX graph (CPU/CUDA provider selection) with `minterpolate` fallback |
| Storage | `framefrog/storage.py` downloads/uploads `s3://` objects via boto3 |
| Local orchestration | `framefrog/pipeline.py` runs stages in order and writes an honest manifest, including per-stage failures |

## 3. Gaps against the required pre-MVP scope

These were the substantive gaps found during the audit.

1. **No persistent job tracking.** Job state lived in a process-local
   `dict` (`framefrog/api.py: JOBS`) and in `manifest.json` files on disk.
   There was no database, no idempotency key, no cancellation flag and no
   explicit state machine (`queued → acquiring_input → validating → processing
   → uploading → completed/failed/cancelled`).
2. **Unversioned API.** Routes were `/post_production`, `/jobs/{id}`,
   `/jobs/{id}/download`. No `/v1` prefix, no `/ready`, no
   `POST /v1/jobs/{id}/cancel`, no `GET /v1/jobs/{id}/outputs`.
3. **No typed configuration.** Everything was read ad hoc from `os.environ`
   (`FRAMEFROG_OUTPUT_DIR`, `FRAMEFROG_RIFE_ONNX`, `FRAMEFROG_MODEL_DIR`, …).
   There was no `pydantic-settings` model and no startup validation.
4. **No authentication.** The API was completely open.
5. **No SSRF or path-traversal controls.** `input_uri` was accepted verbatim;
   a local path was resolved with `os.path.abspath` and a relative path could
   point anywhere on the filesystem. There was no restriction on URI schemes
   and no blocking of loopback/private/link-local/metadata destinations.
6. **No media validation.** Duration, resolution, file size, FPS and stream
   presence were never checked against limits; the pipeline probed the file but
   only recorded what it found.
7. **No concurrency limit.** FastAPI `BackgroundTasks` started one unbounded
   thread per request.
8. **No structured logging.** Logs were plain `logging` calls with free-form
   messages; there was no event/job/stage/duration/error_code envelope.
9. **No request/response schemas.** `options` was an untyped `Dict`, so
   invalid stage names, codecs, FPS values or URIs could only be caught deep
   inside a job.
10. **Incomplete storage abstraction.** No storage interface, no prefixes
    (input/intermediate/output), no presigned upload or download URLs, no
    expiry handling, no key sanitisation.
11. **No tooling scripts.** There was `scripts/fetch_models.py` and
    `scripts/check_spdx.py`, but no `check_environment.py`,
    `generate_test_video.py`, `download_models.py` or `gpu_smoke_test.py`.
12. **No Ruff / MyPy.** The CI workflow ran `compileall`, the SPDX check and
    pytest only.
13. **No `pyproject.toml`.** Dependencies lived in three loose
    `requirements*.txt` files with no optional-dependency groups.
14. **No `.env.example`** and no documented configuration surface.
15. **Docker image ran as root** and had no health check.
16. **`cerebrium.toml` was minimal**: no system packages beyond `ffmpeg`, no
    environment variables, no timeout/concurrency/GPU selection, and its
    `entrypoint` (`main:post_production`) pointed at the CLI function rather
    than the HTTP surface.
17. **No `IMPLEMENTATION_STATUS.md`**, no `docs/ARCHITECTURE.md`, no
    `docs/GPU_VALIDATION.md`, no `docs/CEREBRIUM_DEPLOYMENT.md`, no
    `SECURITY.md`.
18. **GPU claims.** The README and `docs/nvidia-host.md` described GPU paths
    (SAM 2, TensorRT, NVENC, RIFE-on-CUDA) that could not be executed on the
    available CPU-only machine. Nothing in the repository proved CUDA
    behaviour, and the manifests had no way to say "this stage has not been
    validated on a GPU".

## 4. Pattern searches (result)

| Pattern | Result |
|---|---|
| `TODO`, `FIXME`, `XXX`, `HACK` | none |
| `NotImplementedError` | none |
| `placeholder` | none |
| `mock` / `fake` | none in product code (no mocking framework in use) |
| `shell=True` | none — all subprocess calls use argument lists |
| hard-coded credentials | none (only env-var *names* and one test-local secret literal) |
| `time.sleep` | 3 occurrences, all legitimate (queue poll interval, webhook backoff, fan-out settle) — none simulate processing |
| bare `pass` | only in `except` blocks that intentionally ignore a cleanup failure |
| copied media reported as processed | none: the audio stage records `denoise_model: "ffmpeg"` and a `fallback_reason` when the neural model is unavailable |
| hard-coded completed results | none: stage failures are recorded and the manifest status becomes `failed` |

## 5. Environment at audit time

| Item | State |
|---|---|
| Python | 3.14.7 (project targets 3.11) |
| FFmpeg / FFprobe | 8.1.1, with `libx264` and `aac` |
| CUDA GPU | **absent** — `nvidia-smi` not present, no `/dev/nvidia*` |
| `onnxruntime` | 1.32.0 (CPU) |
| `torch`, `deepfilternet`, `sam2` | not installed |
| `scenedetect`, `boto3` | not installed in the system interpreter |
| Docker | daemon reachable |
| `ruff`, `mypy` | not installed (added during this work in a local venv) |

## 6. Licensing of external dependencies

No upstream code or model weights are vendored in the repository. External
components are consumed as packages or downloaded at runtime:

| Component | License | Redistribution | Use in this repo |
|---|---|---|---|
| FFmpeg | LGPL/GPL depending on build | system binary | invoked as a subprocess |
| PySceneDetect | BSD-3-Clause | pip package | imported |
| DeepFilterNet | Apache-2.0 / MIT (checkpoint terms apply) | pip package + downloaded checkpoint | imported, optional |
| RIFE v4.9 ONNX export | RIFE is MIT-licensed; the specific ONNX export in `yuvraj108c/rife-onnx` is fetched at runtime, not vendored | downloaded by `scripts/download_models.py` | loaded with onnxruntime |
| SAM 2 | Apache-2.0 | pip install from git | optional GPU stage |
| Real-ESRGAN / basicsr | BSD-3-Clause (basicsr) / MIT | pip package | optional GPU stage |
| boto3 / botocore | Apache-2.0 | pip package | imported |

The committed `assets/luts/*.cube` files are generated by this repository's own
code (`app/media/lut.py` after the restructure) and carry the repository's own
AGPL-3.0 license.

## 7. Plan of record

1. Restructure `framefrog/` into `app/` with the required responsibility split
   (`api/`, `core/`, `jobs/`, `media/`, `stages/`, `storage/`), keeping the
   proven FFmpeg/LUT/stage logic and adding the missing subsystems.
2. Add typed configuration, structured logging, error taxonomy, security
   controls (API key, SSRF, path safety), validated request schemas.
3. Add SQLite-backed job persistence, an in-process background runner with a
   concurrency limit, idempotency and cancellation.
4. Add media validation, storage interfaces (local + S3 with prefixes and
   presigned URLs).
5. Add scripts, tests (unit + integration, GPU-marked and skipped without
   CUDA), Ruff/MyPy config, CI, Docker images, Cerebrium configuration.
6. Write the documentation set, with per-feature status and exact next
   validation commands.

No GPU-dependent behaviour is claimed as validated anywhere in this work.
