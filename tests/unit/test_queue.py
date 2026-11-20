# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""File-queue semantics: claim, ack, nack, lease recovery."""

import os

import pytest

from app.jobs.queue import FileQueue, QueueError, open_queue


def test_roundtrip(tmp_path):
    q = FileQueue(tmp_path)
    task_id = q.enqueue({"task_id": "t1", "stage": "color_grade"})
    assert task_id == "t1"

    msg = q.dequeue(timeout_s=0.2)
    assert msg is not None
    assert msg["id"] == "t1"
    assert msg["body"]["stage"] == "color_grade"
    assert q.depth() == {"pending": 0, "working": 1, "done": 0}

    q.ack(msg)
    assert q.depth() == {"pending": 0, "working": 0, "done": 1}


def test_nack_requeues(tmp_path):
    q = FileQueue(tmp_path)
    q.enqueue({"task_id": "t1"})
    msg = q.dequeue(timeout_s=0.2)
    q.nack(msg)
    assert q.depth() == {"pending": 1, "working": 0, "done": 0}
    again = q.dequeue(timeout_s=0.2)
    assert again["id"] == "t1"


def test_empty_queue_returns_none(tmp_path):
    assert FileQueue(tmp_path).dequeue(timeout_s=0.1) is None


def test_expired_lease_is_recovered(tmp_path):
    q = FileQueue(tmp_path, lease_s=0.01)
    q.enqueue({"task_id": "t1"})
    msg = q.dequeue(timeout_s=0.2)
    os.utime(msg["receipt"], (0, 0))  # simulate a worker that died long ago
    recovered = q.dequeue(timeout_s=0.2)
    assert recovered is not None
    assert recovered["id"] == "t1"


def test_open_queue_schemes(tmp_path):
    assert isinstance(open_queue("file://" + str(tmp_path)), FileQueue)
    assert isinstance(open_queue(str(tmp_path / "bare")), FileQueue)
    with pytest.raises(QueueError):
        open_queue("ftp://nope")
