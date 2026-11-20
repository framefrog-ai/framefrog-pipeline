# Implementation status

Generated for the `feat/gpu-ready-pre-mvp` branch. Every row states what was
actually built, how it was verified, and the exact command that closes the
remaining gap.

## Status vocabulary

| Status | Meaning |
|---|---|
| `IMPLEMENTED_AND_CPU_TESTED` | Implemented and exercised by a test that ran on this CPU machine |
| `IMPLEMENTED_PENDING_GPU_VALIDATION` | Implemented, but the GPU path has not been executed (no CUDA device available) |
| `IMPLEMENTED_PENDING_EXTERNAL_RUNTIME_VALIDATION` | Implemented, but the runtime/model could not be executed in this environment |
| `CONFIGURED_PENDING_CLOUD_VALIDATION` | Configuration exists; the platform has not been exercised against it |
| `BLOCKED` | Cannot proceed without something outside the repository |
| `NOT_IMPLEMENTED` | Deliberately out of scope for this phase |

No row in this document claims a GPU, cloud or benchmark result that was not
produced by a command in this repository.

---

## 1. Application, configuration and API

| Feature | Status | Test status | Required hardware | External service | Known limitations | Next validation command |
|---|---|---|---|---|---|---|
| Application startup (`app.main:create_app`, `uvicorn app.main:app`) | IMPLEMENTED_AND_CPU_TESTED | `tests/integration/test_api.py` | CPU | none | In-process runner only; a queue consumer replaces it later | `pytest tests/integration/test_api.py -v` |
| Typed configuration (`app/core/config.py`, pydantic-settings) | IMPLEMENTED_AND_CPU_TESTED | `tests/unit/test_config.py` | CPU | none | PostgreSQL URL accepted but not implemented | `pytest tests/unit/test_config.py -v` |
| Startup configuration validation | IMPLEMENTED_AND_CPU_TESTED | `tests/unit/test_config.py` | CPU | none | — | `python scripts/check_environment.py` |
| Versioned API (`/v1/jobs`, `/v1/jobs/{id}`, `/v1/jobs/{id}/cancel`, `/v1/jobs/{id}/outputs`) | IMPLEMENTED_AND_CPU_TESTED | `tests/integration/test_api.py` | CPU | none | No pagination/filtering yet | `pytest tests/integration/test_api.py -v` |
| Liveness / readiness (`/health`, `/ready`) | IMPLEMENTED_AND_CPU_TESTED | `tests/integration/test_api.py` | CPU | none | `/ready` does not probe the model runtimes | `curl localhost:8080/ready` |
| Request/response schemas with validation | IMPLEMENTED_AND_CPU_TESTED | `tests/unit/test_schemas.py` | CPU | none | Partial LUT intensity is rejected rather than implemented | `pytest tests/unit/test_schemas.py -v` |
| Per-stage `enabled` switches and delivery settings | IMPLEMENTED_AND_CPU_TESTED | `tests/integration/test_options_contract.py` | CPU | none | Only `audio_cleanup`, `interpolate` and `color_grade` have a switch | `pytest tests/integration/test_options_contract.py -v` |
| Structured error taxonomy (`app/core/errors.py`) | IMPLEMENTED_AND_CPU_TESTED | `tests/unit/test_errors.py` | CPU | none | — | `pytest tests/unit/test_errors.py -v` |
| Structured JSON logging | IMPLEMENTED_AND_CPU_TESTED | `tests/unit/test_logging.py` | CPU | none | Log shipping is left to the platform | `pytest tests/unit/test_logging.py -v` |
| API authentication (API key, constant-time compare) | IMPLEMENTED_AND_CPU_TESTED | `tests/unit/test_security.py`, `tests/integration/test_api.py` | CPU | none | A single shared key; no per-user identity | `pytest -k "api_key or auth" -v` |
| SSRF and path-safety controls | IMPLEMENTED_AND_CPU_TESTED | `tests/unit/test_security.py` | CPU | none | HTTP inputs are disabled entirely today | `pytest tests/unit/test_security.py -v` |

