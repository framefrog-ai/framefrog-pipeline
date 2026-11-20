# FrameFrog Pipeline — Architecture

This describes the architecture **as implemented in this repository** (branch
`feat/gpu-ready-pre-mvp`). It is written against the code: where something is
only partly wired up, that is stated in [§11](#11-what-is-not-implemented).

## 1. Component map

```
   HTTP client
        |
        v
   FastAPI app (app/main.py::create_app)
     routes_jobs.py   /v1/jobs (returns 202 immediately)
     routes_health.py /health  /ready
     deps.py          require_auth + DI accessors
     schemas.py       pydantic request/response validation
        |
        +--> JobRepository (jobs/repository.py)  SQLite, unique idempotency index
        |
        +--> JobRunner (jobs/runner.py)        ThreadPoolExecutor,
        |                                        max_workers=MAX_CONCURRENT_JOBS
        v
   run_job (jobs/pipeline.py)   acquire -> validate -> stages -> manifest
        |
     +--+------------+------------+
     v              v            v
  StorageBackend  STAGES      media layer
  local.py/s3.py  registry    ffmpeg / ffprobe /
  inputs/         (7 pure     validation / lut
  intermediate/    file
  outputs/      transformations

  Optional, currently NOT wired into the request path:
  jobs/fanout.py + queue.py + worker.py (per-shot fan-out)
  core/ops.py                              (signed links, webhooks, cost)
```

| Path | Responsibility |
|---|---|
| `app/core/*` | `config` (settings), `errors` (taxonomy), `logging` (JSON envelope), `security` (auth, URI/path/SSRF), `capabilities` (runtime probes), `ops` (links, webhooks, cost — not called from the API) |
| `app/api/*` | FastAPI routes, dependency injection, request/response schemas |
| `app/jobs/*` | `models`, `states`, `repository`, `runner`, `pipeline`, `fanout`, `queue`, `worker` |
| `app/storage/*` | storage abstraction (`base`), `local`, `s3` backends |
| `app/media/*` | `ffmpeg`, `ffprobe`, `validation`, `lut` |
| `app/stages/*` | the seven stage implementations and the stage contract |
| `app/models/discovery.py` | optional model runtime/weight discovery |

## 2. Request lifecycle

`POST /v1/jobs` returns **`202 Accepted`** immediately; the HTTP thread never
runs a stage.

```
POST /v1/jobs
  ├─ require_auth        X-API-Key / Authorization: Bearer, constant-time compare
  ├─ pydantic validation known+unique stages, option ranges, lut_uri must be
  │                      .cube, crf/preset/fps bounds
  ├─ idempotency check   find_by_idempotency_key → 409 if it exists
  ├─ parse_input_uri     s3:// | local path; http(s) rejected; local path must be
  │                      inside WORK_DIR ∪ ALLOWED_INPUT_ROOTS
  ├─ validate_target_fps target_fps ≤ MAX_OUTPUT_FPS
  ├─ repository.create   INSERT row status='queued' (unique index on the key)
  ├─ runner.submit       ThreadPoolExecutor.submit(_execute, job_id, request)
  └─ return 202          JobResponse snapshot of the persisted row
```

The background thread then runs `run_job`:

```
  ├─ acquire_input   storage.acquire() → WORK_DIR/jobs/<job_id>/work/00_input.<ext>
  │                  (missing or empty → job fails)
  ├─ validating      validation.validate_media_file() → ffprobe + limit checks
  ├─ processing      for each stage in order:
  │                    stage.run(input_path, workdir, options) → StageResult
  │                    manifest["stages"].append({stage,status,duration_s,output,meta})
  │                    video-producing stages advance `current`
  ├─ uploading       no object-store write is performed here (see §11)
  └─ manifest        written to WORK_DIR/jobs/<job_id>/manifest.json and stored
                     in the row's manifest_json
```

Job directory:

```
WORK_DIR/jobs/<job_id>/
  work/  00_input.mp4  01_audio_clean.mp4  02_scenes.json  03_graded.mp4
         04_interpolated.mp4  05_master.mp4  05_proxy_720p.mp4  05_poster.jpg
         06_upscaled.mp4  07_matte.mp4  07_masks.json  07_sam2_masks/*.png
  manifest.json
```

Read-back: `GET /v1/jobs/{job_id}` (full record), `GET
/v1/jobs/{job_id}/outputs` (artifacts with size + SHA-256), `POST
/v1/jobs/{job_id}/cancel` (sets the cancel flag).

**Progress.** `run_job` calls a `progress(status, payload)` callback per
phase; the runner's `_persist_progress` writes `stage_results`,
`current_stage`, `progress_percent` and the partial manifest back to the row
without touching the state machine. Percentages: acquiring/validating → 5,
processing → 50, uploading → 95, `stage:<name>` → 90 × done/total (cap 95),
100 on completion.

## 3. Job state machine

`app/jobs/states.py` defines the persisted lifecycle: `queued`,
`acquiring_input`, `validating`, `processing`, `uploading`, `completed`,
`failed`, `cancelled` (the last three terminal).

| From | To |
|---|---|
| `queued` | `acquiring_input`, `cancelled`, `failed` |
| `acquiring_input` | `validating`, `cancelled`, `failed` |
| `validating` | `processing`, `cancelled`, `failed` |
| `processing` | `uploading`, `completed`, `cancelled`, `failed` |
| `uploading` | `completed`, `cancelled`, `failed` |
| `completed` / `failed` / `cancelled` | *(no outgoing edges)* |

`validate_transition` raises `InvalidTransitionError` for any other edge;
`SQLiteJobRepository.set_status` and `update` apply the check.

**Implementation note.** The runner currently persists only
`queued → acquiring_input → <terminal>` in the `status` column. The
intermediate states exist in the state machine and are reported as phase
events in the manifest and progress payload, but are not written as row
statuses. The terminal write goes through `repository.save`, which skips the
transition check so a job can always be finalised.

## 4. Stage contract

```python
def run(input_path: str, workdir: str, options: dict) -> StageResult: ...
```

* **Input** — path to the current video (metadata-only stages return it
  unchanged).
* **Workdir** — the per-job `work/` directory; the stage writes outputs there
  and returns their paths.
* **Options** — a flat `dict`. Keys starting with `_` are private to the
  orchestrator (e.g. `_cancel_check`) and are stripped from the manifest.
* **Return** — `StageResult(stage, output_uri, meta, duration_s, status,
  error)` (`app/jobs/models.py`). `meta` must be JSON-serialisable and is
  copied verbatim into the manifest.

Stages are **pure file transformations**: they never touch the database, the
HTTP layer or the object store. That is what makes the same stage callable
from the CLI, the HTTP pipeline and the fan-out worker unchanged.

`app/stages/__init__.py` is the registry — `STAGES` maps the seven names to
their `run` functions, `STAGE_NAMES` is its key list, `GPU_STAGES` is
`("rotoscope", "upscale")`. Adding a stage = one file + one registry entry.

| Stage status (`app/stages/base.py`) | Meaning |
|---|---|
| `completed` | the stage produced its output |
| `failed` | the stage raised; error and short traceback recorded |
| `pending_gpu_validation` | implemented but not yet validated on real GPU hardware; never reported as completed |
| `skipped` | the stage was a no-op (source already at target rate / height) |
| `cancelled` | the job was cancelled before or during the stage |

A `CapabilityError` (missing binary, no CUDA device, unsupported resolution)
is recorded as `pending_gpu_validation` and fails the job — a stage is never
silently downgraded to success.

## 5. Stage order and behaviour

Default job (`DEFAULT_STAGES`): `audio_cleanup → scene_detect →
color_grade → interpolate → deliver`. `rotoscope` and `upscale` are opt-in
(they are in `KNOWN_STAGES` but not in `DEFAULT_STAGES`) because they need a
GPU.

| # | Stage | What it does | Key options |
|---|---|---|---|
| 1 | `audio_cleanup` | Denoise + loudness-normalise to −16 LUFS. DeepFilterNet3 when installed, else an FFmpeg chain (`highpass`, `lowpass`, `afftdn`, `loudnorm`). Video stream-copied, audio re-encoded (AAC 192 k). No audio stream → straight copy. | `denoise_model` (`auto`/`deepfilternet`/`ffmpeg`), `lufs`, `highpass`, `lowpass` |
| 2 | `scene_detect` | Shot boundaries. PySceneDetect `ContentDetector` when installed, else an OpenCV HSV-histogram fallback (5 frames/s analysed). Writes `02_scenes.json`; returns the scene list in `meta`. | `scene_threshold`, `min_scene_s` |
| 3 | `rotoscope` | SAM 2 matte. `auto` seeds from the largest object on frame 0; `prompted` from a point or box. Writes `07_matte.mp4`, `07_sam2_masks/*.png`, `07_masks.json`. The picture is not altered. | `sam2_checkpoint` (required), `sam2_config`, `sam2_device`, `rotoscope_mode`, `rotoscope_prompt` |
| 4 | `color_grade` | Applies a `.cube` LUT via FFmpeg's `lut3d`. Built-in looks are generated by `app/media/lut.py`; an existing `.cube` path wins. Strength < 1 uses a `split`/`blend` filter graph. | `lut`, `look_strength` |
| 5 | `interpolate` | Frame-rate raise to ≤ 60 fps. RIFE v4.9 ONNX (optical flow, scene-aware: frames are never blended across a hard cut) or FFmpeg `minterpolate` (`blend`/`mci`). An explicit `cuda` provider with no CUDA device is a hard `CapabilityError`. | `interp_model`, `target_fps`, `rife_onnx`, `rife_provider`, `interp_quality` |
| 6 | `upscale` | Rescale to `target_height` (default 2160). Backends: TensorRT engine → Real-ESRGAN x4 → FFmpeg Lanczos. No-op (`skipped`) when the source is already at/above the target height. | `target_height`, `upscale_backend`, `upscale_engine`, `upscale_model`, `upscale_tile` |
| 7 | `deliver` | Bundle: H.264/HEVC master (`master_crf`), 720p proxy, poster JPEG at 10 % of the duration (capped at 30 s), `+faststart`. | `master_crf`, `encoder` |

* `scene_detect` output feeds `interpolate` via the private option `scenes`,
  so interpolation never blends across a cut.
* `deliver`'s `meta` populates `manifest["outputs"]` (kind, path,
  `size_bytes`, `sha256`, and for the master width/height/fps/duration/codecs).
