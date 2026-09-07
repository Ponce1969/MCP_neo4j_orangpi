"""Neo4j implementation of NeighborhoodQueryPort (Slice F1 wiring).

Read-only adapter that feeds S3 context scoring with the mention-source and
related-neighbor sets for a single entity.
"""

from __future__ import annotations

from typing import Any

from book_graph_rag.ports.neighborhood_query_port import NeighborhoodQueryPort


class Neo4jNeighborhoodQueryAdapter(NeighborhoodQueryPort):
    """Read-only neighborhood queries against a Neo4j driver."""

    def __init__(self, driver: Any) -> None:
        self._driver = driver

    async def mention_sources(self, entity_id: str) -> set[str]:
        """Return the distinct source ids of chunks that mention ``entity_id``.

        Falls back to ``book_id`` for legacy chunks that do not carry
        ``source_id``.
        """
        sources: set[str] = set()
        async with self._driver.session() as session:
            result = await session.run(
                """
                MATCH (c:Chunk)-[:MENTIONS]->(e:Entity {id: $entity_id})
                RETURN DISTINCT coalesce(c.source_id, c.book_id) AS source_id
                """,
                {"entity_id": entity_id},
            )
            async for record in result:
                source_id = record["source_id"]
                if source_id:
                    sources.add(source_id)
        return sources

    async def related_neighbors(self, entity_id: str) -> set[str]:
        """Return the distinct entity ids related to ``entity_id``."""
        neighbors: set[str] = set()
        async with self._driver.session() as session:
            result = await session.run(
                """
                MATCH (e:Entity {id: $entity_id})-[:RELATED]-(other:Entity)
                RETURN DISTINCT other.id AS other_id
                """,
                {"entity_id": entity_id},
            )
            async for record in result:
                other_id = record["other_id"]
                if other_id:
                    neighbors.add(other_id)
        return neighbors

    async def close(self) -> None:
        """Close the underlying Neo4j driver."""
        await self._driver.close()