## 2. Job system

| Feature | Status | Test status | Required hardware | External service | Known limitations | Next validation command |
|---|---|---|---|---|---|---|
| Persistent job storage (SQLite, WAL, unique idempotency index) | IMPLEMENTED_AND_CPU_TESTED | `tests/unit/test_repository.py`, `tests/integration/test_runner.py` | CPU | none | Single node; SQLite only | `pytest tests/unit/test_repository.py -v` |
| Repository interface (PostgreSQL-ready) | IMPLEMENTED_AND_CPU_TESTED | `tests/unit/test_repository.py` | CPU | none | No PostgreSQL implementation exists | `pytest tests/unit/test_repository.py -v` |
| In-process background runner with bounded concurrency | IMPLEMENTED_AND_CPU_TESTED | `tests/integration/test_runner.py` | CPU | none | Threads, not processes; no distributed queue | `pytest tests/integration/test_runner.py -v` |
| Job state machine and legal transitions | IMPLEMENTED_AND_CPU_TESTED | `tests/unit/test_states.py` | CPU | none | — | `pytest tests/unit/test_states.py -v` |
| Idempotency keys | IMPLEMENTED_AND_CPU_TESTED | `tests/integration/test_api.py` | CPU | none | Keys are unique per database, not per caller | `pytest -k idempotency -v` |
| Cancellation (flag checked between stages and inside RIFE) | IMPLEMENTED_AND_CPU_TESTED | `tests/integration/test_runner.py`, `tests/integration/test_api.py` | CPU | none | A running FFmpeg process is not interrupted mid-encode | `pytest -k cancel -v` |
| Deterministic stage order and per-job temp directory | IMPLEMENTED_AND_CPU_TESTED | `tests/integration/test_pipeline.py` | CPU | none | Intermediates are kept after the run (no auto-clean yet) | `pytest tests/integration/test_pipeline.py -v` |
| Per-shot fan-out over a file/SQS queue | IMPLEMENTED_AND_CPU_TESTED | `tests/unit/test_fanout.py`, `tests/unit/test_queue.py`, `tests/integration/test_fanout_request_path.py` | CPU | SQS for the SQS backend (not exercised live) | The SQS backend was never run against a real queue | `pytest tests/unit/test_queue.py -v` |
| Fan-out in the request path (`options.fanout.mode`) | IMPLEMENTED_AND_CPU_TESTED | `tests/integration/test_fanout_request_path.py` | CPU | none | `auto` degrades to the monolithic stage and records why; `shots` fails the job | `pytest tests/integration/test_fanout_request_path.py -v` |

## 3. Media, stages and delivery

