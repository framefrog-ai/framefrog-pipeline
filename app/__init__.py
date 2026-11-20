# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""FrameFrog — AI cloud video post-production pipeline.

Public surface:

    from app import JobRequest, run_job

    manifest = run_job(JobRequest(input_uri="rushes.mov"))

The HTTP surface lives in :mod:`app.main` (``uvicorn app.main:app``).
"""

from app.jobs.models import JobRecord, JobRequest, StageResult
from app.jobs.pipeline import run_job
from app.jobs.states import JobState

__all__ = [
    "JobRecord",
    "JobRequest",
    "JobState",
    "StageResult",
    "run_job",
]
__version__ = "0.8.0"
