"""Port for checkpoint lifecycle operations."""

from __future__ import annotations

import abc
from datetime import datetime

from book_graph_rag.domain.checkpoint_models import Checkpoint, LeaseResult, VersionDimensions


class CheckpointPort(abc.ABC):
    """Contract for acquiring, releasing and inspecting chunk checkpoints."""

    @abc.abstractmethod
    async def acquire_lease(
        self, source_id: str, chunk_index: int, versions: VersionDimensions
    ) -> LeaseResult:
        """Move a chunk to PROCESSING and record the lease time.

        Idempotent: repeated calls for the same (source_id, chunk_index) with
        the same version dimensions return the same checkpoint.
        """
        ...

    @abc.abstractmethod
    async def release_lease_to_failed(
        self,
        source_id: str,
        chunk_index: int,
        error_type: str,
        error_message: str,
        attempt: int,
    ) -> Checkpoint:
        """Transition a PROCESSING chunk to FAILED and store error metadata."""
        ...

    @abc.abstractmethod
    async def mark_stale_and_reset(
        self, source_id: str, expected_versions: VersionDimensions
    ) -> int:
        """Mark non-PROCESSED checkpoints stale when versions mismatch.

        Returns the number of rows changed.
        """
        ...

    @abc.abstractmethod
    async def fetch_state(
        self, source_id: str, chunk_indices: list[int]
    ) -> dict[int, Checkpoint]:
        """Return existing checkpoints keyed by chunk_index.

        Missing indices are omitted; callers treat them as PENDING.
        """
        ...

    @abc.abstractmethod
    async def reclaim_stale_leases(
        self, source_id: str, now: datetime, stale_seconds: int
    ) -> int:
        """Reset PROCESSING leases older than ``stale_seconds``.

        Returns the number of leases reclaimed.
        """
        ...