| Feature | Status | Test status | Required hardware | External service | Known limitations | Next validation command |
|---|---|---|---|---|---|---|
| FFmpeg wrapper (argument lists, timeouts, output checks) | IMPLEMENTED_AND_CPU_TESTED | `tests/unit/test_ffmpeg.py` | CPU + FFmpeg | none | Per-command timeout is 30 min | `pytest tests/unit/test_ffmpeg.py -v` |
| FFprobe integration (rational fps, streams, size) | IMPLEMENTED_AND_CPU_TESTED | `tests/unit/test_ffprobe.py` | CPU + FFmpeg | none | — | `pytest tests/unit/test_ffprobe.py -v` |
| Media validation against the configured limits | IMPLEMENTED_AND_CPU_TESTED | `tests/integration/test_pipeline.py` | CPU | none | Limits are global, not per-tenant | `pytest -k media_validation -v` |
| Scene detection (PySceneDetect `ContentDetector`) | IMPLEMENTED_AND_CPU_TESTED | `tests/integration/test_pipeline.py` (synthetic cut), `tests/unit/test_scene_detection.py` | CPU | none | OpenCV fallback is coarser | `pytest tests/integration/test_pipeline.py -k scene -v` |
| Colour grading (`.cube` LUT via `lut3d`) | IMPLEMENTED_AND_CPU_TESTED | `tests/integration/test_pipeline.py`, `tests/unit/test_lut_validation.py` | CPU | none | Intensity must be 1.0; partial blending is rejected, not implemented | `pytest -k lut -v` |
| Delivery (H.264/AAC MP4, `+faststart`, poster, proxy) | IMPLEMENTED_AND_CPU_TESTED | `tests/integration/test_pipeline.py` | CPU | none | Loudness target is applied in the audio stage, not re-checked at delivery | `pytest -k deliver -v` |
| Audio cleanup — FFmpeg spectral fallback path | IMPLEMENTED_AND_CPU_TESTED | `tests/integration/test_pipeline.py` | CPU | none | Spectral denoise is not speech enhancement | `pytest -k audio -v` |
| Audio cleanup — DeepFilterNet3 adapter | IMPLEMENTED_PENDING_EXTERNAL_RUNTIME_VALIDATION | adapter boundary unit-tested (`tests/unit/test_capabilities.py`); **the model has not been executed in this environment** (`deepfilternet` is not installed and torch is absent) | CPU (or GPU) | none | The adapter is complete; the neural run is unverified | `pip install -e ".[audio]" && python -c "from df.enhance import init_df; init_df()"` then `pytest -k audio -v` |
| Frame interpolation — RIFE v4.9 ONNX, CPU provider | IMPLEMENTED_AND_CPU_TESTED (CPU only) | `tests/integration/test_rife_cpu.py` — real inference on the local 21 MB graph: the output reaches the target frame rate and no frame is blended across a cut | CPU | none | The graph is downloaded, not committed | `pytest tests/integration/test_rife_cpu.py -v` |
| Frame interpolation — RIFE v4.9 ONNX, CUDA provider | IMPLEMENTED_PENDING_GPU_VALIDATION | `tests/integration/test_gpu.py` (skipped without CUDA) | NVIDIA GPU + CUDA | none | Never executed: this machine has no CUDA device | `pytest -m gpu -v` on a GPU host, then `python scripts/gpu_smoke_test.py` |
| Scene-boundary-aware interpolation (no blending across cuts) | IMPLEMENTED_AND_CPU_TESTED | `tests/integration/test_rife_cpu.py` | CPU | none | Cut detection quality bounds the result | `pytest -k "cut" -v` |
| Interpolation limits (≤ 60 fps, ≤ 1920×1080 for the neural path) | IMPLEMENTED_AND_CPU_TESTED | `tests/unit/test_capabilities.py` | CPU | none | — | `pytest -k "fps or limit" -v` |
| NVENC hardware encoding | IMPLEMENTED_PENDING_GPU_VALIDATION | `tests/unit/test_encoders.py` (selection logic only) | NVIDIA GPU | none | `auto` falls back to libx264 without a device | `python main.py --input clip.mp4 --encoder nvenc` on a GPU host |
| SAM 2 rotoscoping | IMPLEMENTED_PENDING_GPU_VALIDATION | `tests/unit/test_rotoscope.py` (contract only) | NVIDIA GPU | none | SAM 2 is not on PyPI and needs a checkpoint | `pip install -e ".[gpu]" && python scripts/download_models.py --sam2 small` |
| 4K upscaling (TensorRT / Real-ESRGAN) | IMPLEMENTED_PENDING_GPU_VALIDATION | `tests/unit/test_upscale.py` (contract only) | NVIDIA GPU | none | Not part of the supported API surface | `python scripts/gpu_smoke_test.py` |
| Signed download URLs and job webhooks (`app/core/ops.py`) | IMPLEMENTED_AND_CPU_TESTED | `tests/unit/test_ops.py` | CPU | webhook receiver (not exercised live) | The download side is exposed on `/v1`; the webhook side is not | `pytest tests/unit/test_ops.py -v` |
| Signed artefact downloads (`GET /v1/jobs/{job_id}/download`) | IMPLEMENTED_AND_CPU_TESTED | `tests/integration/test_signed_downloads.py` | CPU | none | Needs `SIGNING_SECRET`; without it no link is issued | `pytest tests/integration/test_signed_downloads.py -v` |