* Encoder selection is shared (`app/media/ffmpeg.py::video_encode_args`):
  `auto` uses `h264_nvenc` only when FFmpeg advertises it **and** a
  `/dev/nvidia*` node exists, else `libx264`. The chosen encoder is recorded
  in each stage's `meta`.

## 6. Storage abstraction

`app/storage/base.py` defines `StorageBackend`: `acquire(uri, destination)`,
`publish(local_path, key)`, `exists(key)`, `delete(key)`,
`presign_get/put(key, expires)`, `health()`.

| Prefix setting | Default | Purpose |
|---|---|---|
| `S3_INPUT_PREFIX` | `inputs` | source media |
| `S3_INTERMEDIATE_PREFIX` | `intermediate` | per-job working files |
| `S3_OUTPUT_PREFIX` | `outputs` | delivered artifacts |

`build_key(prefix, *parts)` concatenates untrusted parts through
`safe_object_key`: separators stripped, `.`/`..` segments dropped, the result
never starts with `/`.

* **`local.py` — `LocalStorage`.** Rooted at `WORK_DIR`; `path_for(key)`
  resolves the key and re-checks containment against the root before any
  read or write. `presign_get/put` return `None` — local files are not
  served over a signed URL.
* **`s3.py` — `S3Storage`.** boto3, lazily imported; works with AWS S3, R2
  and MinIO via `S3_ENDPOINT_URL`. `acquire` downloads `s3://bucket/key`;
  `publish` uploads and returns the `s3://` URI. Presigned GET/PUT use
  `PRESIGNED_URL_EXPIRY_SECONDS` (default 900 s) and are logged only
  redacted (`redact_url` strips the query string). `health()` calls
  `head_bucket` and reports the bucket, never credentials.

