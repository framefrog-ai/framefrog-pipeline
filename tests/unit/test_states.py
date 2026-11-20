# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Job state machine."""

from __future__ import annotations

import pytest

from app.jobs.states import (
    ALLOWED_TRANSITIONS,
    InvalidTransitionError,
    JobState,
    can_transition,
    validate_transition,
)


def test_all_states_present():
    values = {state.value for state in JobState}
    assert values == {
        "queued",
        "acquiring_input",
        "validating",
        "processing",
        "uploading",
        "completed",
        "failed",
        "cancelled",
    }


def test_terminal_states_have_no_outgoing_edges():
    for state in JobState.terminal_states():
        assert ALLOWED_TRANSITIONS[state] == frozenset()
    assert JobState.COMPLETED.is_terminal
    assert JobState.FAILED.is_terminal
    assert JobState.CANCELLED.is_terminal
    assert not JobState.QUEUED.is_terminal


def test_happy_path_transitions():
    assert can_transition(JobState.QUEUED, JobState.ACQUIRING_INPUT)
    assert can_transition(JobState.ACQUIRING_INPUT, JobState.VALIDATING)
    assert can_transition(JobState.VALIDATING, JobState.PROCESSING)
    assert can_transition(JobState.PROCESSING, JobState.UPLOADING)
    assert can_transition(JobState.UPLOADING, JobState.COMPLETED)


def test_illegal_transition_rejected():
    with pytest.raises(InvalidTransitionError):
        validate_transition(JobState.QUEUED, JobState.COMPLETED)
    with pytest.raises(InvalidTransitionError):
        validate_transition(JobState.COMPLETED, JobState.PROCESSING)


def test_cancel_is_always_allowed_from_open_states():
    for state in (
        JobState.QUEUED,
        JobState.ACQUIRING_INPUT,
        JobState.VALIDATING,
        JobState.PROCESSING,
        JobState.UPLOADING,
    ):
        assert can_transition(state, JobState.CANCELLED)
