# Cerebrium deployment

**Status: `CONFIGURED_PENDING_CLOUD_VALIDATION`.**

`cerebrium.toml` in this repository has **never been deployed** and has not
been verified against the live Cerebrium platform. Cerebrium's configuration
schema changes between CLI releases, so the section names and keys must be
checked against <https://docs.cerebrium.ai/> for the CLI version you install
before the first deploy.

Nothing in this document reports a deployed service, a cold-start time, a
GPU type that was actually provisioned or a price.

## 1. What is configured

| Setting | Value | Why |
|---|---|---|
| Entrypoint | `app.main:app` | The FastAPI surface accepts jobs, tracks them and runs them in-process |
| Python | 3.11 | Matches `pyproject.toml` |
| System packages | `ffmpeg`, `libgl1`, `libglib2.0-0` | FFmpeg/FFprobe are system binaries; OpenCV needs the GL libraries |
| Dependencies | `requirements.txt` | Cerebrium installs from a requirements file; the source of truth is `pyproject.toml` |
| Pre-build | `python scripts/download_models.py` | Fetches the RIFE graph so a cold start does not wait for 21 MB |
| GPU | `L40S`, one per instance | Full decode→encode chain; change to the type the credits can provision |
| Concurrency | 1 | One FFmpeg-heavy job per GPU |
| Timeout | 3600 s | Matches the maximum accepted input duration (`MAX_VIDEO_DURATION_SECONDS`) |
| Scale to zero | `true` | Idle deployments must not burn credits |
| Model cache | `/app/models` | Weights survive between runs of the same instance |
| `COMPUTE_DEVICE` | `cuda` | Selects the CUDA provider |
| `GPU_VALIDATION_REQUIRED` | `true` | A GPU stage that cannot run fails the job instead of falling back to the CPU |

Secrets are referenced as `${VAR}` placeholders and injected by the platform.
No real credential is stored in this repository.

## 2. Deploying (not executed from this repository)

```bash
pip install cerebrium
cerebrium login
cerebrium deploy                     # uses cerebrium.toml
cerebrium logs <deployment-id>       # follow the container logs
```

Before the first deploy:

1. Install the CLI version whose schema matches `cerebrium.toml` and adjust
   the file if the section names differ.
2. Create the bucket and set `S3_BUCKET`, `S3_ENDPOINT_URL` (if the provider
   is R2 or MinIO) and the region.
3. Generate a strong `API_KEY`; the application refuses to start in
   production without one.
4. Decide where the job database lives. **The container-local SQLite file is
   lost when the instance is recycled.** For a first smoke test that is
   acceptable; for a real deployment, either pin a volume or move to
   PostgreSQL (the repository interface is ready, the implementation is not).
5. Confirm the GPU type that the plan can actually provision — `L40S` is a
   default, not a verified fact.

## 3. Verifying a deployment

```bash
# liveness
curl -fsS https://<deployment-host>/health

# readiness (checks FFmpeg, the database and the storage backend)
curl -fsS https://<deployment-host>/ready

# submit a job
curl -fsS -X POST https://<deployment-host>/v1/jobs \
  -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
  -d '{"input_uri": "s3://<bucket>/inputs/<clip>.mp4",
       "stages": ["scene_detect", "audio_cleanup", "color_grade", "deliver"]}'

# poll
curl -fsS https://<deployment-host>/v1/jobs/<job_id> -H "X-API-Key: $API_KEY"
```

A successful deployment run is recorded by the manifest's
`gpu_validation_status` and by the per-stage `provider`/`encoder` fields —
those are the only trustworthy signals that the GPU path really executed.

## 4. Known gaps for the serverless shape

These are known and **not** solved by this configuration:

| Gap | Consequence | Mitigation |
|---|---|---|
| Container-local SQLite | Jobs are lost when the instance recycles | Pin a volume, or implement the PostgreSQL repository |
| No distributed queue | The in-process runner dies with the container | Move to the SQS queue that `app/jobs/queue.py` already supports |
| Per-shot fan-out worker | `app/jobs/worker.py` must run as a separate service | Deploy it as a second service pointed at the same queue |
| Scale-to-zero latency | The first job after an idle period pays a cold start | Measure it during GPU validation |
| Weights in the image | A new image revision re-downloads the model | Use the model cache path |

## 5. Before any beta

- [ ] The Cerebrium schema in `cerebrium.toml` matches the installed CLI.
- [ ] A job completes end to end on the serverless GPU, and the manifest
      records a CUDA provider and the expected encoder.
- [ ] The cold start, the job duration and the cost per video minute are
      measured and recorded in `docs/GPU_VALIDATION.md`.
- [ ] A job that requests `device: "cuda"` fails with a capability error on a
      CPU-only instance (the setting `GPU_VALIDATION_REQUIRED=true` enforces
      this).
- [ ] No credential appears in the repository, the image or the logs.