**Currently** only `acquire` is called by the pipeline. `publish` is
implemented and tested but the request path does not call it — outputs stay
in the job workdir (see §11).

## 7. Media layer

**`app/media/ffmpeg.py`.** Commands are always **argument lists**;
`shell=True` appears nowhere in the repository. Every call has an explicit
timeout: 1800 s for FFmpeg (`DEFAULT_TIMEOUT`), 120 s for ffprobe, 20–30 s
for capability probes. Failures raise `FFmpegError` carrying the **last 12
lines of stderr** and the argument list; `run_to_file` additionally verifies
the expected output exists and is non-empty. `safe_command(args)` produces a
log-safe command line (credential-style query parameters → `***`, arguments
truncated at 200 chars). `choose_encoder` resolves `auto | libx264 |
libx265 | nvenc | h264_nvenc | hevc_nvenc`.

**`app/media/ffprobe.py`.** `probe()` parses ffprobe JSON. Frame rate comes
from `avg_frame_rate`, falling back to `r_frame_rate`, parsed by
`parse_rational`: `"30000/1001"` → `29.97`, `"0/0"`/`"N/A"`/empty → `0.0`.
Nothing is inferred from the file name or container extension.

**`app/media/validation.py`.** `validate_media_file()` runs before any
processing and raises `MediaValidationError` (HTTP 422) with a precise
`details` payload: the file must exist, be a regular file, be non-empty,
have a video stream and a determinable duration and frame rate; and

