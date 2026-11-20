# FrameFrog API — v1

The HTTP surface of the pipeline. Every example below is the real contract;
the schemas live in `app/api/schemas.py` and are enforced by the service.

- Base URL in development: `http://127.0.0.1:8080`
- Interactive documentation: `/docs` (Swagger UI), `/openapi.json`
- Authentication: `X-API-Key: <key>` **or** `Authorization: Bearer <key>`

Authentication is mandatory outside development mode: `APP_ENV=production`
without `API_KEY` refuses to start.

---

## `GET /health`

Liveness. Never fails because a dependency is missing.

```json
{
  "status": "ok",
  "name": "framefrog-pipeline",
  "version": "0.8.0",
  "uptime_s": 42.1,
  "app_env": "production"
}
```

## `GET /ready`

Readiness. **503** when FFmpeg, FFprobe, the database or the storage backend
is unusable.

```json
{
  "status": "ready",
  "checks": {
    "ffmpeg":   {"ok": true, "version": "ffmpeg version 8.1.1"},
    "ffprobe":  {"ok": true, "version": "ffprobe version 8.1.1"},
    "database": {"ok": true, "backend": "SQLiteJobRepository"},
    "storage":  {"ok": true, "backend": "local"},
    "runner":   {"ok": true, "max_workers": 2, "queued": 0},
    "models":   {"ok": true, "deepfilternet": false, "onnxruntime": true,
                 "rife_model": "rife49.onnx", "rife_available": true},
    "gpu":      {"ok": true, "available": false}
  }
}
```

The optional model runtimes are reported but never make the service unready:
every one of them has a documented fallback.

---

## `POST /v1/jobs`

Creates a job and queues it. **202 Accepted** — the response returns as soon
as the job is persisted; processing happens in the background.

```json
{
  "input_uri": "s3://my-bucket/inputs/rushes.mp4",
  "stages": ["scene_detect", "audio_cleanup", "interpolate", "color_grade", "deliver"],
  "options": {
    "scene_detect": {"threshold": 27.0, "minimum_scene_length_seconds": 0.5},
    "audio_cleanup": {"enabled": true, "target_lufs": -16.0, "backend": "auto"},
    "interpolate": {"enabled": true, "target_fps": 60, "device": "auto"},
    "color_grade": {"enabled": false, "lut_uri": null, "intensity": 1.0},
    "fanout": {"mode": "off", "workers": 4, "mode_backend": "inline"},
    "deliver": {"container": "mp4", "video_codec": "h264", "audio_codec": "aac",
                "crf": 20, "preset": "medium"}
  },
  "idempotency_key": "rushes-2026-11-19-a"
}
```

Rules the service enforces before anything is persisted:

| Field | Rule |
|---|---|
| `input_uri` | `s3://bucket/key` or a local path inside `WORK_DIR` (plus `ALLOWED_INPUT_ROOTS`); `http(s)` is disabled |
| `stages` | known stage names only, no duplicates; unknown names → 422 |
| `options.interpolate.target_fps` | ≤ `MAX_OUTPUT_FPS` (60) → otherwise 422 |
| `options.color_grade.intensity` | must be `1.0` (partial blending is not implemented) |
| `options.color_grade.lut_uri` | must be a `.cube` file, no path traversal, and it must resolve inside a trusted LUT directory |
| `options.deliver.preset` | one of the x264 preset names |
| `idempotency_key` | letters, digits, `-`, `_`, `.`; a repeated key → **409** |

A stage with an explicit `"enabled": false` is not executed: the manifest
records it with status `skipped`.

Response:

```json
{
  "job_id": "fj_2a95b1b2",
  "status": "queued",
  "current_stage": null,
  "progress_percent": 0,
  "created_at": "2026-11-19T20:15:03+00:00",
  "started_at": null,
  "completed_at": null,
  "input_metadata": {},
  "stage_results": [],
  "outputs": [],
  "error": null,
  "gpu_validation_status": "pending"
}
```

---

## `GET /v1/jobs/{job_id}`

The job as it is now.

```json
{
  "job_id": "fj_2a95b1b2",
  "status": "completed",
  "current_stage": null,
  "progress_percent": 100,
  "created_at": "2026-11-19T20:15:03+00:00",
  "started_at": "2026-11-19T20:15:03+00:00",
  "completed_at": "2026-11-19T20:16:04+00:00",
  "input_metadata": {
    "duration_s": 6.0, "width": 320, "height": 180, "fps": 30.0,
    "video_codec": "h264", "has_audio": true, "audio_codec": "aac",
    "audio_sample_rate": 48000, "size_bytes": 470000
  },
  "stage_results": [
    {"stage": "scene_detect", "status": "completed", "duration_s": 0.59,
     "meta": {"method": "pyscenedetect", "scene_count": 2, "cuts_s": [3.0]}},
    {"stage": "color_grade", "status": "skipped", "duration_s": 0.0,
     "meta": {"reason": "disabled by the request options"}}
  ],
  "outputs": [
    {"kind": "master", "path": "/app/work/jobs/fj_2a95b1b2/work/05_master.mp4",
     "size_bytes": 600000, "sha256": "…", "width": 320, "height": 180,
     "fps": 60.0, "duration_s": 6.0,
     "download_url": "https://…/v1/jobs/fj_2a95b1b2/download?expires=…&token=…"}
  ],
  "error": null,
  "gpu_validation_status": "pending"
}
```

