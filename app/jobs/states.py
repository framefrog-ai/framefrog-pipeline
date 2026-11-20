# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Job states and the legal transitions between them.

The state machine is deliberately explicit: a job can only move along the
edges listed in :data:`ALLOWED_TRANSITIONS`. Anything else is a bug in the
runner and is reported as such instead of silently overwriting state.
"""

from __future__ import annotations

from enum import StrEnum


class JobState(StrEnum):
    """Persisted lifecycle of a job."""

    QUEUED = "queued"
    ACQUIRING_INPUT = "acquiring_input"
    VALIDATING = "validating"
    PROCESSING = "processing"
    UPLOADING = "uploading"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @classmethod
    def terminal_states(cls) -> frozenset[JobState]:
        return frozenset({cls.COMPLETED, cls.FAILED, cls.CANCELLED})

    @property
    def is_terminal(self) -> bool:
        return self in self.terminal_states()


#: Legal transitions. Terminal states have no outgoing edges.
ALLOWED_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.QUEUED: frozenset({JobState.ACQUIRING_INPUT, JobState.CANCELLED, JobState.FAILED}),
    JobState.ACQUIRING_INPUT: frozenset({JobState.VALIDATING, JobState.CANCELLED, JobState.FAILED}),
    JobState.VALIDATING: frozenset({JobState.PROCESSING, JobState.CANCELLED, JobState.FAILED}),
    JobState.PROCESSING: frozenset(
        {JobState.UPLOADING, JobState.COMPLETED, JobState.CANCELLED, JobState.FAILED}
    ),
    JobState.UPLOADING: frozenset({JobState.COMPLETED, JobState.CANCELLED, JobState.FAILED}),
    JobState.COMPLETED: frozenset(),
    JobState.FAILED: frozenset(),
    JobState.CANCELLED: frozenset(),
}


class InvalidTransitionError(ValueError):
    """Raised when a job would move between two states that are not connected."""


def can_transition(current: JobState, target: JobState) -> bool:
    """Whether ``current -> target`` is a legal move."""
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def validate_transition(current: JobState, target: JobState) -> None:
    """Raise :class:`InvalidTransitionError` for an illegal move."""
    if not can_transition(current, target):
        raise InvalidTransitionError(f"illegal job state transition: {current.value} -> {target.value}")


def state_for_stage(stage: str) -> JobState:
    """The state a job is in while a given stage runs."""
    if stage == "deliver":
        return JobState.UPLOADING
    return JobState.PROCESSING