| Limit | Setting (default) |
|---|---|
| size | `MAX_UPLOAD_BYTES` (2 GiB) |
| duration | `MAX_VIDEO_DURATION_SECONDS` (3600) |
| width / height | `MAX_VIDEO_WIDTH` (3840), `MAX_VIDEO_HEIGHT` (2160) |
| frame rate | `MAX_OUTPUT_FPS` (60) |

`collect_violations()` returns every violation of an already-probed file;
`file_sha256()` produces the manifest checksums.

**`app/media/lut.py`.** The three built-in looks (`cinematic_warm`,
`teal_orange`, `neutral_film`) are **generated from code** as Adobe `.cube`
files (17³ entries, red-fastest ordering), so they are reproducible and
diffable. `ensure_lut(name_or_path)` returns an existing file as-is, else
generates the named built-in into the first writable asset directory.
`validate_lut(path)` checks the `.cube` extension, non-empty file, size
≤ 8 MiB, presence of `LUT_3D_SIZE`, and that the number of numeric data rows
equals `LUT_3D_SIZE³`; out-of-range values are rejected.
`lut_fingerprint(path)` returns name, cube size and SHA-256, recorded in the
grade stage's `meta`.

## 8. Jobs layer

**`models.py`** — `JobRequest` (input URI, stages, options, output dir,
idempotency key), `StageResult`, and the persisted `JobRecord` (status,
request, stage results, outputs, error, cancel flag, manifest, timestamps,
GPU validation status). `new_job_id()` returns `fj_<8 hex>`.

**`states.py`** — the state machine of §3.

**`repository.py`** — `JobRepository` is an ABC with a narrow interface so a
PostgreSQL implementation can replace it without touching the runner or the
API (`repository_from_settings` raises `NotImplementedError` for `postgres://`
today). `SQLiteJobRepository` uses one `sqlite3` connection
(`check_same_thread=False`, 30 s busy timeout) guarded by a re-entrant lock
around every write; a `jobs(...)` table with JSON columns for request,
metadata, stage results, outputs, error and manifest; a **unique partial
index** `jobs_idempotency_key ON jobs (idempotency_key) WHERE idempotency_key
IS NOT NULL` (the database, not the application, enforces idempotency); an
index on `created_at`; and `request_cancel()` refusing to cancel a job that
already reached a terminal state.

