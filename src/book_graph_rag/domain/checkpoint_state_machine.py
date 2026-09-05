"""Pure state-machine logic for checkpoint lifecycle transitions."""

from __future__ import annotations

from book_graph_rag.domain.checkpoint_models import CheckpointStatus


class InvalidCheckpointTransition(Exception):  # noqa: N818
    """Raised when a checkpoint state transition is not allowed."""

    def __init__(
        self,
        from_state: CheckpointStatus | None,
        to_state: CheckpointStatus,
        source_id: str,
        chunk_index: int,
    ) -> None:
        self.from_state = from_state
        self.to_state = to_state
        self.source_id = source_id
        self.chunk_index = chunk_index
        super().__init__(
            f"Invalid checkpoint transition {from_state!r} -> {to_state!r} "
            f"for {source_id}:{chunk_index}"
        )


_ALLOWED_TRANSITIONS: set[tuple[CheckpointStatus | None, CheckpointStatus]] = {
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


def transition(
    from_state: CheckpointStatus | None,
    to_state: CheckpointStatus,
    *,
    force: bool = False,
    source_id: str = "",
    chunk_index: int = 0,
) -> None:
    """Validate and apply a checkpoint state transition.

    ``force=True`` bypasses the transition table so that administrative
    commands (e.g. ``--force-reprocess``) can move a PROCESSED checkpoint.
    """
    if force:
        return
    if (from_state, to_state) not in _ALLOWED_TRANSITIONS:
        raise InvalidCheckpointTransition(from_state, to_state, source_id, chunk_index)
