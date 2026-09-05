"""Pure domain models for resumable indexing checkpoints."""

from __future__ import annotations

from enum import StrEnum


class CheckpointStatus(StrEnum):
    """Lifecycle states of a chunk checkpoint."""

    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"
    STALE = "STALE"