> The connection issues `PRAGMA journal_mode=WAL` and
> `PRAGMA synchronous=NORMAL`, so the API can read while the runner writes;
> writes are additionally serialised by an in-process lock. Single-node only.

**`runner.py`** — `JobRunner` owns a `ThreadPoolExecutor(max_workers=
MAX_CONCURRENT_JOBS, thread_name_prefix="framefrog-job")`. `submit()`
rebuilds a `JobRequest` from the persisted row and enqueues `_execute`, which
transitions the job to `acquiring_input`, calls `run_job` with a `progress`
callback and a `cancel_check` reading the repository's cancel flag, then
finalises the row. `shutdown(wait=False)` cancels pending futures on stop.
Cancellation is checked **between stages** and, for interpolation, **per
frame** (`_cancel_check`).

**`pipeline.py` — the manifest.** `run_job` returns and persists a manifest
with: `job_id`, `status`, `started_at`, `finished_at`, `elapsed_s`;
`request` (input URI, stages, **redacted** options); `input` /
`input_metadata` (the ffprobe result); `stages[]` (`stage`, `status`,
`duration_s`, `output`, `meta`, plus `error`/`traceback` on failure);
`artifacts` (`source`, `scenes`, `matte`, `master`, `proxy`, `poster`);
`outputs[]` (kind, path, `size_bytes`, `sha256`, master media metadata);
`gpu_validation_status`; and `error` (`{code, message, details}`). It is
written to `WORK_DIR/jobs/<job_id>/manifest.json` and stored in
`manifest_json`. Partial work is never discarded silently: a failed stage is
recorded with its error and the manifest keeps pointing at the artifacts
produced before the failure. `_public_options()` removes keys starting with
`_` and replaces `webhook_secret`, `api_key` and `s3_secret_access_key` with
`***`.

**`fanout.py`, `queue.py`, `worker.py`** — per-shot fan-out. `fanout.py`
splits the input at scene cuts (video only), runs one task per shot through
the **same** `stage.run()` contract, and stitches the results (`concat`
demuxer + `-c copy`), muxing the untouched continuous audio once at stitch
time so per-shot AAC priming cannot create seams. Modes: `inline` (a bounded
`ThreadPoolExecutor`; FFmpeg is a subprocess, so threads are the right unit)
or `queue`. `queue.py` provides two backends behind one contract:
**`FileQueue`** (a directory with `pending/`, `working/`, `done/`; a claim is
an atomic `os.replace` rename, and messages whose lease expired return to
`pending`) and **`SqsQueue`** (SQS via boto3, same ack/nack semantics).
Delivery is at-least-once, so workers must be idempotent. `worker.py` is a
standalone drainer (`python -m app.jobs.worker --queue-url … --results-dir
…`), with `--once` for cron-driven GPU boxes. **Currently**
`fanout.fanout_stage()` is implemented and unit-tested, but `pipeline.py`
calls the stage registry directly and never invokes it — fan-out is reachable
from the library and its tests, not from `POST /v1/jobs`.

## 9. Configuration

`app/core/config.py` uses pydantic-settings (`BaseSettings`, `.env` file,
case-insensitive names, `extra="ignore"`). `get_settings()` is an
`lru_cache`d singleton; `operator_settings()` returns a copy with
`allow_external_local_inputs=True` for the local CLI. No other module reads
`os.environ` for behaviour that matters (only the legacy `FRAMEFROG_*` names
are honoured by `app/core/ops.py` and the model discovery code).

