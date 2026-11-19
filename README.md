# FrameFrog Pipeline

[![CI](https://github.com/framefrog-ai/framefrog-pipeline/actions/workflows/ci.yml/badge.svg)](https://github.com/framefrog-ai/framefrog-pipeline/actions/workflows/ci.yml)

> **FrameFrog** — AI cloud video post-production, as a pipeline you can actually run.
>
> Audio cleanup → scene detection → 3D LUT grade → frame interpolation → delivery,
> orchestrated per job on serverless GPUs. FFmpeg does the heavy lifting; every
> step is a stateless function with a typed contract.

This is the working backend behind [framefrog.me](https://framefrog.me). Clone it,
run `python demo.py`, and you get a graded, interpolated, delivery-ready master
plus a proxy, a poster frame and a manifest — no footage or models required.

---

## Quickstart

```bash
git clone https://github.com/framefrog-ai/framefrog-pipeline.git
cd framefrog-pipeline
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt        # FFmpeg must be on PATH

# optional model backends (see "Model backends" below)
pip install onnxruntime deepfilternet
python scripts/fetch_models.py         # RIFE v4.9 ONNX, 21 MB

python demo.py                          # end-to-end; uses RIFE if the graph is present
python main.py --input rushes.mov --lut cinematic_warm --target-fps 60
```

`demo.py` synthesises a 6-second, two-scene clip with an audio tone, then runs the
full pipeline. Example output:

```
FrameFrog job fj_ab12cd34  [completed]
  source   640x360 @ 30.0 fps · 6.0 s
  audio_cleanup  ok     0.12s
  scene_detect   ok     0.08s
  color_grade    ok     0.35s
  interpolate    ok     1.40s
  deliver        ok     1.10s
  master   …/05_master.mp4
  proxy    …/05_proxy_720p.mp4
  poster   …/05_poster.jpg
  scenes   …/02_scenes.json
  elapsed  3.1 s
```

---

## Stages

| Stage | What it does | Key options |
|---|---|---|
| `audio_cleanup` | DeepFilterNet3 neural denoise, then −16 LUFS loudness; FFmpeg spectral denoise fallback. Picture stream-copied | `denoise_model`, `lufs`, `highpass`, `lowpass` |
| `scene_detect` | shot boundaries via PySceneDetect, OpenCV histogram fallback | `scene_threshold`, `min_scene_s` |
| `color_grade` | applies a `.cube` look with `lut3d`, optional strength blend | `lut`, `look_strength` |
| `interpolate` | RIFE v4.9 optical-flow interpolation (ONNX Runtime), 60/120 fps; FFmpeg `minterpolate` fallback | `interp_model`, `target_fps`, `rife_onnx`, `rife_provider`, `interp_quality` |
| `deliver` | H.264 master, 720p proxy, poster frame, faststart | `master_crf` |

### Model backends

Both models are optional and both degrade gracefully:

| Backend | Install | Used when |
|---|---|---|
| DeepFilterNet3 | `pip install deepfilternet` (torch) | `denoise_model=auto` and the package imports |
| RIFE v4.9 | `python scripts/fetch_models.py` + `onnxruntime` | `interp_model=auto` and a graph is found |

Resolution order for the RIFE graph: `options["rife_onnx"]` →
`$FRAMEFROG_RIFE_ONNX` → `$FRAMEFROG_MODEL_DIR` → `assets/models/rife*.onnx`.
ONNX Runtime picks CUDA when available, CPU otherwise (`rife_provider` can
force either).

Fallback semantics are explicit and recorded in the manifest:

- `auto` (default) — try the model, fall back to FFmpeg, record
  `fallback_reason`.
- `rife` / `deepfilternet` — a hard requirement; the stage fails with install
  instructions if the runtime is missing.
- No hidden downloads. A job never pulls model weights mid-flight; fetching is
  a separate, reproducible step.

Model weights are not committed — `scripts/fetch_models.py` prints the SHA-256
of the graph it writes, and the manifest records which graph and provider ran.

### Per-shot fan-out

When `scene_detect` has run, `color_grade` and `interpolate` can process each
detected shot independently and stitch the results. This is off by default:

```json
{"fanout": "shots", "fanout_mode": "queue", "fanout_workers": 4}
```

| Option | Values | Meaning |
|---|---|---|
| `fanout` | `off` (default) / `auto` / `shots` | `auto` falls back to the monolithic stage and records `fanout_fallback_reason`; `shots` fails loudly |
| `fanout_mode` | `inline` (default) / `queue` | bounded thread pool, or publish to a queue and consume |
| `fanout_workers` | int | concurrent shot tasks (default ≤4) |
| `queue_url` | `file://path` / `sqs://url` | queue backend; defaults to a file queue inside the job |
| `queue_external` | bool | enqueue only and wait for a separate worker |
| `fanout_timeout_s` | seconds | hard ceiling for result collection |

How it works: shots are split at scene cuts (video only, re-encoded), each
task calls the **same** `stage.run()` contract on its shot, results are
collected in scene order, and the shot videos are concatenated. Audio is never
fanned out — the original continuous track is muxed once at stitch time, so
per-shot AAC priming cannot introduce seams. The stage manifest records
`fanout`, backend, shot count, per-shot durations, cuts and the stitched
duration.

The file queue is claim-by-rename with lease recovery, so multiple worker
processes on one host can share it. For a separate worker (another box, a GPU
pod), run:

```bash
python -m framefrog.worker --queue-url file:///jobs/fj_x/fanout_color_grade/queue \
    --results-dir /jobs/fj_x/fanout_color_grade/results --workers 4
```

With `"queue_external": true` the orchestrator only publishes and waits for the
result files, so both sides need shared storage. `sqs://` uses the same
payloads and ack/nack semantics through boto3.

All stages share one signature:

```python
def run(input_path: str, workdir: str, options: dict) -> StageResult: ...
```

Adding a stage is one file in `framefrog/stages/` plus one line in
`framefrog/stages/__init__.py`. The orchestrator learns nothing else about it.

### Built-in looks

`assets/luts/*.cube` are generated deterministically by `framefrog/lut.py`:

- `cinematic_warm` — warm highs, gently cooled shadows, filmic S-curve
- `teal_orange` — split-tone, teal shadows / orange skin
- `neutral_film` — clean filmic contrast, mild saturation

Point `--lut` at any `.cube` file to use your own. Every job manifest records
the LUT's name, cube size and SHA-256 so a grade is reproducible.

---

## Job layout and manifest

```
outputs/jobs/fj_ab12cd34/
  00_input.mp4
  01_audio_clean.mp4
  02_scenes.json
  03_graded.mp4
  04_interpolated.mp4
  05_master.mp4
  05_proxy_720p.mp4
  05_poster.jpg
  manifest.json
```

The manifest is the audit trail: request, source probe, per-stage status and
timings, artifacts, liveness. If a stage fails, the manifest records the FFmpeg
tail and everything produced up to that point — partial work is never silently
discarded.

```json
{
  "job_id": "fj_ab12cd34",
  "status": "completed",
  "input": { "width": 640, "height": 360, "fps": 30.0, "duration_s": 6.0 },
  "stages": [ { "stage": "color_grade", "status": "completed", "duration_s": 0.35,
                "meta": { "lut": "cinematic_warm.cube", "strength": 1.0, "sha256": "…" } },
              { "stage": "interpolate", "status": "completed",
                "meta": { "interp_model": "RIFE v4.9 (ONNX Runtime)",
                          "provider": "CPUExecutionProvider", "output_fps": 60.0 } } ],
  "artifacts": { "master": "…/05_master.mp4", "proxy": "…", "poster": "…", "scenes": "…" }
}
```

---

## HTTP API

```bash
uvicorn framefrog.api:app --port 8080
```

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | capabilities + FFmpeg availability |
| `GET` | `/stages` | stage catalogue |
| `POST` | `/post_production` | queue a job, returns `202 {job_id}` |
| `GET` | `/jobs/{id}` | manifest (memory, falls back to disk); adds `download_url` when signing is on |
| `GET` | `/jobs/{id}/download` | master MP4; requires `?token=&expires=` when signing is on |

```bash
curl -X POST localhost:8080/post_production \
  -H 'content-type: application/json' \
  -d '{"input_uri": "rushes/spot-01.mov",
       "options": {"lut": "teal_orange", "look_strength": 0.7, "target_fps": 60}}'
```

S3 inputs work when `boto3` (or the AWS CLI) is configured:
`"input_uri": "s3://bucket/rushes/spot-01.mov"`.

---

## Operations

Three opt-in pieces, all stdlib-only, all recorded in the manifest:

**Signed download links.** Set `FRAMEFROG_SIGNING_SECRET` and the API only serves
the master with a valid HMAC token. `GET /jobs/{id}` then carries a ready-to-use
`download_url`; `FRAMEFROG_SIGNING_TTL` controls link lifetime (default 1 hour).
Without the secret (local dev) downloads stay open — the `/health` probe reports
which mode is active under `ops.signing`.

**Job webhooks.** Pass `options.webhook_url` (or `--webhook-url`) and the final
manifest is POSTed when the job finishes — success or failure. With
`FRAMEFROG_WEBHOOK_SECRET` set, the body is signed:

```
X-FrameFrog-Signature: sha256=<hex HMAC of the raw body>
```

Delivery is best-effort with 3 attempts and backoff; the outcome lands in
`manifest.webhook` (`delivered`, `attempts`, `status`, `error`). The hook URL is
redacted before recording so tokens never leak into the audit trail.

**Cost accounting.** Every manifest gets `cost`: measured `gpu_seconds` /
`cpu_seconds`, per-stage class and cost. A stage is billed as GPU only if it
actually used a GPU backend (DeepFilterNet or RIFE); everything else is CPU.
Rates default to **0.0** — the manifest reports seconds but never invents a
price. Set `FRAMEFROG_GPU_HOURLY_USD` / `FRAMEFROG_CPU_HOURLY_USD` (or
`--gpu-rate` / `--cpu-rate`) to turn seconds into dollars using your provider's
rate card.

```json
"cost": {
  "rates": {"gpu_hourly_usd": 3.0, "cpu_hourly_usd": 0.3},
  "gpu_seconds": 72.0, "cpu_seconds": 18.0,
  "stages": [{"stage": "interpolate", "class": "gpu", "seconds": 36.0, "usd": 0.03}],
  "total_usd": 0.0615
}
```

---

## Deploy

```bash
pip install cerebrium
cerebrium deploy          # reads cerebrium.toml
```

`cerebrium.toml` ships both shapes: a FastAPI service (`framefrog.api:app`) and a
function worker (`main:post_production`). The `Dockerfile` is the same image used
locally; FFmpeg is the only system package.

The image bundles the RIFE backend by default (`onnxruntime` + the 21 MB graph at
build time). Build lean and use the FFmpeg interpolation fallback with:

```bash
docker build --build-arg WITH_RIFE=0 -t framefrog .
```

In production, the in-process `BackgroundTasks` is replaced by a queue consumer
(SQS / Cerebrium async), so workers scale independently of the API and each
stage can fan out per shot.

---

## Tests

```bash
python -m pytest -q
```

Coverage: LUT generation and fingerprints (no FFmpeg), scene detection on a
synthetic hard cut (OpenCV only), model resolution and fallback semantics,
signed-link HMACs, webhook delivery/redaction, cost accounting, file-queue
claim/ack/nack semantics, per-shot split/queue/stitch on a real FFmpeg clip,
and API smoke tests. The RIFE integration test runs only when
both the graph and `onnxruntime` are present; FFmpeg-dependent tests skip
automatically when the binary is missing.

CI (`.github/workflows/ci.yml`) runs the suite on Ubuntu with FFmpeg on every
push to `main` and every pull request. Model weights are not fetched in CI, so
the RIFE test skips there by design while the FFmpeg paths run for real.

---

## Why it is built this way

- **Stateless stages, one contract.** Each stage can run as its own GPU function,
  be retried in isolation, or be swapped for a model-backed version without
  touching the rest.
- **FFmpeg is the substrate, not a hidden detail.** Filter graphs live in the
  stage files where a colourist or engineer can read them.
- **Honest "AI".** Look selection, denoise model choice and interpolation quality
  are inputs to a deterministic execution layer. DeepFilterNet and RIFE plug into
  the same stage contract as the FFmpeg fallbacks; the manifest, artifacts and
  API are identical either way, and every fallback is recorded.
- **Reproducibility.** LUT hashes, model graph names, ONNX providers and stage
  timings are recorded per job.

## Roadmap

- [x] Real, runnable stages with FFmpeg
- [x] Deterministic LUT library + fingerprints
- [x] Manifest, API, tests, Docker
- [x] DeepFilterNet3 behind `audio_cleanup`
- [x] RIFE v4.9 (ONNX Runtime) behind `interpolate`
- [x] Per-shot fan-out via queue
- [x] Signed upload/download URLs + job webhooks
- [x] Model-aware cost accounting (GPU-seconds per stage)

---

© FrameFrog — [framefrog.me](https://framefrog.me) · contact@framefrog.me
