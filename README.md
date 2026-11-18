# FrameFrog Pipeline

> **FrameFrog** — AI Cloud Video Post-Production Pipeline.
> Automated audio cleanup, scene detection, AI color grading with 3D LUTs, and
> frame interpolation — orchestrated on **serverless GPUs**.

This repository is the starter backend for the pipeline that powers
[framefrog.me](https://framefrog.me). It defines a composable, stage-based
architecture where each processing step is an independent, stateless GPU
function that can be run alone or chained end-to-end.

---

## Architecture at a glance

```
            ┌──────────────────────────────────────────────────────────┐
            │                     API / Orchestrator                     │
            │            POST /post_production  (any client)             │
            └───────────────┬───────────────────────────────┬───────────┘
                            │                               │
        ┌───────────────────▼──────┐         ┌──────────────▼─────────────┐
        │  Object storage (S3)     │         │  Job queue / event bus      │
        │  rushes   →   masters    │         │  (optional for fan-out)     │
        └───────────────────┬──────┘         └──────────────┬─────────────┘
                            │                               │
   ┌─────────────┬──────────▼──────────┬──────────────┬─────▼───────────┐
   │ audio_clean │   scene_detect      │  color_grade │  interpolate    │
   │ (denoise,   │   (shot boundaries, │  (AI look    │  (motion-compen │
   │  dereverb,  │    markers, smart   │   matching,  │   sated 60/120  │
   │  LUFS)      │    proxies)         │   3D LUTs)   │   fps)          │
   └─────────────┴──────────┬──────────┴──────────────┴─────┬───────────┘
                            │                               │
                         ┌──▼───────────────────────────────▼──┐
                         │        pack & normalize (FFmpeg)     │
                         │  mux · loudness · delivery masters   │
                         └──────────────────────────────────────┘
```

### Design principles

- **Stateless stages.** Each stage takes an input object key + params and returns
  an output object key. No shared mutable state between stages.
- **Serverless GPUs.** Stages deploy as containerized GPU functions (see
  `cerebrium.toml`), scale-to-zero when idle, and fan out per-shot under load.
- **Object-storage handoff.** Stages exchange artifacts through an S3-compatible
  bucket, so any stage can be retried or swapped independently.
- **Reproducibility.** FFmpeg, CUDA, model weights, and LUTs are pinned. The
  same input + params must yield the same output.

---

## Stages

| Stage           | Input                         | Output                        | Notes                              |
|-----------------|-------------------------------|-------------------------------|------------------------------------|
| `audio_cleanup` | video/audio stream            | cleaned audio track           | denoise, dereverb, LUFS normalize  |
| `scene_detect`  | video                         | scene/shot boundary list      | drives per-shot grading & proxies  |
| `color_grade`   | video + reference/LUT         | graded video                  | AI look matching + 3D LUT engine   |
| `interpolate`   | video                         | high-fps video                | motion-compensated 60/120 fps      |
| `deliver`       | graded/interpolated video     | masters + proxies             | mux, normalize, export             |

---

## Quickstart (local scaffold)

```bash
git clone https://github.com/framefrog-ai/framefrog-pipeline.git
cd framefrog-pipeline
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py --input ./sample.mp4 --stages audio_cleanup,scene_detect
```

> The scaffold uses placeholder implementations. Wire in real models
> (e.g., Demucs/DeepFilterNet for audio, PySceneDetect for scenes, OpenCV/FFmpeg
> for grading and interpolation) per stage.

## Deploy (serverless GPU)

```bash
pip install cerebrium
cerebrium deploy            # reads cerebrium.toml
```

---

## Contract (draft)

Request:

```json
POST /post_production
{
  "input_uri": "s3://rushes/spot-01.mov",
  "stages": ["audio_cleanup", "scene_detect", "color_grade", "interpolate"],
  "options": { "target_fps": 60, "lut": "cinematic_warm" }
}
```

Response:

```json
{ "job_id": "fj_123", "status": "queued", "outputs": {} }
```

---

## Roadmap

- [ ] Real model integrations per stage
- [ ] Per-shot fan-out via queue
- [ ] LUT library + brand kits
- [ ] Job status webhooks
- [ ] Cost/telemetry dashboard

---

© FrameFrog — [framefrog.me](https://framefrog.me) · contact@framefrog.me