| Variable | Default | Notes |
|---|---|---|
| `APP_ENV` | `development` | `development` / `test` / `production` |
| `LOG_LEVEL` | `INFO` | validated against the stdlib level names |
| `API_KEY` | *(unset)* | `SecretStr`; **mandatory in production** |
| `WORK_DIR` | `./work` | job directories, local storage root |
| `DATABASE_URL` | `sqlite:///./work/framefrog.db` | only `sqlite://` is implemented |
| `STORAGE_BACKEND` | `local` | `local` or `s3` |
| `MAX_UPLOAD_BYTES` | `2147483648` | input size limit |
| `MAX_VIDEO_DURATION_SECONDS` | `3600` | input duration limit |
| `MAX_VIDEO_WIDTH` | `3840` | input width limit |
| `MAX_VIDEO_HEIGHT` | `2160` | input height limit |
| `MAX_OUTPUT_FPS` | `60` | input frame-rate limit and interpolation ceiling |
| `MAX_CONCURRENT_JOBS` | `2` | runner thread-pool size |
| `FFMPEG_PATH` | `ffmpeg` | binary name or path |
| `FFPROBE_PATH` | `ffprobe` | binary name or path |
| `S3_ENDPOINT_URL` | *(unset)* | for R2 / MinIO |
| `S3_REGION` | `us-east-1` | |
| `S3_BUCKET` | *(unset)* | **required when `STORAGE_BACKEND=s3`** |
| `S3_ACCESS_KEY_ID` | *(unset)* | `SecretStr` |
| `S3_SECRET_ACCESS_KEY` | *(unset)* | `SecretStr` |
| `S3_INPUT_PREFIX` | `inputs` | |
| `S3_INTERMEDIATE_PREFIX` | `intermediate` | |
| `S3_OUTPUT_PREFIX` | `outputs` | |
| `PRESIGNED_URL_EXPIRY_SECONDS` | `900` | presigned URL lifetime |
| `MODEL_CACHE_DIR` | `./models` | model weight cache |
| `DEEPFILTERNET_ENABLED` | `true` | |
| `RIFE_ENABLED` | `true` | |
| `RIFE_REPOSITORY_PATH` | *(unset)* | |
| `RIFE_MODEL_PATH` | *(unset)* | alternative RIFE graph location |
| `COMPUTE_DEVICE` | `auto` | `auto` / `cpu` / `cuda` |
| `GPU_VALIDATION_REQUIRED` | `false` | fail rather than fall back on GPU stages |
| `ALLOWED_INPUT_ROOTS` | *(empty)* | comma-separated extra local input roots |
| `ALLOW_EXTERNAL_LOCAL_INPUTS` | `false` | operator CLI only; the HTTP API never sets it |

Legacy `FRAMEFROG_*` names, read directly from the environment by
`app/core/ops.py`, `app/models/discovery.py`, `app/media/lut.py`,
`app/media/ffmpeg.py` and `app/jobs/worker.py`: `FRAMEFROG_SIGNING_SECRET`,
`FRAMEFROG_SIGNING_TTL`, `FRAMEFROG_WEBHOOK_SECRET`,
`FRAMEFROG_GPU_HOURLY_USD`, `FRAMEFROG_CPU_HOURLY_USD`, `FRAMEFROG_MODEL_DIR`,
`FRAMEFROG_RIFE_ONNX`, `FRAMEFROG_LUT_DIR`, `FRAMEFROG_FORCE_NVENC`,
`FRAMEFROG_DISABLE_NVENC`, `FRAMEFROG_QUEUE_URL`, `FRAMEFROG_FANOUT_RESULTS`.

**Startup validation.** `LOG_LEVEL` must be a stdlib level name.
`MAX_UPLOAD_BYTES`, `MAX_CONCURRENT_JOBS`, `PRESIGNED_URL_EXPIRY_SECONDS`,
`MAX_VIDEO_DURATION_SECONDS`, `MAX_VIDEO_WIDTH`, `MAX_VIDEO_HEIGHT` and
`MAX_OUTPUT_FPS` must be > 0. **In `production`**: `API_KEY` must be set and
must not be one of the known development values (`""`, `changeme`,
`change-me`, `secret`, `dev`, `test`, `local`) — the process refuses to start
otherwise. `STORAGE_BACKEND=s3` requires `S3_BUCKET`.

## 10. Logging

`app/core/logging.py` renders one JSON object per line on stdout:

```json
{"timestamp":"2026-11-19T10:11:12.345Z","level":"INFO","logger":"framefrog.runner",
 "message":"job_finished","event":"job_finished","job_id":"fj_ab12cd34",
 "duration_seconds":3.141}
```

