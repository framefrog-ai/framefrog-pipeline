# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Per-shot fan-out: split at scene cuts, grade/interpolate shots in parallel.

Why per shot? A 40-shot commercial grades and interpolates faster as N
independent pieces than as one long file, and a failed shot can be retried
without redoing the reel. The stages stay untouched: each shot is just a
shorter input to the same ``run(input, workdir, options)`` contract.

Two execution modes, one result:

* ``inline`` — a bounded thread pool (FFmpeg is a subprocess, so threads are
  the right unit). This is the default.
* ``queue`` — tasks are published to a queue; consumers execute them and write
  result JSON. Locally the orchestrator runs the consumers; set
  ``queue_external`` to let ``python -m framefrog.worker`` (or a serverless
  worker) drain the same queue and write into a shared results directory.

Audio is deliberately *not* fanned out. Shots are video-only and the original
continuous audio is muxed once at stitch time, so per-shot AAC priming can
never introduce seams. That is also why the stitched stage re-encodes audio
(once) when the input had it.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app.jobs import queue as queue_mod
from app.media import ffmpeg
from app.stages.base import StageResult

FANOUT_STAGES = ("color_grade", "interpolate", "upscale")
_ORCH_KEYS = {
    "fanout",
    "fanout_stages",
    "fanout_mode",
    "fanout_workers",
    "fanout_timeout_s",
    "fanout_backend",
    "queue_url",
    "queue_external",
    "fanout_keep_shots",
}


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #
def shots_from_scenes(scenes: list[dict[str, Any]], duration: float) -> list[dict[str, Any]]:
    """Normalise a scene list into contiguous, order-preserving shots."""
    if not scenes:
        return []
    shots: list[dict[str, Any]] = []
    prev = 0.0
    for i, scene in enumerate(scenes):
        start = max(prev, float(scene.get("start_s") or 0.0))
        end = float(scene.get("end_s") or duration) if i < len(scenes) - 1 else duration
        end = min(max(end, start), duration or end)
        if end - start >= 0.05:
            shots.append({"index": len(shots), "start_s": round(start, 3), "end_s": round(end, 3)})
            prev = end
    if not shots:
        shots = [{"index": 0, "start_s": 0.0, "end_s": duration}]
    return shots


