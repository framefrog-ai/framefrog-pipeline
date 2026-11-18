"""HTTP API — the thin serverless surface around the pipeline.

    uvicorn framefrog.api:app --port 8080

Endpoints
---------
GET  /health                     capability probe
GET  /stages                     stage list + descriptions
POST /post_production            queue a job (background)
GET  /jobs                       recent jobs (in process)
GET  /jobs/{job_id}              manifest (in process, falls back to disk)
GET  /jobs/{job_id}/download     master file when the job is done

Jobs run in a FastAPI BackgroundTask. On Cerebrium the same module is the
container entrypoint; in production the background task becomes an SQS / queue
consumer so workers scale independently of the API.
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from framefrog.pipeline import describe, run_job
from framefrog.types import JobRequest

OUTPUT_DIR = os.environ.get("FRAMEFROG_OUTPUT_DIR", os.path.join(os.getcwd(), "outputs"))
os.makedirs(OUTPUT_DIR, exist_ok=True)

app = FastAPI(title="FrameFrog Pipeline", version="0.3.0")
JOBS: Dict[str, dict] = {}


class PostProductionRequest(BaseModel):
    input_uri: str = Field(..., description="local path or s3://bucket/key")
    stages: Optional[List[str]] = Field(
        default=None,
        description="stage names in order; defaults to the full pipeline",
    )
    options: Dict = Field(default_factory=dict)


def _run(job_id: str, req: JobRequest) -> None:
    JOBS[job_id] = run_job(
        req,
        job_id=job_id,
        progress=lambda status, m: JOBS.__setitem__(job_id, {**JOBS.get(job_id, {}), **m}),
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok", **describe()}


@app.get("/stages")
def stages() -> dict:
    return {
        "stages": [
            {"name": "audio_cleanup", "summary": "denoise, de-hum, -16 LUFS loudness"},
            {"name": "scene_detect", "summary": "shot boundaries (PySceneDetect or OpenCV)"},
            {"name": "color_grade", "summary": "3D LUT look, blendable strength"},
            {"name": "interpolate", "summary": "60/120 fps motion interpolation"},
            {"name": "deliver", "summary": "master, 720p proxy, poster frame"},
        ]
    }


@app.post("/post_production", status_code=202)
def post_production(body: PostProductionRequest, background: BackgroundTasks) -> dict:
    job_id = f"fj_{os.urandom(4).hex()}"
    req = JobRequest(
        input_uri=body.input_uri,
        stages=body.stages or list(describe()["stages"]),
        options=body.options,
        output_dir=OUTPUT_DIR,
    )
    JOBS[job_id] = {"job_id": job_id, "status": "queued", "request": body.model_dump()}
    background.add_task(_run, job_id, req)
    return {"job_id": job_id, "status": "queued"}


@app.get("/jobs")
def list_jobs() -> dict:
    return {"jobs": list(JOBS.values())[-20:]}


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    if job_id in JOBS:
        return JOBS[job_id]
    for root, _dirs, files in os.walk(OUTPUT_DIR):
        if "manifest.json" in files and os.path.basename(root) == job_id:
            with open(os.path.join(root, "manifest.json"), encoding="utf-8") as fh:
                return json.load(fh)
    raise HTTPException(status_code=404, detail="job not found")


@app.get("/jobs/{job_id}/download")
def download(job_id: str) -> FileResponse:
    manifest = get_job(job_id)
    master = (manifest.get("artifacts") or {}).get("master")
    if not master or not os.path.exists(master):
        raise HTTPException(status_code=409, detail="job has no master yet")
    return FileResponse(master, media_type="video/mp4", filename=os.path.basename(master))
