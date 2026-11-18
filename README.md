# FrameFrog Pipeline

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
| `GET` | `/jobs/{id}` | manifest (memory, falls back to disk) |
| `GET` | `/jobs/{id}/download` | master MP4 |

```bash
curl -X POST localhost:8080/post_production \
  -H 'content-type: application/json' \
  -d '{"input_uri": "rushes/spot-01.mov",
       "options": {"lut": "teal_orange", "look_strength": 0.7, "target_fps": 60}}'
```

S3 inputs work when `boto3` (or the AWS CLI) is configured:
`"input_uri": "s3://bucket/rushes/spot-01.mov"`.

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
synthetic hard cut (OpenCV only), model resolution and fallback semantics, a
real FFmpeg integration run, and API smoke tests. The RIFE integration test runs
only when both the graph and `onnxruntime` are present; FFmpeg-dependent tests
skip automatically when the binary is missing.

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
- [ ] Per-shot fan-out via queue
- [ ] Signed upload/download URLs + job webhooks
- [ ] Model-aware cost accounting (GPU-seconds per stage)

---

© FrameFrog — [framefrog.me](https://framefrog.me) · contact@framefrog.me
