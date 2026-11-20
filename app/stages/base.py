# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared stage contract.

A stage is a function ``run(input_path, workdir, options) -> StageResult``.
It receives a path and a work directory, returns a new path (or the same one
for metadata-only stages) and a JSON-serialisable ``meta`` dictionary.

Stages never talk to the database, to the API or to the object store. They
are pure transformations on files, which is what makes them runnable as
standalone serverless functions later.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.core.errors import CapabilityError
from app.jobs.models import StageResult

#: Stage outcome vocabulary used in manifests.
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_PENDING_GPU_VALIDATION = "pending_gpu_validation"
STATUS_SKIPPED = "skipped"
STATUS_CANCELLED = "cancelled"

#: A stage implementation.
StageFn = Callable[[str, str, dict[str, Any]], StageResult]


@dataclass
class StageContext:
    """Everything a stage may need beyond its three arguments."""

    settings: Any = None
    job_id: str | None = None
    cancel_check: Callable[[], bool] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def cancelled(self) -> bool:
        """Whether the job was cancelled before this stage may continue."""
        if self.cancel_check is None:
            return False
        try:
            return bool(self.cancel_check())
        except Exception:  # pragma: no cover - defensive
            return False


def require_binary(path: str, name: str) -> str:
    """Return the binary path, or raise a capability error."""
    import shutil

    resolved = shutil.which(path) or (path if os.path.isfile(path) else None)
    if not resolved:
        raise CapabilityError(
            f"{name} is not available on this machine",
            details={"binary": path, "install": f"install {name} and make it available on PATH"},
        )
    return resolved


def mark_pending_gpu_validation(stage: str, reason: str, **meta: Any) -> StageResult:
    """Build a result that says "implemented, not yet validated on a GPU".

    The stage is never reported as completed in this case.
    """
    return StageResult(
        stage=stage,
        output_uri="",
        meta={
            "status": STATUS_PENDING_GPU_VALIDATION,
            "reason": reason,
            "gpu_validation_status": "pending",
            **meta,
        },
        status=STATUS_PENDING_GPU_VALIDATION,
        error=reason,
    )


__all__ = [
    "STATUS_CANCELLED",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "STATUS_PENDING_GPU_VALIDATION",
    "STATUS_SKIPPED",
    "StageContext",
    "StageFn",
    "StageResult",
    "mark_pending_gpu_validation",
    "require_binary",
]