### Job states

```
queued → acquiring_input → validating → processing → uploading → completed
                 │              │            │           │
                 └──────────────┴────────────┴───────────┴──→ failed | cancelled
```

Only the transitions in `app/jobs/states.py` are legal; an illegal move
raises instead of overwriting state.

### Stage states

`completed` · `failed` · `skipped` (disabled by the request) ·
`pending_gpu_validation` (the stage needs a capability this machine does not
have) · `cancelled`.

### GPU metrics

`gpu_validation_status` stays `pending` until a GPU run happens. The fields
`gpu_name`, `gpu_seconds`, `peak_vram_mb`, `cuda_version` and
`inference_seconds` are **absent**, never zero — an unmeasured value is not a
measured zero.

---

## `POST /v1/jobs/{job_id}/cancel`

Asks the runner to stop. The flag is checked between stages and inside the
RIFE frame loop. A job that already finished answers **409**.

```json
{"job_id": "fj_2a95b1b2", "status": "processing", "cancel_requested": true}
```

## `GET /v1/jobs/{job_id}/outputs`

The artifacts, each with a download link when one can be issued (a presigned
object-store URL, or an HMAC-signed link to the download route).

## `GET /v1/jobs/{job_id}/download`

Streams one artifact.

```
GET /v1/jobs/{job_id}/download?artifact=master&expires=1785000000&token=<hmac>
```

- `artifact` — `master`, `proxy` or `poster` (default `master`)
- `expires` / `token` — the signature from `download_url`; a tampered or
  expired link is refused with **422**

Without `SIGNING_SECRET` no signed link is issued at all.

## `POST /v1/jobs/upload-url`

Returns where to PUT a source file and the URI to use as the job input.

```json
{"filename": "rushes.mp4"}
```

```json
{
  "input_uri": "s3://my-bucket/inputs/uploads/<token>/rushes.mp4",
  "upload_url": "https://…?X-Amz-Signature=…",
  "method": "PUT",
  "expires_in": 900,
  "key": "inputs/uploads/<token>/rushes.mp4"
}
```

Object storage only: the local backend has nothing to sign and answers
**409**.

---

## Errors

Every error has the same shape and never leaks internal detail:

```json
{
  "error": {
    "code": "validation_error",
    "message": "unknown stage(s): nope; known: audio_cleanup, scene_detect, …",
    "details": {}
  }
}
```

| Code | Status | Meaning |
|---|---|---|
| `validation_error` | 422 | the request is not acceptable |
| `media_validation_error` | 422 | the input breaks a configured limit |
| `authentication_error` | 401 | missing or wrong API key |
| `not_found` | 404 | unknown job or artifact |
| `conflict` | 409 | duplicate idempotency key, finished job, unsupported backend |
| `capability_unavailable` | 503 | a required capability (CUDA, a model) is missing |
| `storage_error` | 502 | object storage failed |
| `internal_error` | 500 | anything else |

The detail (FFmpeg stderr, tracebacks, file system paths) goes to the
structured log, not to the response.

## Limits

| Limit | Setting | Default |
|---|---|---|
| Input size | `MAX_UPLOAD_BYTES` | 2 GiB |
| Input duration | `MAX_VIDEO_DURATION_SECONDS` | 3600 s |
| Input resolution | `MAX_VIDEO_WIDTH` × `MAX_VIDEO_HEIGHT` | 3840 × 2160 |
| Frame rate | `MAX_OUTPUT_FPS` | 60 |
| Interpolation target | — | 60 (120 fps is not implemented) |
| RIFE input resolution | — | 1920 × 1080 |
| Parallel jobs | `MAX_CONCURRENT_JOBS` | 2 |
| Grade intensity | — | 1.0 |

## A complete run

```bash
# 1. upload
URI=$(curl -s -X POST localhost:8080/v1/jobs/upload-url \
  -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
  -d '{"filename": "rushes.mp4"}' | jq -r .input_uri)

# 2. put the file
curl -s -X PUT "$(curl -s -X POST localhost:8080/v1/jobs/upload-url \
  -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
  -d '{"filename": "rushes.mp4"}' | jq -r .upload_url)" \
  --data-binary @rushes.mp4

# 3. create the job
JOB=$(curl -s -X POST localhost:8080/v1/jobs \
  -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
  -d "{\"input_uri\": \"$URI\", \"stages\": [\"scene_detect\", \"color_grade\", \"deliver\"]}" \
  | jq -r .job_id)

# 4. poll
curl -s localhost:8080/v1/jobs/$JOB -H "X-API-Key: $API_KEY" | jq '.status, .outputs'
```
