# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Minimal queue abstraction for per-shot fan-out.

Two backends, one contract:

* **FileQueue** — a directory with ``pending/``, ``working/`` and ``done/``.
  Claiming is an atomic ``rename``, so several worker processes on one host can
  share it. This is the default and the backend the tests exercise.
* **SqsQueue** — SQS when boto3 and a queue URL are configured. Same payloads,
  same ack/nack semantics.

Messages are plain dicts; delivery is at-least-once, so workers must be
idempotent (a shot task writing the same file twice is harmless).

    queue = open_queue("file:///tmp/fanout")
    msg = queue.enqueue({"task_id": "t1", "stage": "color_grade", ...})
    got = queue.dequeue(timeout_s=1.0)
    queue.ack(got)          # or queue.nack(got) to retry
"""

from __future__ import annotations

import json
import os
import shutil
import time
import urllib.parse
from typing import Any


class QueueError(RuntimeError):
    """Raised when a queue backend cannot be used as configured."""


class FileQueue:
    """Directory-backed queue with atomic claim and lease recovery."""

    def __init__(self, root: str, lease_s: float = 600.0) -> None:
        self.root = os.path.abspath(root)
        self.lease_s = float(lease_s)
        for sub in ("pending", "working", "done"):
            os.makedirs(os.path.join(self.root, sub), exist_ok=True)

    # -- producer ---------------------------------------------------------- #
    def enqueue(self, message: dict[str, Any]) -> str:
        task_id = str(message.get("task_id") or f"task_{time.time_ns()}")
        message = {**message, "task_id": task_id}
        dest = os.path.join(self.root, "pending", f"{task_id}.json")
        tmp = dest + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(message, fh)
        os.replace(tmp, dest)
        return task_id

    # -- consumer ---------------------------------------------------------- #
    def dequeue(self, timeout_s: float = 0.0) -> dict[str, Any] | None:
        deadline = time.time() + max(0.0, timeout_s)
        while True:
            self._recover_expired()
            pending = sorted(os.listdir(os.path.join(self.root, "pending")))
            for name in pending:
                if not name.endswith(".json"):
                    continue
                src = os.path.join(self.root, "pending", name)
                dst = os.path.join(self.root, "working", name)
                try:
                    os.replace(src, dst)  # atomic claim
                except FileNotFoundError:
                    continue  # another worker won it
                with open(dst, encoding="utf-8") as fh:
                    body = json.load(fh)
                return {"id": body.get("task_id", name[:-5]), "body": body, "receipt": dst}
            if time.time() >= deadline:
                return None
            time.sleep(0.05)

    def ack(self, message: dict[str, Any]) -> None:
        receipt = message.get("receipt")
        if receipt and os.path.exists(receipt):
            os.replace(receipt, os.path.join(self.root, "done", os.path.basename(receipt)))

    def nack(self, message: dict[str, Any]) -> None:
        receipt = message.get("receipt")
        if receipt and os.path.exists(receipt):
            os.replace(receipt, os.path.join(self.root, "pending", os.path.basename(receipt)))

    def depth(self) -> dict[str, int]:
        return {
            sub: len([n for n in os.listdir(os.path.join(self.root, sub)) if n.endswith(".json")])
            for sub in ("pending", "working", "done")
        }

    def _recover_expired(self) -> None:
        """Return messages whose worker died to the pending queue."""
        now = time.time()
        working = os.path.join(self.root, "working")
        for name in os.listdir(working):
            if not name.endswith(".json"):
                continue
            path = os.path.join(working, name)
            try:
                age = now - os.path.getmtime(path)
            except OSError:
                continue
            if age > self.lease_s:
                try:
                    os.replace(path, os.path.join(self.root, "pending", name))
                except FileNotFoundError:
                    pass


class SqsQueue:
    """SQS backend. Requires boto3; the body is JSON-encoded."""

    def __init__(self, queue_url: str, visibility_timeout: int = 600) -> None:
        try:
            import boto3  # type: ignore
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise QueueError("sqs:// queues require boto3: pip install boto3") from exc
        self.url = queue_url
        self.visibility_timeout = int(visibility_timeout)
        self.client = boto3.client("sqs")

    def enqueue(self, message: dict[str, Any]) -> str:
        task_id = str(message.get("task_id") or f"task_{time.time_ns()}")
        body = {**message, "task_id": task_id}
        self.client.send_message(QueueUrl=self.url, MessageBody=json.dumps(body))
        return task_id

    def dequeue(self, timeout_s: float = 0.0) -> dict[str, Any] | None:  # pragma: no cover
        resp = self.client.receive_message(
            QueueUrl=self.url,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=max(0, min(20, int(timeout_s))),
            VisibilityTimeout=self.visibility_timeout,
        )
        msgs = resp.get("Messages") or []
        if not msgs:
            return None
        msg = msgs[0]
        return {"id": msg.get("MessageId"), "body": json.loads(msg["Body"]), "receipt": msg["ReceiptHandle"]}

    def ack(self, message: dict[str, Any]) -> None:  # pragma: no cover
        self.client.delete_message(QueueUrl=self.url, ReceiptHandle=message["receipt"])

    def nack(self, message: dict[str, Any]) -> None:  # pragma: no cover
        self.client.change_message_visibility(
            QueueUrl=self.url, ReceiptHandle=message["receipt"], VisibilityTimeout=0
        )

    def depth(self) -> dict[str, int]:  # pragma: no cover
        attrs = self.client.get_queue_attributes(
            QueueUrl=self.url,
            AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
        ).get("Attributes", {})
        return {
            "pending": int(attrs.get("ApproximateNumberOfMessages", 0)),
            "working": int(attrs.get("ApproximateNumberOfMessagesNotVisible", 0)),
            "done": 0,
        }


def open_queue(target: str, *, lease_s: float = 600.0) -> Any:
    """Build a queue from ``file://path``, ``sqs://url`` or a bare path."""
    if target.startswith("sqs://"):
        return SqsQueue(target[len("sqs://") :], visibility_timeout=int(lease_s))
    if target.startswith("file://"):
        return FileQueue(urllib.parse.urlparse(target).path, lease_s=lease_s)
    if "://" in target:
        raise QueueError(f"unsupported queue scheme: {target.split('://', 1)[0]}")
    return FileQueue(target, lease_s=lease_s)


def default_queue_url(workdir: str) -> str:
    return "file://" + os.path.join(workdir, "queue")


def clear_queue(path: str) -> None:  # pragma: no cover - maintenance helper
    shutil.rmtree(path, ignore_errors=True)
