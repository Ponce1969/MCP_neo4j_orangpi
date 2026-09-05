"""Integration tests for the Neo4j checkpoint adapter."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from book_graph_rag.config import Settings
from book_graph_rag.domain.checkpoint_models import (
    CheckpointStatus,
    VersionDimensions,
)
from book_graph_rag.infrastructure.neo4j_checkpoint_adapter import (
    Neo4jCheckpointAdapter,
)


def _versions(source_version: str = "src-v1") -> VersionDimensions:
    return VersionDimensions(
        source_version=source_version,
        pipeline_version="1.0.0",
        model_version="openai:gpt-4o-mini:2026-09-01",
        schema_version="1.0.0",
    )


@pytest.mark.neo4j_integration
async def test_checkpoint_port_lifecycle(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """Acquire, fail, reclaim and mark-stale flow against a real Neo4j graph."""
    adapter = Neo4jCheckpointAdapter(neo4j_settings)
    source_id = "test:checkpoint-lifecycle"
    versions = _versions()

    try:
        # acquire_lease creates a new PROCESSING row.
        lease = await adapter.acquire_lease(source_id, 0, versions)
        assert lease.is_new_lease is True
        assert lease.checkpoint.status == CheckpointStatus.PROCESSING
        assert lease.checkpoint.chunk_index == 0
        assert lease.checkpoint.versions == versions

        # Second acquire for the same key is idempotent.
        lease2 = await adapter.acquire_lease(source_id, 0, versions)
        assert lease2.checkpoint.status == CheckpointStatus.PROCESSING

        # fetch_state returns the existing checkpoint.
        state = await adapter.fetch_state(source_id, [0, 1])
        assert 0 in state
        assert 1 not in state
        assert state[0].status == CheckpointStatus.PROCESSING

        # release_lease_to_failed moves it to FAILED and bumps attempt.
        failed = await adapter.release_lease_to_failed(
            source_id,
            0,
            error_type="LLMExtractionError",
            error_message="malformed JSON",
            attempt=1,
        )
        assert failed.status == CheckpointStatus.FAILED
        assert failed.attempt == 1
        assert failed.error_type == "LLMExtractionError"

        # Re-acquire a FAILED chunk so we can test reclaim.
        await adapter.acquire_lease(source_id, 1, versions)

        # reclaim_stale_leases resets old PROCESSING leases to PENDING.
        stale_threshold = datetime.now(UTC) - timedelta(seconds=10)
        reclaimed = await adapter.reclaim_stale_leases(
            source_id, stale_threshold, stale_seconds=5
        )
        assert reclaimed == 0  # lease was just created

        # Manually age the lease by creating a fresh one with an old timestamp is
        # not possible through the port, so we update it directly via the driver.
        async with neo4j_driver.session() as session:
            await session.run(
                "MATCH (c:Checkpoint {source_id: $source_id, chunk_index: 1}) "
                "SET c.leased_at = $old",
                {"source_id": source_id, "old": stale_threshold - timedelta(seconds=10)},
            )

        reclaimed = await adapter.reclaim_stale_leases(
            source_id, datetime.now(UTC), stale_seconds=5
        )
        assert reclaimed == 1
        pending = await adapter.fetch_state(source_id, [1])
        assert pending[1].status == CheckpointStatus.PENDING
        assert pending[1].leased_at is None

        # mark_stale_and_reset flips non-PROCESSED rows whose versions differ.
        new_versions = _versions(source_version="src-v2")
        changed = await adapter.mark_stale_and_reset(source_id, new_versions)
        assert changed >= 2
        state = await adapter.fetch_state(source_id, [0, 1])
        assert state[0].status == CheckpointStatus.STALE
        assert state[1].status == CheckpointStatus.STALE
    finally:
        await adapter.close()
