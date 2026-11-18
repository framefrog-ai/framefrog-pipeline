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

python demo.py                          # end-to-end, ~30 s on a laptop
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
| `audio_cleanup` | high/low-pass, FFT denoise, −16 LUFS loudness; picture stream-copied | `lufs`, `denoise`, `highpass`, `lowpass` |
| `scene_detect` | shot boundaries via PySceneDetect, OpenCV histogram fallback | `scene_threshold`, `min_scene_s` |
| `color_grade` | applies a `.cube` look with `lut3d`, optional strength blend | `lut`, `look_strength` |
| `interpolate` | 60/120 fps `minterpolate`; no-op if already high-fps | `target_fps`, `interp_quality` (`fast`/`high`) |
| `deliver` | H.264 master, 720p proxy, poster frame, faststart | `master_crf` |

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
                "meta": { "lut": "cinematic_warm.cube", "strength": 1.0, "sha256": "…" } } ],
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

In production, the in-process `BackgroundTasks` is replaced by a queue consumer
(SQS / Cerebrium async), so workers scale independently of the API and each
stage can fan out per shot.

---

## Tests

```bash
python -m pytest -q
```

Coverage: LUT generation and fingerprints (no FFmpeg), scene detection on a
synthetic hard cut (OpenCV only), a real FFmpeg integration run, and API smoke
tests. FFmpeg-dependent tests skip automatically when the binary is missing.

---

## Why it is built this way

- **Stateless stages, one contract.** Each stage can run as its own GPU function,
  be retried in isolation, or be swapped for a model-backed version without
  touching the rest.
- **FFmpeg is the substrate, not a hidden detail.** Filter graphs live in the
  stage files where a colourist or engineer can read them.
- **Honest "AI".** Look selection, denoise model choice and interpolation quality
  are inputs to a deterministic execution layer. When a learned model is wired
  in (DeepFilterNet for audio, RIFE for interpolation), it replaces a stage
  function — the manifest, artifacts and API stay identical.
- **Reproducibility.** LUT hashes, stage timings and source probes are recorded
  per job.

## Roadmap

- [x] Real, runnable stages with FFmpeg
- [x] Deterministic LUT library + fingerprints
- [x] Manifest, API, tests, Docker
- [ ] DeepFilterNet / Demucs dereverb behind `audio_cleanup`
- [ ] RIFE / FILM behind `interpolate` on GPU
- [ ] Per-shot fan-out via queue
- [ ] Signed upload/download URLs + job webhooks

---

© FrameFrog — [framefrog.me](https://framefrog.me) · contact@framefrog.me