## 4. Storage

| Feature | Status | Test status | Required hardware | External service | Known limitations | Next validation command |
|---|---|---|---|---|---|---|
| Local filesystem storage | IMPLEMENTED_AND_CPU_TESTED | `tests/unit/test_storage.py` | CPU | none | — | `pytest -k "local" -v` |
| S3-compatible storage (boto3, custom endpoint for R2) | IMPLEMENTED_AND_CPU_TESTED | `tests/unit/test_storage.py` against a fake boto3 client | CPU | S3 / R2 / MinIO | **No live bucket was used** — the repository has no credentials; live behaviour (IAM, presigned URLs, multipart) is unverified | `STORAGE_BACKEND=s3 S3_BUCKET=… pytest tests/integration/test_storage_s3.py -v` against a real bucket |
| Presigned upload / download URLs with expiry | IMPLEMENTED_AND_CPU_TESTED | `tests/unit/test_storage.py`, `tests/integration/test_output_links.py` | CPU | S3-compatible service | Signed query strings are redacted in logs but returned in full to the caller | `pytest -k presign -v` |
| Presigned upload flow for API clients (`POST /v1/jobs/upload-url`) | IMPLEMENTED_AND_CPU_TESTED | `tests/integration/test_upload_flow.py` | CPU | S3-compatible service | Object storage only; the local backend answers 409 because it has nothing to sign | `pytest tests/integration/test_upload_flow.py -v` |
| Object-key sanitisation (no prefix escape) | IMPLEMENTED_AND_CPU_TESTED | `tests/unit/test_security.py`, `tests/unit/test_storage.py` | CPU | none | — | `pytest -k "key or traversal" -v` |

## 5. Deployment and operations

| Feature | Status | Test status | Required hardware | External service | Known limitations | Next validation command |
|---|---|---|---|---|---|---|
| CPU Docker image (non-root, health check, no secrets) | IMPLEMENTED_AND_CPU_TESTED | image built and `scripts/check_environment.py` executed inside it | CPU | none | The image contains no model weights | `docker build -t framefrog-pipeline:cpu . && docker run --rm framefrog-pipeline:cpu python scripts/check_environment.py` |
| GPU Docker image (`Dockerfile.gpu`) | CONFIGURED_PENDING_CLOUD_VALIDATION | **never built** | NVIDIA GPU + CUDA 12.1 | none | Base image and CUDA wheels are pinned but unverified | `docker build -f Dockerfile.gpu -t framefrog-pipeline:gpu .` on a CUDA host |
| Cerebrium deployment configuration | CONFIGURED_PENDING_CLOUD_VALIDATION | **never deployed**; the schema is not verified against the live platform | GPU instance | Cerebrium | `scale_to_zero`, `gpu_type` and the environment block must be checked against the current CLI docs | `cerebrium deploy` (not executed) |
| CI: Ruff lint, Ruff format, MyPy, pytest, Docker build, secret scan, dependency audit, container scan | IMPLEMENTED_AND_CPU_TESTED | the workflow runs on GitHub and is green: all five jobs passed on commit `908f978` (run 44691725997) | CPU | GitHub Actions | The gitleaks action needs an organisation licence, so the OSS image is used; the example configuration is allowlisted; Trivy blocks on CRITICAL findings only | `gh run list --branch feat/gpu-ready-pre-mvp` |
| GPU validation workflow (`.github/workflows/gpu-validation.yml`) | IMPLEMENTED_PENDING_GPU_VALIDATION | **never run** — it needs a self-hosted GPU runner | NVIDIA GPU | GitHub Actions + self-hosted runner | The `gpu` runner label must exist | `gh workflow run gpu-validation.yml -f runner=gpu-l40s` |
| GPU smoke-test tool (`scripts/gpu_smoke_test.py`) | IMPLEMENTED_PENDING_GPU_VALIDATION | runs on this machine and correctly reports `NOT RUN — CUDA GPU UNAVAILABLE` with a non-zero exit | NVIDIA GPU | none | It measures peak CUDA memory only when torch can report it | `python scripts/gpu_smoke_test.py` on a GPU host |
| Environment check (`scripts/check_environment.py`) | IMPLEMENTED_AND_CPU_TESTED | executed in this branch and inside the Docker image | CPU | none | — | `python scripts/check_environment.py` |
| Synthetic media generator (`scripts/generate_test_video.py`) | IMPLEMENTED_AND_CPU_TESTED | used by the integration tests | CPU + FFmpeg | none | Test sources only — no real footage | `python scripts/generate_test_video.py --output /tmp/sample.mp4` |
| Model acquisition (`scripts/download_models.py`) | IMPLEMENTED_AND_CPU_TESTED | the RIFE graph was downloaded and used by the CPU interpolation test | CPU | Hugging Face / GitHub releases | The RIFE digest is pinned; the SAM 2 and Real-ESRGAN digests are not yet pinned | `python scripts/download_models.py` |
| Model checksum pinning and verification | IMPLEMENTED_AND_CPU_TESTED | `tests/unit/test_model_download.py` | CPU | none | Only the RIFE graph is pinned so far | `python scripts/download_models.py --verify-only` |

