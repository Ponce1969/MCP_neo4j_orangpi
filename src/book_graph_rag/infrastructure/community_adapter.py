"""Neo4j implementation of the community-summary read and write ports.

This adapter owns its own async driver and is the only component that may
mutate ``:CommunitySummary`` nodes.  The base graph (:Entity, :RELATED) is
read-only for this adapter.
"""

from __future__ import annotations

from typing import Any

from neo4j import AsyncGraphDatabase

from book_graph_rag.config import Settings
from book_graph_rag.domain.models import (
    CommunitySummary,
    Entity,
    Relationship,
)
from book_graph_rag.ports.community_read_port import CommunityReadPort
from book_graph_rag.ports.community_write_port import CommunityWritePort


class Neo4jCommunityAdapter(CommunityReadPort, CommunityWritePort):
    """Async Neo4j implementation of ``CommunityReadPort`` + ``CommunityWritePort``."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # Deserialize the SecretStr once at construction time. The password is
        # passed to the driver and never logged or printed by this adapter.
        self._driver: Any = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
        )

    async def close(self) -> None:
        """Close the underlying Neo4j driver."""
        await self._driver.close()

    async def ensure_indexes(self) -> None:
        """Create the index backing ``:CommunitySummary`` checkpoint lookups.

        Idempotent (``IF NOT EXISTS``). Also registers the label in Neo4j's
        schema so checkpoint queries don't emit an unknown-label notification
        on the first run before any summary has been written.
        """
        async with self._driver.session() as session:
            result = await session.run(
                "CREATE INDEX community_summary_level IF NOT EXISTS "
                "FOR (c:CommunitySummary) ON (c.level)"
            )
            await result.consume()

    async def load_entity_graph(self) -> tuple[list[Entity], list[Relationship]]:
        """Read all :Entity nodes and :RELATED edges from the base graph."""
        async with self._driver.session() as session:
            entity_result = await session.run(
                """
                MATCH (e:Entity)
                RETURN e.id AS id, e.name AS name, e.type AS type,
                       e.description AS description, e.source_page AS source_page
                """,
                {},
            )
            entities: list[Entity] = []
            async for record in entity_result:
                entities.append(
                    Entity(
                        id=record["id"],
                        name=record["name"],
                        type=record["type"],
                        description=record["description"],
                        source_page=record["source_page"],
                    )
                )

            relationship_result = await session.run(
                """
                MATCH (src:Entity)-[r:RELATED]->(dst:Entity)
                RETURN r.type AS type, r.description AS description,
                       r.source_page AS source_page,
                       src.id AS source_entity_id, dst.id AS target_entity_id
                """,
                {},
            )
            relationships: list[Relationship] = []
            async for record in relationship_result:
                relationships.append(
                    Relationship(
                        source_entity_id=record["source_entity_id"],
                        target_entity_id=record["target_entity_id"],
                        type=record["type"],
                        description=record["description"],
                        source_page=record["source_page"],
                    )
                )

            return entities, relationships

    async def get_isolated_entities(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return ``:Entity`` nodes with no ``:RELATED`` edges (degree 0).

        Isolated entities are excluded by Leiden and only covered by the level-0
        global summary. Useful for spotting extraction noise vs. orphaned entities
        that should have been related during ingestion.
        """
        async with self._driver.session() as session:
            result = await session.run(
                "MATCH (e:Entity) WHERE NOT (e)-[:RELATED]-() "
                "RETURN e.name AS name, e.type AS type "
                "LIMIT $limit",
                {"limit": limit},
            )
            return [record.data() async for record in result]

    async def get_summaries_by_level(self, level: int) -> list[CommunitySummary]:
        """Return all :CommunitySummary nodes for the given level."""
        async with self._driver.session() as session:
            result = await session.run(
                """
                MATCH (c:CommunitySummary {level: $level})
                RETURN c.id AS id, c.level AS level, c.summary AS summary,
                       c.entity_ids AS entity_ids, c.parent_id AS parent_id
                """,
                {"level": level},
            )
            summaries: list[CommunitySummary] = []
            async for record in result:
                summaries.append(
                    CommunitySummary(
                        id=record["id"],
                        level=record["level"],
                        summary=record["summary"],
                        entity_ids=record["entity_ids"],
                        parent_id=record["parent_id"],
                    )
                )
            return summaries

    async def count_summaries(self) -> int:
        """Return the total number of :CommunitySummary nodes."""
        async with self._driver.session() as session:
            result = await session.run(
                "MATCH (c:CommunitySummary) RETURN count(c) AS count",
                {},
            )
            records = [record async for record in result]
            return records[0]["count"] if records else 0

    async def upsert_summaries(self, summaries: list[CommunitySummary]) -> None:
        """Persist summaries idempotently via MERGE keyed by the stable id."""
        async with self._driver.session() as session:
            await session.run(
                """
                UNWIND $summaries AS s
                MERGE (c:CommunitySummary {id: s.id})
                SET c.level = s.level,
                    c.summary = s.summary,
                    c.entity_ids = s.entity_ids,
                    c.parent_id = s.parent_id
                """,
                {"summaries": [summary.model_dump() for summary in summaries]},
            )

    async def upsert_summary(self, summary: CommunitySummary) -> None:
        """Persist a single summary idempotently (incremental checkpoint)."""
        async with self._driver.session() as session:
            await session.run(
                """
                MERGE (c:CommunitySummary {id: $id})
                SET c.level = $level,
                    c.summary = $summary,
                    c.entity_ids = $entity_ids,
                    c.parent_id = $parent_id
                """,
                {
                    "id": summary.id,
                    "level": summary.level,
                    "summary": summary.summary,
                    "entity_ids": summary.entity_ids,
                    "parent_id": summary.parent_id,
                },
            )

    async def clear_summaries(self) -> None:
        """Remove all :CommunitySummary nodes without touching the base graph."""
        async with self._driver.session() as session:
            await session.run(
                "MATCH (c:CommunitySummary) DETACH DELETE c",
                {},
            )
