"""Unit tests for the checkpoint state machine."""

from __future__ import annotations

import itertools

import pytest

from book_graph_rag.domain.checkpoint_models import CheckpointStatus
from book_graph_rag.domain.checkpoint_state_machine import (
    InvalidCheckpointTransition,
    transition,
)


@pytest.mark.parametrize(
    ("from_state", "to_state"),
    [
        (None, CheckpointStatus.PENDING),
        (CheckpointStatus.PENDING, CheckpointStatus.PROCESSING),
        (CheckpointStatus.PROCESSING, CheckpointStatus.PROCESSING),
        (CheckpointStatus.PROCESSING, CheckpointStatus.PROCESSED),
        (CheckpointStatus.PROCESSING, CheckpointStatus.FAILED),
        (CheckpointStatus.PENDING, CheckpointStatus.FAILED),
        (CheckpointStatus.FAILED, CheckpointStatus.PENDING),
        (CheckpointStatus.PENDING, CheckpointStatus.STALE),
        (CheckpointStatus.PROCESSING, CheckpointStatus.STALE),
        (CheckpointStatus.FAILED, CheckpointStatus.STALE),
        (CheckpointStatus.STALE, CheckpointStatus.PENDING),
    ],
)
def test_state_machine_valid_transitions(
    from_state: CheckpointStatus | None, to_state: CheckpointStatus
) -> None:
    """All allowed transitions must execute without raising."""
    transition(from_state, to_state, source_id="agentic-patterns:test", chunk_index=7)


_STATES = list(CheckpointStatus)
_ALLOWED_SET = {
    (None, CheckpointStatus.PENDING),
    (CheckpointStatus.PENDING, CheckpointStatus.PROCESSING),
    (CheckpointStatus.PROCESSING, CheckpointStatus.PROCESSING),
    (CheckpointStatus.PROCESSING, CheckpointStatus.PROCESSED),
    (CheckpointStatus.PROCESSING, CheckpointStatus.FAILED),
    (CheckpointStatus.PENDING, CheckpointStatus.FAILED),
    (CheckpointStatus.FAILED, CheckpointStatus.PENDING),
    (CheckpointStatus.PENDING, CheckpointStatus.STALE),
    (CheckpointStatus.PROCESSING, CheckpointStatus.STALE),
    (CheckpointStatus.FAILED, CheckpointStatus.STALE),
    (CheckpointStatus.STALE, CheckpointStatus.PENDING),
}
_DISALLOWED = [
    (f, t)
    for f, t in itertools.product([None, *_STATES], _STATES)
    if (f, t) not in _ALLOWED_SET
]


@pytest.mark.parametrize(("from_state", "to_state"), _DISALLOWED)
def test_state_machine_invalid_transitions(
    from_state: CheckpointStatus | None, to_state: CheckpointStatus
) -> None:
    """Disallowed transitions must raise with diagnostic context."""
    with pytest.raises(InvalidCheckpointTransition) as exc_info:
        transition(
            from_state,
            to_state,
            source_id="agentic-patterns:test",
            chunk_index=7,
        )

    err = exc_info.value
    assert err.from_state is from_state
    assert err.to_state == to_state
    assert err.source_id == "agentic-patterns:test"
    assert err.chunk_index == 7


def test_state_machine_processed_is_terminal() -> None:
    """PROCESSED is terminal unless an administrative force flag is used."""
    with pytest.raises(InvalidCheckpointTransition):
        transition(
            CheckpointStatus.PROCESSED,
            CheckpointStatus.PENDING,
            source_id="agentic-patterns:test",
            chunk_index=7,
        )

    transition(
        CheckpointStatus.PROCESSED,
        CheckpointStatus.PENDING,
        force=True,
        source_id="agentic-patterns:test",
        chunk_index=7,
    )
