"""Fan-out worker: drain a queue and execute shot tasks.

    python -m framefrog.worker --queue-url file:///tmp/fanout --results-dir /tmp/fanout/results
    python -m framefrog.worker --queue-url sqs://https://sqs.../queue --results-dir /mnt/shared/results

The orchestrator writes results to a directory the parent job can read; on
serverless, point both sides at shared storage (EFS, S3-mounted volume, or an
object-store sync). With ``--once`` the worker drains what is available and
exits, which is useful for cron-driven GPU boxes.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from framefrog import queue as queue_mod
from framefrog.fanout import _record, run_task

_STOP = threading.Event()


def _handle_signal(_signum, _frame) -> None:
    _STOP.set()


def _loop(queue, results_dir: str, once: bool, attempts: dict, lock: threading.Lock) -> None:
    while not _STOP.is_set():
        msg = queue.dequeue(timeout_s=1.0)
        if msg is None:
            if once:
                return
            continue
        result = run_task(msg["body"])
        _record(result, results_dir)
        with lock:
            attempts[result["task_id"]] = attempts.get(result["task_id"], 0) + 1
            retry = attempts[result["task_id"]] < 2
        if result["status"] == "completed" or not retry:
            queue.ack(msg)
        else:
            queue.nack(msg)
        state = result["status"]
        print(f"[worker] {result['task_id']} {state} {result.get('duration_s', '?')}s", flush=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="framefrog.worker", description="FrameFrog fan-out worker")
    ap.add_argument(
        "--queue-url",
        default=os.environ.get("FRAMEFROG_QUEUE_URL"),
        help="file://path, sqs://url (default: $FRAMEFROG_QUEUE_URL)",
    )
    ap.add_argument(
        "--results-dir",
        default=os.environ.get("FRAMEFROG_FANOUT_RESULTS"),
        help="shared directory the orchestrator reads (default: $FRAMEFROG_FANOUT_RESULTS)",
    )
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--once", action="store_true", help="drain available tasks, then exit")
    args = ap.parse_args(argv)

    if not args.queue_url or not args.results_dir:
        ap.error("--queue-url and --results-dir are required (or set the env vars)")

    q = queue_mod.open_queue(args.queue_url)
    os.makedirs(args.results_dir, exist_ok=True)
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    attempts: dict = {}
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [
            pool.submit(_loop, q, args.results_dir, args.once, attempts, lock)
            for _ in range(max(1, args.workers))
        ]
        for fut in futures:
            fut.result()
    print("[worker] stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
