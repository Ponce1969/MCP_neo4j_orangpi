"""Integration tests for GraphDatabasePort.load_active_entities."""

from __future__ import annotations

import pytest

from book_graph_rag.config import Settings
from book_graph_rag.domain.models import Entity
from book_graph_rag.infrastructure.neo4j_command_adapter import Neo4jCommandAdapter


@pytest.mark.neo4j_integration
async def test_load_active_entities_returns_only_unmerged(
    neo4j_settings: Settings,
) -> None:
    """Active entities exclude any node with a non-empty merged_into property."""
    adapter = Neo4jCommandAdapter(neo4j_settings)
    try:
        await adapter.upsert_entities(
            [
                Entity(id="book:ch1:active-agent", name="Active", type="agent"),
                Entity(id="book:ch1:merged-agent", name="Merged", type="agent"),
                Entity(id="book:ch1:active-concept", name="Concept", type="concept"),
            ]
        )

        async with adapter._driver.session() as session:
            await session.run(
                """
                MATCH (n:Entity {id: $id})
                SET n.merged_into = $canonical, n.merged_at = datetime()
                """,
                id="book:ch1:merged-agent",
                canonical="book:ch1:active-agent",
            )

        active = await adapter.load_active_entities()
        active_ids = {e.id for e in active}
        assert active_ids == {
            "book:ch1:active-agent",
            "book:ch1:active-concept",
        }
    finally:
        await adapter.close()


@pytest.mark.neo4j_integration
async def test_load_active_entities_ignores_empty_merged_into(
    neo4j_settings: Settings,
) -> None:
    """An empty string merged_into is treated as active (defensive filter)."""
    adapter = Neo4jCommandAdapter(neo4j_settings)
    try:
        await adapter.upsert_entities(
            [
                Entity(id="book:ch1:empty-merge", name="Empty", type="agent"),
            ]
        )

        async with adapter._driver.session() as session:
            await session.run(
                "MATCH (n:Entity {id: $id}) SET n.merged_into = ''",
                id="book:ch1:empty-merge",
            )

        active = await adapter.load_active_entities()
        assert any(e.id == "book:ch1:empty-merge" for e in active)
    finally:
        await adapter.close()
