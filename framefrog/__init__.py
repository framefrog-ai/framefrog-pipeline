"""FrameFrog — AI cloud video post-production pipeline.

Public surface:

    from framefrog import JobRequest, run_job

    manifest = run_job(JobRequest(input_uri="rushes.mov"))
"""

from framefrog.pipeline import STAGE_NAMES, run_job
from framefrog.types import JobRequest, StageResult

__all__ = ["JobRequest", "StageResult", "run_job", "STAGE_NAMES"]
__version__ = "0.5.0"