def split_shots(
    input_path: str,
    shots: list[dict[str, Any]],
    out_dir: str,
    options: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Re-encode each shot (video only) into ``out_dir``; audio stays upstream."""
    os.makedirs(out_dir, exist_ok=True)
    enc_args, _encoder = ffmpeg.video_encode_args(options or {}, crf=12, preset="ultrafast")
    made = []
    for shot in shots:
        start, end = shot["start_s"], shot["end_s"]
        out = os.path.join(out_dir, f"shot_{shot['index']:03d}.mp4")
        ffmpeg.run(
            [
                ffmpeg.ffmpeg_bin(),
                "-y",
                "-ss",
                f"{start:.3f}",
                "-i",
                input_path,
                "-t",
                f"{max(0.05, end - start):.3f}",
                "-map",
                "0:v:0",
                "-an",
                *enc_args,
                "-movflags",
                "+faststart",
                out,
            ]
        )
        made.append({**shot, "path": out})
    return made


def build_tasks(stage: str, shots: list[dict[str, Any]], stage_dir: str, options: dict) -> list[dict]:
    clean = {k: v for k, v in options.items() if k not in _ORCH_KEYS}
    tasks = []
    for shot in shots:
        task_id = f"{stage}_{shot['index']:03d}"
        tasks.append(
            {
                "task_id": task_id,
                "stage": stage,
                "input": shot["path"],
                "shot_index": shot["index"],
                "start_s": shot["start_s"],
                "end_s": shot["end_s"],
                "workdir": os.path.join(stage_dir, "work", task_id),
                "options": clean,
            }
        )
    return tasks


# --------------------------------------------------------------------------- #
# Task execution
# --------------------------------------------------------------------------- #
def run_task(task: dict[str, Any]) -> dict[str, Any]:
    """Execute one shot task with the same stage function the pipeline uses."""
    from app.stages import STAGES

    os.makedirs(task["workdir"], exist_ok=True)
    started = time.time()
    try:
        stage_fn = STAGES[task["stage"]]
        result = stage_fn(task["input"], task["workdir"], task["options"])
        return {
            "task_id": task["task_id"],
            "shot_index": task["shot_index"],
            "status": "completed",
            "output": result.output_uri,
            "meta": result.meta,
            "duration_s": round(time.time() - started, 3),
        }
    except Exception as exc:  # recorded, not swallowed
        return {
            "task_id": task["task_id"],
            "shot_index": task["shot_index"],
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "duration_s": round(time.time() - started, 3),
        }


def _record(result: dict[str, Any], results_dir: str) -> None:
    os.makedirs(results_dir, exist_ok=True)
    dest = os.path.join(results_dir, f"{result['task_id']}.json")
    tmp = dest + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(result, fh, default=str)
    os.replace(tmp, dest)


def _read_results(results_dir: str, task_ids: list[str]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for task_id in task_ids:
        path = os.path.join(results_dir, f"{task_id}.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                found[task_id] = json.load(fh)
    return found


def run_inline(tasks: list[dict], workers: int) -> list[dict]:
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(run_task, tasks))


def run_queued(
    tasks: list[dict],
    queue: Any,
    results_dir: str,
    workers: int,
    timeout_s: float,
    external: bool = False,
) -> list[dict]:
    """Publish tasks, then either consume them here or wait for external workers."""
    task_ids = []
    for task in tasks:
        queue.enqueue(task)
        task_ids.append(task["task_id"])

    if external:
        return _wait_for_results(results_dir, task_ids, timeout_s)

    deadline = time.time() + max(1.0, timeout_s)
    stop = threading.Event()
    counters = {"done": 0}
    attempts: dict[str, int] = {}
    lock = threading.Lock()

    def consume() -> None:
        while not stop.is_set() and time.time() < deadline:
            msg = queue.dequeue(timeout_s=0.3)
            if msg is None:
                with lock:
                    if counters["done"] >= len(task_ids):
                        return
                continue
            result = run_task(msg["body"])
            _record(result, results_dir)
            with lock:
                counters["done"] += 1
                attempts[result["task_id"]] = attempts.get(result["task_id"], 0) + 1
                retries_left = attempts[result["task_id"]] < 2
            if result["status"] == "completed" or not retries_left:
                queue.ack(msg)
            else:
                queue.nack(msg)

    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    for _ in range(max(1, workers)):
        pool.submit(consume)
    results = _wait_for_results(results_dir, task_ids, max(0.1, deadline - time.time()))
    stop.set()
    pool.shutdown(wait=False)
    return results


def _wait_for_results(results_dir: str, task_ids: list[str], timeout_s: float) -> list[dict]:
    deadline = time.time() + max(0.0, timeout_s)
    while True:
        found = _read_results(results_dir, task_ids)
        if len(found) == len(task_ids):
            break
        if time.time() >= deadline:
            break
        time.sleep(0.1)
    ordered: list[dict[str, Any]] = []
    for task_id in task_ids:
        task_result = _read_results(results_dir, [task_id]).get(task_id)
        entry: dict[str, Any] = dict(task_result) if task_result else {}
        ordered.append(
            entry
            or {
                "task_id": task_id,
                "status": "failed",
                "error": "fan-out timeout: no result from worker",
            }
        )
    return ordered


# --------------------------------------------------------------------------- #
# Stitching
# --------------------------------------------------------------------------- #
def _concat_list(paths: list[str], list_path: str) -> None:
    with open(list_path, "w", encoding="utf-8") as fh:
        for path in paths:
            escaped = os.path.abspath(path).replace("'", "'\\''")
            fh.write(f"file '{escaped}'\n")


def stitch_shots(input_path: str, outputs: list[str], out_path: str, stage_dir: str) -> str:
    """Concat shot videos, then mux the untouched continuous audio once."""
    list_path = os.path.join(stage_dir, "concat.txt")
    joined = os.path.join(stage_dir, "joined_video.mp4")
    _concat_list(outputs, list_path)
    ffmpeg.run(
        [
            ffmpeg.ffmpeg_bin(),
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            list_path,
            "-c",
            "copy",
            joined,
        ]
    )

    info = ffmpeg.probe(input_path)
    if info.has_audio:
        ffmpeg.run(
            [
                ffmpeg.ffmpeg_bin(),
                "-y",
                "-i",
                joined,
                "-i",
                input_path,
                "-map",
                "0:v:0",
                "-map",
                "1:a:0?",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-shortest",
                "-movflags",
                "+faststart",
                out_path,
            ]
        )
    else:
        ffmpeg.run(
            [
                ffmpeg.ffmpeg_bin(),
                "-y",
                "-i",
                joined,
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                out_path,
            ]
        )
    return out_path


# --------------------------------------------------------------------------- #
# Entry point used by the orchestrator
# --------------------------------------------------------------------------- #
def fanout_stage(
    stage: str,
    input_path: str,
    workdir: str,
    scenes: list[dict[str, Any]],
    options: dict[str, Any],
) -> StageResult | None:
    """Run one stage per shot and return a stitched result.

    Returns ``None`` when fan-out does not apply (disabled, unknown stage, or
    fewer than two shots). Raises on execution failure so the caller decides
    whether ``auto`` degrades to the monolithic stage.
    """
    mode = str(options.get("fanout", "off")).lower()
    if mode in {"off", "false", "0", "none"}:
        return None
    stages = options.get("fanout_stages") or FANOUT_STAGES
    if stage not in stages:
        return None
    if not scenes or len(scenes) < 2:
        return None

    info = ffmpeg.probe(input_path)
    shots = shots_from_scenes(scenes, info.duration_s)
    if len(shots) < 2:
        return None

    stage_dir = os.path.join(workdir, f"fanout_{stage}")
    os.makedirs(stage_dir, exist_ok=True)
    started = time.time()
    shot_inputs = split_shots(input_path, shots, os.path.join(stage_dir, "shots"), options)
    tasks = build_tasks(stage, shot_inputs, stage_dir, options)

    workers = int(options.get("fanout_workers", min(4, len(tasks))))
    timeout_s = float(options.get("fanout_timeout_s", 3600))
    qmode = str(options.get("fanout_mode", "inline")).lower()
    results_dir = os.path.join(stage_dir, "results")
    os.makedirs(results_dir, exist_ok=True)
    results: list[dict]

    if qmode == "queue":
        url = options.get("queue_url") or queue_mod.default_queue_url(stage_dir)
        q = queue_mod.open_queue(url, lease_s=max(60.0, timeout_s))
        results = run_queued(
            tasks,
            q,
            results_dir,
            workers,
            timeout_s,
            external=bool(options.get("queue_external", False)),
        )
        backend = "sqs" if url.startswith("sqs://") else "file-queue"
    else:
        results = run_inline(tasks, workers)
        for result in results:
            _record(result, results_dir)
        backend = "inline"

    failed = [r for r in results if r.get("status") != "completed"]
    if failed:
        first = failed[0]
        raise RuntimeError(
            f"per-shot {stage} failed on {len(failed)}/{len(results)} shot(s): "
            f"{first.get('task_id')}: {first.get('error', 'unknown error')}"
        )

    stitched = os.path.join(stage_dir, f"{stage}_stitched.mp4")
    outputs = [r["output"] for r in sorted(results, key=lambda r: r["shot_index"])]
    stitch_shots(input_path, outputs, stitched, stage_dir)

    out_info = ffmpeg.probe(stitched)
    meta: dict[str, Any] = {
        "fanout": True,
        "fanout_backend": backend,
        "fanout_mode": qmode,
        "shots": len(results),
        "workers": workers,
        "shot_durations_s": [r.get("duration_s") for r in results],
        "cuts_s": [s["start_s"] for s in shots[1:]],
        "output_fps": round(out_info.fps, 3),
        "output_duration_s": round(out_info.duration_s, 3),
        "elapsed_s": round(time.time() - started, 3),
        "results_dir": results_dir,
        "first_shot_meta": results[0].get("meta", {}),
    }
    if not options.get("fanout_keep_shots"):
        shutil.rmtree(os.path.join(stage_dir, "shots"), ignore_errors=True)

    return StageResult(stage, stitched, meta)


def fanout_capabilities() -> dict[str, Any]:
    return {
        "stages": list(FANOUT_STAGES),
        "backends": ["inline", "file-queue", "sqs"],
        "external_worker": "python -m framefrog.worker --queue-url <url> --results-dir <dir>",
    }
