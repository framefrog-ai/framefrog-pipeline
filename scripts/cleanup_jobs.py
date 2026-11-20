#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Reclaim the disk used by finished jobs.

    python scripts/cleanup_jobs.py             # use JOB_RETENTION_HOURS
    python scripts/cleanup_jobs.py --older-than 3600
    python scripts/cleanup_jobs.py --dry-run

Only jobs that already reached a terminal state are touched. A running job is
never removed, and nothing is deleted when the retention window is zero.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="remove finished jobs past the retention window")
    parser.add_argument(
        "--older-than",
        type=float,
        default=None,
        help="age in seconds (default: JOB_RETENTION_HOURS)",
    )
    parser.add_argument("--dry-run", action="store_true", help="only report what would be removed")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    from app.core.config import Settings
    from app.jobs.repository import repository_from_settings
    from app.jobs.retention import cleanup_finished_jobs, disk_usage

    settings = Settings()
    repository = repository_from_settings(settings)
    try:
        if args.dry_run:
            from app.jobs.states import JobState

            window = (
                args.older_than
                if args.older_than is not None
                else float(settings.job_retention_hours) * 3600.0
            )
            import time

            cutoff = time.time() - window
            candidates = []
            for record in repository.list(limit=1000):
                if not JobState(record.status).is_terminal:
                    continue
                stamp = record.completed_at or record.created_at
                try:
                    from datetime import datetime

                    finished = datetime.fromisoformat(stamp).timestamp() if stamp else None
                except ValueError:
                    finished = None
                if finished is not None and finished <= cutoff:
                    candidates.append(record.job_id)
            removed: list[str] = []
        else:
            removed = cleanup_finished_jobs(settings, repository, older_than_s=args.older_than)
        usage = disk_usage(settings)
    finally:
        repository.close()

    if args.json:
        print(json.dumps({"removed": removed, "dry_run": args.dry_run, "usage": usage}, indent=2))
    else:
        verb = "would remove" if args.dry_run else "removed"
        print(f"{verb} {len(removed)} job(s)")
        for job_id in removed:
            print(f"  {job_id}")
        print(f"job directory: {usage['jobs']} job(s), {usage['bytes'] / 1e6:.1f} MB at {usage['root']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
