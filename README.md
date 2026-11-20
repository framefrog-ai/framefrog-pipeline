# FrameFrog pipeline

> **FrameFrog** — an AI-native cloud video post-production pipeline.
> Scene detection, neural audio cleanup, 3D LUT grading, RIFE frame
> interpolation and MP4 delivery, orchestrated behind one FastAPI service and
> a persistent job queue.

[![CI](https://img.shields.io/github/actions/workflow/status/framefrog-ai/framefrog-pipeline/ci.yml?branch=feat/gpu-ready-pre-mvp&label=CI&logo=github&logoColor=10B981&labelColor=0D1117&color=10B981)](https://github.com/framefrog-ai/framefrog-pipeline/actions/workflows/ci.yml)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-10B981?style=for-the-badge&labelColor=0D1117)](./LICENSE)

- **Product site:** <https://framefrog.me>
- **Status of every feature:** [IMPLEMENTATION_STATUS.md](./IMPLEMENTATION_STATUS.md)
- **Architecture:** [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md)
- **GPU validation plan:** [docs/GPU_VALIDATION.md](./docs/GPU_VALIDATION.md)
- **Security model:** [SECURITY.md](./SECURITY.md)

---

## 1. Scope

FrameFrog takes rushes and returns broadcast-ready masters through a chain of
independent, GPU-capable stages:

```text
NVDEC → DeepFilterNet3 → SAM 2 → 3D LUT → RIFE → Real-ESRGAN → NVENC
decode   neural audio     mattes   look     frames   4K detail   hardware
```

Every stage is a stateless function over files. That is what lets the same
code run as a local CLI, inside the FastAPI service, or as an isolated
serverless GPU function.

**This repository is a pre-MVP.** The CPU path is implemented and tested; the
GPU path is implemented but has never been executed, because no CUDA device
was available while it was written. Nothing in this repository claims a GPU,
VRAM, benchmark or cost result that a command in this repository did not
produce. See [IMPLEMENTATION_STATUS.md](./IMPLEMENTATION_STATUS.md).

## 2. Architecture at a glance

```text
        POST /v1/jobs
                │
                ▼
    ┌───────────────────────┐        ┌────────────────────┐
    │  api/routes_jobs.py   │───────▶│ jobs/repository.py │  SQLite (WAL)
    │  validated schemas    │        │  jobs + idempotency│
    └───────────┬───────────┘        └─────────┬──────────┘
                │ 202 Accepted                 │
                ▼                              ▼
    ┌───────────────────────────────────────────────────────┐
    │ jobs/runner.py — bounded thread pool, cancellation     │
    └───────────────┬───────────────────────────────────────┘
                    ▼
    ┌───────────────────────────────────────────────────────┐
    │ jobs/pipeline.py — stage order, manifest, progress     │
    └───┬───────────┬───────────┬───────────┬───────────────┘
        ▼           ▼           ▼           ▼
   scene_detect  audio_    color_grade   interpolate   deliver
                 cleanup
        │           │           │           │           │
        └───────────┴───────────┴───────────┴───────────┘
                            ▼
                  media/ffmpeg.py · media/ffprobe.py
                            ▼
              storage/local.py  ·  storage/s3.py
```

- `app/core/` — configuration, errors, structured logging, security, capability probes
- `app/api/` — HTTP schemas, dependencies and routes (`/health`, `/ready`, `/v1/jobs`)
- `app/jobs/` — domain models, state machine, SQLite repository, background runner
- `app/media/` — FFmpeg/FFprobe wrappers, LUT handling, media validation
- `app/stages/` — the seven stage implementations
- `app/storage/` — local filesystem and S3-compatible backends
- `app/models/` — discovery of optional model runtimes and weights

## 3. Installation

Requirements: **Python 3.11+**, **FFmpeg and FFprobe** (`apt-get install ffmpeg`
/ `brew install ffmpeg`).

```bash
git clone git@github.com:framefrog-ai/framefrog-pipeline.git
cd framefrog-pipeline

python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # runtime + test tooling
pip install -e ".[models]"       # + ONNX Runtime (RIFE)
pip install -e ".[audio]"        # + DeepFilterNet (pulls torch)
pip install -e ".[gpu]"          # + CUDA torch / onnxruntime-gpu
```

Optional groups live in `pyproject.toml`: `audio`, `models`, `gpu`, `gpu-extra`,
`dev`. The base install has **no CUDA dependency** — the application imports
and starts on a machine without a GPU.

Model weights are not vendored; fetch them explicitly:

```bash
python scripts/download_models.py             # RIFE v4.9 ONNX (21 MB)
python scripts/download_models.py --verify-only   # check what is on disk
```

Every download is verified against a pinned SHA-256 digest. A file that does
not match is removed and the command fails — an unverified model is never
used.

## 4. Configuration

Copy `.env.example` to `.env` and adjust. Every variable is read by
`app/core/config.py`; the process validates them at startup and refuses to
start on an invalid configuration.

| Variable | Default | Purpose |
|---|---|---|
| `APP_ENV` | `development` | `development` / `test` / `production` |
| `LOG_LEVEL` | `INFO` | Log level for the JSON logger |
| `API_KEY` | *(empty)* | Required in production; `X-API-Key` or `Authorization: Bearer` |
| `SIGNING_SECRET` | *(empty)* | HMAC key for signed artefact download links |
| `WORK_DIR` | `./work` | Job working directory and SQLite location |
| `DATABASE_URL` | `sqlite:///./work/framefrog.db` | Job store |
| `STORAGE_BACKEND` | `local` | `local` or `s3` |
| `MAX_UPLOAD_BYTES` | `2147483648` | Largest accepted input |
| `MAX_VIDEO_DURATION_SECONDS` | `3600` | Longest accepted input |
| `MAX_VIDEO_WIDTH` / `MAX_VIDEO_HEIGHT` | `3840` / `2160` | Largest accepted resolution |
| `MAX_OUTPUT_FPS` | `60` | Highest accepted frame rate (and interpolation target) |
| `MAX_CONCURRENT_JOBS` | `2` | Jobs running in parallel |
| `JOB_RETENTION_HOURS` | `24` | How long a finished job and its media are kept (0 keeps everything) |
| `FFMPEG_PATH` / `FFPROBE_PATH` | `ffmpeg` / `ffprobe` | Binary locations |
| `S3_ENDPOINT_URL` | *(none)* | Custom endpoint (Cloudflare R2, MinIO) |
| `S3_REGION` / `S3_BUCKET` | `us-east-1` / *(none)* | Object store |
| `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` | *(none)* | Object store credentials |
| `S3_INPUT_PREFIX` / `S3_INTERMEDIATE_PREFIX` / `S3_OUTPUT_PREFIX` | `inputs` / `intermediate` / `outputs` | Key prefixes |
| `PRESIGNED_URL_EXPIRY_SECONDS` | `900` | Signed URL lifetime |
| `MODEL_CACHE_DIR` | `./models` | Where model weights are cached |
| `DEEPFILTERNET_ENABLED` | `true` | Enable the neural audio adapter |
| `RIFE_ENABLED` | `true` | Enable the interpolation adapter |
| `RIFE_REPOSITORY_PATH` / `RIFE_MODEL_PATH` | *(none)* | Explicit model location |
| `COMPUTE_DEVICE` | `auto` | `auto` / `cpu` / `cuda` |
| `GPU_VALIDATION_REQUIRED` | `false` | Fail instead of falling back when a GPU stage is requested |

`APP_ENV=production` **requires** `API_KEY`; the process exits with a clear
message otherwise.

## 5. Running it locally

```bash
# API
uvicorn app.main:app --host 0.0.0.0 --port 8080

# one-shot CLI run over a local file
python main.py --input ./rushes.mov --stages scene_detect,color_grade,deliver

# end-to-end demo on generated footage (no input needed)
python demo.py
```

Interactive API docs: <http://localhost:8080/docs>.

## 6. API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness |
| `GET` | `/ready` | Readiness (binaries, database, storage) |
| `POST` | `/v1/jobs` | Create a job — returns `202` immediately |
| `GET` | `/v1/jobs/{job_id}` | Job state, stage results, outputs |
| `POST` | `/v1/jobs/{job_id}/cancel` | Request cancellation |
| `GET` | `/v1/jobs/{job_id}/outputs` | Produced artifacts (with signed download links) |
| `GET` | `/v1/jobs/{job_id}/download` | Signed artefact download (`?artifact=master&expires=…&token=…`) |
| `POST` | `/v1/jobs/upload-url` | Presigned upload URL for a source file (object storage only) |

```bash
curl -X POST http://localhost:8080/v1/jobs \
  -H 'Content-Type: application/json' \
  -H "X-API-Key: $API_KEY" \
  -d '{
    "input_uri": "s3://my-bucket/inputs/rushes.mp4",
    "stages": ["scene_detect", "audio_cleanup", "interpolate", "color_grade", "deliver"],
    "options": {
      "scene_detect": {"threshold": 27.0, "minimum_scene_length_seconds": 0.5},
      "audio_cleanup": {"enabled": true, "target_lufs": -16.0},
      "interpolate": {"enabled": true, "target_fps": 60, "device": "auto"},
      "color_grade": {"enabled": false, "lut_uri": null, "intensity": 1.0},
      "deliver": {"container": "mp4", "video_codec": "h264", "audio_codec": "aac", "crf": 20, "preset": "medium"}
    },
    "idempotency_key": "rushes-2026-11-19-a"
  }'
```

Downloading without exposing the storage: set `SIGNING_SECRET` and every
output carries a link that expires and is signed with an HMAC token. A
tampered or expired link is refused. With object storage the output carries
a presigned URL instead.

Notifying a caller: set `options.webhook_url` and the finished manifest is
POSTed to it (HMAC-signed with `options.webhook_secret`). Delivery is
best-effort and never changes the job status; the attempt is recorded in the
manifest.

Uploading without exposing local paths: ask for a presigned URL, PUT the file
to object storage, then create the job with the returned `input_uri`.

```bash
curl -X POST http://localhost:8080/v1/jobs/upload-url \
  -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
  -d '{"filename": "rushes.mp4"}'
# -> {"input_uri": "s3://my-bucket/inputs/uploads/<token>/rushes.mp4",
#     "upload_url": "https://...", "method": "PUT", "expires_in": 900}
```

Job lifecycle: `queued → acquiring_input → validating → processing → uploading
→ completed | failed | cancelled`. Transitions are validated; an illegal
transition raises rather than silently overwriting state.

A stage option with an explicit `"enabled": false` (`audio_cleanup`,
`interpolate`, `color_grade`) removes that stage from the run; the manifest
records it with status `skipped` instead of running it. Only an explicit flag
disables a stage — the defaults never do.

`fanout.mode` decides how the heavy stages are executed: `off` (the default),
`shots` (each detected shot is processed separately and the shots are
stitched back together) or `auto` (per-shot, falling back to the monolithic
stage if the fan-out fails — the reason is recorded in the manifest).

Local inputs are restricted to `WORK_DIR` (plus `ALLOWED_INPUT_ROOTS`); the
CLI additionally allows the current directory because it runs on the
operator's machine.

## 7. Tests

```bash
pytest -m "not gpu" -v         # the CPU suite (no GPU needed)
pytest -m gpu -v               # GPU-marked tests, skipped without CUDA
python scripts/generate_test_video.py   # synthetic media
```

Test media is generated locally with FFmpeg's own test sources — nothing is
downloaded. The suite covers configuration, authentication, URI validation,
SSRF protection, path safety, state transitions, idempotency, rational frame
rates, FFprobe parsing, FFmpeg command construction, scene serialisation, LUT
validation, storage keys, the DeepFilterNet and RIFE capability boundaries,
error responses and redaction — plus integration coverage of health,
readiness, the job API, scene detection, LUT application, final MP4 delivery
and local storage.

A skipped test is not a passed test: the GPU tests are skipped on this
machine and the repository records that fact.

## 8. Docker

```bash
docker build -t framefrog-pipeline:cpu .
docker run --rm -p 8080:8080 -e APP_ENV=development framefrog-pipeline:cpu

# GPU image (never built from this repository — no CUDA host available)
docker build -f Dockerfile.gpu -t framefrog-pipeline:gpu .
```

The CPU image runs as a non-root user, contains no secrets and no model
weights, and has a health check. It defaults to `APP_ENV=production`, where
an `API_KEY` is mandatory.

## 9. Storage

- **local** — everything under `WORK_DIR` (`jobs/<job_id>/work/…`).
- **s3** — any S3-compatible service via boto3, with `inputs`, `intermediate`
  and `outputs` prefixes, presigned upload/download URLs and a custom
  `S3_ENDPOINT_URL` for Cloudflare R2 or MinIO.

Object keys are sanitised so a job id or file name can never escape a prefix.
Credentials are read from the environment and never logged; presigned URLs are
recorded redacted.

Colour looks live in `assets/luts/` (committed). A look that has to be
generated at runtime is written to the model cache (`MODEL_CACHE_DIR/luts`),
the work directory or the system temp directory — never into the source tree.

A grade only applies a **built-in look** or a file inside a trusted LUT
directory (`assets/luts`, `$FRAMEFROG_LUT_DIR`, `$MODEL_CACHE_DIR/luts`,
`$WORK_DIR/luts`). Any other path is refused, so a request cannot make the
pipeline read an arbitrary file from the host.

## 10. DeepFilterNet (audio cleanup)

The adapter extracts mono 48 kHz PCM, runs DeepFilterNet3, loudness
normalises to −16 LUFS and remuxes against the untouched picture. When the
package is not installed, the FFmpeg spectral path runs instead and the
manifest records `denoise_model: "ffmpeg"` with the fallback reason — the
original audio is never returned as "cleaned". Requesting
`denoise_model: "deepfilternet"` without the package is an error, not a
silent fallback.

```bash
pip install -e ".[audio]"
```

## 11. RIFE (frame interpolation)

The RIFE v4.9 ONNX graph drives real optical-flow interpolation. It is
scene-aware: frames are never blended across a hard cut. Output is capped at
60 fps and 1920×1080 for the neural path. An explicit `device: "cuda"`
request fails with a capability error when no CUDA device is present — a GPU
job never silently runs on the CPU.

```bash
python scripts/download_models.py
python -m pytest tests/integration/test_rife_cpu.py -v
```

## 12. Cerebrium

`cerebrium.toml` configures the serverless GPU deployment (FastAPI entry
point, system packages, environment, GPU type, scale-to-zero, model cache).
It has **not** been deployed from this repository; see
[docs/CEREBRIUM_DEPLOYMENT.md](./docs/CEREBRIUM_DEPLOYMENT.md).

## 13. Limits

| Limit | Value |
|---|---|
| Input size | `MAX_UPLOAD_BYTES` (2 GiB default) |
| Input duration | `MAX_VIDEO_DURATION_SECONDS` (1 h default) |
| Input resolution | `MAX_VIDEO_WIDTH` × `MAX_VIDEO_HEIGHT` (3840×2160 default) |
| Frame rate | `MAX_OUTPUT_FPS` (60) |
| Interpolation target | 60 fps (120 fps is not implemented) |
| RIFE input resolution | 1920×1080 |
| Parallel jobs | `MAX_CONCURRENT_JOBS` (2) |
| Colour grade intensity | 1.0 only (partial blending is not implemented) |

## 14. Housekeeping

A finished job keeps its media for `JOB_RETENTION_HOURS` (24 by default).
After that the directory and the job row are removed, so a long-running
service does not fill the disk. A job that is still queued or running is
never touched.

```bash
python scripts/cleanup_jobs.py --dry-run   # what would be removed
python scripts/cleanup_jobs.py             # remove it
```

## 15. Security

API key authentication outside development mode, SSRF protection (loopback,
private, link-local and cloud-metadata destinations are refused), local path
containment under `WORK_DIR`, traversal rejection, media limits, no
`shell=True` anywhere, per-job temporary directories, bounded concurrency,
command timeouts and redacted signed URLs. The full model is in
[SECURITY.md](./SECURITY.md).

## 16. Known limitations

- No GPU execution has been validated; every GPU claim is `pending`.
- The job runner is in-process (threads); there is no distributed queue.
- SQLite is single-node; the repository interface allows PostgreSQL later.
- One shared API key; no accounts, quotas or rate limiting.
- DeepFilterNet has not been executed in this environment.
- The colour-grade stage applies a supplied `.cube`; it does not derive a
  look from a reference image.
- 4K, 120 fps, multi-GPU, Kubernetes, dashboard, billing and multi-region are
  out of scope for this phase.

## 17. Roadmap

1. Validate CUDA execution, RIFE performance, VRAM and cost per video minute
   once startup cloud credits are available ([plan](./docs/GPU_VALIDATION.md)).
2. Deploy to Cerebrium and validate the serverless GPU lifecycle.
3. Replace the in-process runner with a queue consumer (SQS).
4. PostgreSQL job store for multi-node deployments.
5. Per-tenant keys, quotas and rate limiting.
6. Partial LUT intensity blending, once implemented and tested.

## 18. Licence

AGPL-3.0-or-later — see [LICENSE](./LICENSE). Third-party components keep
their own licences; nothing upstream is vendored into this repository.
