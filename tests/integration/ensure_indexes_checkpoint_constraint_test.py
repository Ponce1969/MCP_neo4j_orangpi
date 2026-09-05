"""Integration tests for command-side index/constraints."""

from __future__ import annotations

from typing import Any

import pytest

from book_graph_rag.config import Settings
from book_graph_rag.infrastructure.neo4j_command_adapter import Neo4jCommandAdapter


@pytest.mark.neo4j_integration
async def test_ensure_indexes_creates_checkpoint_unique_constraint(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """ensure_indexes creates an idempotent uniqueness constraint on :Checkpoint."""
    adapter = Neo4jCommandAdapter(neo4j_settings)
    try:
        await adapter.ensure_indexes()
        await adapter.ensure_indexes()  # idempotent second call

        async with neo4j_driver.session() as session:
            result = await session.run(
                """
                SHOW CONSTRAINTS YIELD name, type, labelsOrTypes, properties
                RETURN name, type, labelsOrTypes, properties
                """
            )
            constraints = [record.data() async for record in result]
    finally:
        await adapter.close()

    matches = [
        c
        for c in constraints
        if c["type"] == "UNIQUENESS"
        and c["labelsOrTypes"] == ["Checkpoint"]
        and c["properties"] == ["source_id", "chunk_index"]
    ]
    assert len(matches) == 1, f"expected one Checkpoint uniqueness constraint, got {constraints}"