| Key | Presence |
|---|---|
| `timestamp` | always (UTC, millisecond resolution) |
| `level`, `logger`, `message` | always |
| `event` | when emitted through `log_event` |
| `job_id`, `stage` | only when known |
| `duration_seconds` | only when measured |
| `error_code` | only when an error occurred |

**Unmeasured values are omitted, never zeroed.** The same rule governs GPU
metrics: `app/core/capabilities.py::gpu_metric_fields` returns only
`gpu_validation_status` while the status is `pending`, and the measurement
fields (`gpu_name`, `gpu_seconds`, `peak_vram_mb`, `cuda_version`,
`inference_seconds`) appear only for a validated run. The interpolation stage
records `gpu_validation_status: "pending"` in its metadata for the same
reason.

Redaction helpers: `redact_url(url)` replaces the value of credential-style
query parameters with `***` (used for presigned URLs before logging);
`redact_text(text, secrets)` replaces known secret values.

## 11. What is **not** implemented

Stated plainly, because a reader of the API would otherwise assume these
exist:

| Item | Status |
|---|---|
| 4K / 120 fps delivery target | 120 fps is **not supported**: interpolation is hard-capped at 60 fps (`MAX_INTERPOLATED_FPS` in the API schemas, `MAX_TARGET_FPS` in the interpolate stage) and the API rejects a higher `target_fps`. A 2160-line upscale stage exists (`upscale`, opt-in) but its GPU backends (TensorRT, Real-ESRGAN) are unvalidated on real hardware. |
| Multi-GPU | **Not implemented.** One device only; `COMPUTE_DEVICE` is `auto`/`cpu`/`cuda`, and the CUDA path is not validated. |
| Kubernetes | **Not implemented.** No manifests, no Helm chart, no operator. Deployment is Docker (`Dockerfile`, `Dockerfile.gpu`) or Cerebrium (`cerebrium.toml`). |
| Dashboard / web UI | **Not implemented.** The service exposes a JSON API, `/health`, `/ready` and FastAPI's `/docs`. |
| Billing | **Not implemented.** `app/core/ops.py` computes cost from measured stage seconds, but nothing in the request path calls it and there is no invoice, plan or metering layer. |
| User accounts | **Not implemented.** One shared API key for the whole service; no users, no per-key identity, no ownership model. |
| AI reference-image look matching | **Not implemented.** `color_grade` applies a named built-in look or a `.cube` file. Selecting a look from a reference image is out of scope for this codebase. |
| Multi-region | **Not implemented.** One deployment, one `WORK_DIR`, one SQLite file. |
| Load testing | **Not performed.** No k6/Locust harness, no capacity numbers, no stress results exist. |
| Cost optimisation | **Not implemented.** No model caching strategy, no spot/priority scheduling, no encoder-cost heuristics beyond the `auto` encoder choice. |
| Generated colour looks | Written to `MODEL_CACHE_DIR/luts`, `WORK_DIR/luts` or the system temp directory — never into the source tree. The committed `assets/luts/cinematic_warm.cube` is read from the repository root. |
| Output upload to object storage | Implemented for the S3 backend: after the stages, `pipeline.py` publishes the master, proxy and poster under the output prefix and records the remote URI. The local backend keeps the files in the job workdir. |
| Fan-out in the request path | Wired: `pipeline.py` calls `fanout.fanout_stage()` for the grade and interpolation stages when `options.fanout.mode` is `auto` or `shots`. |
| Signed download links, webhooks, cost in the manifest | Implemented in `app/core/ops.py` and covered by unit tests, but not wired into the API routes or the pipeline manifest. |
| PostgreSQL job store | `repository_from_settings` raises `NotImplementedError` for `postgres://` URLs. |
| WAL mode | Enabled (`PRAGMA journal_mode=WAL`), but the database file is still container/node-local. |
| Horizontal scale-out of the API | The runner is in-process; there is no queue consumer wired to the HTTP service, so N API processes would each run their own pool against one SQLite file. |