## 6. Explicitly not implemented (roadmap)

| Item | Status | Note |
|---|---|---|
| 4K delivery | NOT_IMPLEMENTED | Upscaling exists but is not part of the supported API surface |
| 120 fps interpolation | NOT_IMPLEMENTED | Hard limit of 60 fps is enforced with a capability error |
| Multi-GPU inference | NOT_IMPLEMENTED | One GPU per deployment |
| Kubernetes manifests | NOT_IMPLEMENTED | Serverless and Docker only |
| Web dashboard | NOT_IMPLEMENTED | API only |
| Billing / subscriptions | NOT_IMPLEMENTED | Cost accounting reports measured seconds only |
| User accounts and per-tenant quotas | NOT_IMPLEMENTED | A single API key |
| AI reference-image look matching | NOT_IMPLEMENTED | The LUT stage applies a supplied `.cube`; it does not derive a look from a reference image |
| Multi-region deployment | NOT_IMPLEMENTED | Single region |
| Load testing | NOT_IMPLEMENTED | Concurrency limit exists, no capacity numbers are known |
| Cost optimisation | NOT_IMPLEMENTED | Rates are configurable, no figures are published |
| PostgreSQL job store | NOT_IMPLEMENTED | The repository interface allows it; the implementation does not exist |
| Presigned download links on the `/v1` API | IMPLEMENTED_AND_CPU_TESTED | the output entries carry a presigned object-store URL when the backend can sign one, and the HMAC-signed local link otherwise (`tests/integration/test_output_links.py`) | — | — |
| Container scanning in CI | IMPLEMENTED_NOT_TESTED | the Trivy step is configured in `.github/workflows/ci.yml`; it has never run because the workflow has not been executed on GitHub |

## 7. What the GPU phase must still prove

The repository deliberately does **not** claim any of the following. Each has an
exact command in `docs/GPU_VALIDATION.md`:

1. that RIFE runs on CUDA and how long it takes per second of footage;
2. the peak VRAM of the SAM 2 and upscaling stages;
3. that NVENC encodes a master at the expected quality;
4. that the serverless GPU platform cold-starts within the configured timeout;
5. the cost per processed video minute on the real rate card;
6. that a GPU job never silently falls back to the CPU.

Until those run, `gpu_validation_status` stays `pending` in every manifest and
the GPU metric fields (`gpu_name`, `gpu_seconds`, `peak_vram_mb`,
`cuda_version`, `inference_seconds`) are **absent**, never zero.
