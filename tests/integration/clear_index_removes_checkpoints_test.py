"""Integration tests for clear_index destructive operation."""

from __future__ import annotations

from typing import Any

import pytest

from book_graph_rag.config import Settings
from book_graph_rag.infrastructure.neo4j_command_adapter import Neo4jCommandAdapter


@pytest.mark.neo4j_integration
async def test_clear_index_removes_checkpoint_nodes(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """clear_index removes all :Checkpoint nodes as part of the destructive op."""
    source_id = "test:clear-index-checkpoints"
    async with neo4j_driver.session() as session:
        await session.run(
            """
            UNWIND range(0, 2) AS idx
            CREATE (c:Checkpoint {
                source_id: $source_id,
                chunk_index: idx,
                status: 'PROCESSED',
                attempt: 1,
                source_version: 'src-v1',
                pipeline_version: '1.0.0',
                model_version: 'openai:gpt-4o-mini:2026-09-01',
                schema_version: '1.0.0',
                updated_at: datetime()
            })
            """,
            {"source_id": source_id},
        )

    adapter = Neo4jCommandAdapter(neo4j_settings)
    try:
        await adapter.clear_index()

        async with neo4j_driver.session() as session:
            result = await session.run(
                "MATCH (c:Checkpoint) RETURN count(c) AS remaining"
            )
            record = await result.single()
    finally:
        await adapter.close()

    assert record is not None
    assert record["remaining"] == 0
