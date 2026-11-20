# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Reclaim the disk used by finished jobs.

A job leaves a directory behind:

    WORK_DIR/jobs/<job_id>/work/   the media
    WORK_DIR/jobs/<job_id>/manifest.json

Those files are useful for a while and then they are just disk. The
retention window (``JOB_RETENTION_HOURS``) decides how long they are kept:
a finished job older than the window loses its directory *and* its row, so
the database does not grow without bound either.

A job that is still queued, running or waiting to be cancelled is never
touched — the cleanup only ever removes work that has already finished.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.core.logging import get_logger, log_event
from app.jobs.states import JobState

log = get_logger("jobs.retention")


def job_directory(settings: Settings, job_id: str) -> Path:
    """The directory a job owns."""
    return settings.work_dir_resolved / "jobs" / job_id


def cleanup_finished_jobs(
    settings: Settings,
    repository: Any,
    older_than_s: float | None = None,
) -> list[str]:
    """Remove finished jobs older than the retention window.

    ``older_than_s`` overrides the configured window (used by the tests and
    by the manual script). A window of ``0`` or a negative value means
    "keep everything" — the caller decides, this function never deletes by
    accident.
    """
    window_s = older_than_s
    if window_s is None:
        window_s = float(settings.job_retention_hours) * 3600.0
    if window_s <= 0:
        return []

    cutoff = time.time() - window_s
    removed: list[str] = []
    for record in repository.list(limit=1000):
        if not JobState(record.status).is_terminal:
            continue
        stamp = record.completed_at or record.created_at
        try:
            finished = _parse_timestamp(stamp)
        except ValueError:
            continue
        if finished is None or finished > cutoff:
            continue
        directory = job_directory(settings, record.job_id)
        shutil.rmtree(directory, ignore_errors=True)
        deleted = repository.delete(record.job_id)
        if deleted:
            removed.append(record.job_id)
    if removed:
        log_event(log, "jobs_cleaned", count=len(removed))
    return removed


def _parse_timestamp(value: str | None) -> float | None:
    """Parse an ISO-8601 timestamp into a POSIX timestamp."""
    if not value:
        return None
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.timestamp()


def disk_usage(settings: Settings) -> dict:
    """How much disk the job directory uses."""
    root = settings.work_dir_resolved / "jobs"
    total = 0
    count = 0
    if root.exists():
        for entry in os.scandir(root):
            if not entry.is_dir():
                continue
            count += 1
            for dirpath, _dirnames, filenames in os.walk(entry.path):
                for name in filenames:
                    try:
                        total += os.path.getsize(os.path.join(dirpath, name))
                    except OSError:
                        continue
    return {"jobs": count, "bytes": total, "root": str(root)}
