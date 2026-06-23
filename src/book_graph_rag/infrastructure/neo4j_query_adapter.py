"""GraphQueryPort implementation using the async Neo4j driver.

All queries are MATCH-only (read-side). Write operations live in
``Neo4jCommandAdapter``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from neo4j import AsyncGraphDatabase

from book_graph_rag.config import Settings
from book_graph_rag.domain.models import (
    Entity,
    EntityType,
    EntityWithContext,
    GraphPath,
    QueryTimeoutError,
    Relationship,
    RelationshipType,
)
from book_graph_rag.ports.graph_query_port import GraphQueryPort


class Neo4jQueryAdapter(GraphQueryPort):
    """Async Neo4j implementation of ``GraphQueryPort`` for read queries."""

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

    async def _run_with_timeout(self, coro: Any, timeout: float = 3.0) -> Any:
        """Wrap a coroutine with a hard timeout and map to a domain error."""
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except TimeoutError:
            raise QueryTimeoutError(f"Query exceeded {timeout}s timeout") from None

    def _node_to_entity(self, node: Any) -> EntityWithContext:
        """Map a Neo4j Node to an ``EntityWithContext``."""
        entity = Entity(
            id=node.get("id", ""),
            name=node.get("name", ""),
            type=node.get("type", ""),
            description=node.get("description", ""),
            source_page=node.get("source_page"),
        )
        return EntityWithContext(entity=entity)

    async def find_entity(
        self, name: str, entity_type: EntityType | None
    ) -> list[EntityWithContext]:
        """Return entities matching ``name`` and optional ``entity_type``."""
        async with self._driver.session() as session:
            result = await self._run_with_timeout(
                session.run(
                    """
                    MATCH (n:Entity {name: $name})
                    WHERE $type IS NULL OR n.type = $type
                    RETURN n
                    LIMIT $limit
                    """,
                    {"name": name, "type": entity_type, "limit": 100},
                )
            )
            return [self._node_to_entity(record["n"]) async for record in result]

    async def find_entities_batch(self, ids: list[str]) -> list[EntityWithContext]:
        """Return entities for the given list of ids (max 200)."""
        async with self._driver.session() as session:
            result = await self._run_with_timeout(
                session.run(
                    """
                    UNWIND $ids AS id
                    MATCH (n:Entity {id: id})
                    RETURN n
                    """,
                    {"ids": ids},
                )
            )
            return [self._node_to_entity(record["n"]) async for record in result]

    def _relationship_to_domain(self, rel: Any) -> Relationship:
        """Map a Neo4j Relationship to a domain ``Relationship``."""
        return Relationship(
            source_entity_id=rel.start_node["id"],
            target_entity_id=rel.end_node["id"],
            type=rel.type,
            description=rel.get("description", ""),
            source_page=rel.get("source_page"),
        )

    async def traverse_relationships(
        self, source_id: str, rel_type: RelationshipType | None, depth: int
    ) -> tuple[list[EntityWithContext], list[Relationship]]:
        """Traverse outgoing relationships up to ``depth`` levels."""
        clamped_depth = max(0, min(depth, 3))

        if clamped_depth == 0:
            async with self._driver.session() as session:
                result = await self._run_with_timeout(
                    session.run(
                        "MATCH (start:Entity {id: $source_id}) RETURN start",
                        {"source_id": source_id},
                    )
                )
                records = [record async for record in result]
                if not records:
                    return [], []
                return [self._node_to_entity(records[0]["start"])], []

        query = f"""
            MATCH p = (start:Entity {{id: $source_id}})-[:RELATED*1..{clamped_depth}]->(end:Entity)
            WHERE $rel_type IS NULL OR ALL(r IN relationships(p) WHERE r.type = $rel_type)
            RETURN start, end, relationships(p) AS rels
            LIMIT 100
        """
        async with self._driver.session() as session:
            result = await self._run_with_timeout(
                session.run(
                    query,
                    {"source_id": source_id, "rel_type": rel_type},
                )
            )
            entity_by_id: dict[str, EntityWithContext] = {}
            relationships: list[Relationship] = []
            seen_rel_keys: set[tuple[str, str, str]] = set()
            async for record in result:
                start_entity = self._node_to_entity(record["start"])
                entity_by_id[start_entity.entity.id] = start_entity
                end_entity = self._node_to_entity(record["end"])
                entity_by_id[end_entity.entity.id] = end_entity

                for rel in record["rels"]:
                    for node in (rel.start_node, rel.end_node):
                        entity = self._node_to_entity(node)
                        entity_by_id[entity.entity.id] = entity

                    domain_rel = self._relationship_to_domain(rel)
                    rel_key = (
                        domain_rel.source_entity_id,
                        domain_rel.target_entity_id,
                        domain_rel.type,
                    )
                    if rel_key not in seen_rel_keys:
                        seen_rel_keys.add(rel_key)
                        relationships.append(domain_rel)

            return list(entity_by_id.values()), relationships

    async def find_path(
        self, start_id: str, end_id: str, max_depth: int
    ) -> list[GraphPath]:
        """Return shortest paths between two entities within ``max_depth``."""
        clamped_depth = max(1, min(max_depth, 3))
        query = f"""
            MATCH p = shortestPath(
                (a:Entity {{id: $start_id}})
                -[:RELATED*..{clamped_depth}]->
                (b:Entity {{id: $end_id}})
            )
            RETURN p
        """
        async with self._driver.session() as session:
            result = await self._run_with_timeout(
                session.run(
                    query,
                    {"start_id": start_id, "end_id": end_id},
                )
            )
            record: Any | None = None
            async for row in result:
                record = row
                break
            if record is None:
                return []
            path = record["p"]
            nodes = [self._node_to_entity(node).entity for node in path.nodes]
            relationships = [
                self._relationship_to_domain(rel) for rel in path.relationships
            ]
            return [GraphPath(nodes=nodes, relationships=relationships)]

    async def search_chunks(self, query: str, limit: int) -> list[dict[str, Any]]:
        """Full-text search over chunk nodes."""
        async with self._driver.session() as session:
            result = await self._run_with_timeout(
                session.run(
                    """
                    CALL db.fulltext.queryNodes("chunk_text_index", $query)
                    YIELD node, score
                    RETURN node, score
                    ORDER BY score DESC
                    LIMIT $limit
                    """,
                    {"query": query, "limit": limit},
                )
            )
            return [
                {
                    "text": record["node"].get("text", ""),
                    "page_start": record["node"].get("page_start"),
                    "page_end": record["node"].get("page_end"),
                    "score": record["score"],
                }
                async for record in result
            ]

    async def count_entities(self, entity_type: str | None) -> int:
        """Return the number of entities, optionally filtered by type."""
        async with self._driver.session() as session:
            result = await self._run_with_timeout(
                session.run(
                    """
                    MATCH (n:Entity)
                    WHERE $type IS NULL OR n.type = $type
                    RETURN count(n) AS count
                    """,
                    {"type": entity_type},
                )
            )
            records = [record async for record in result]
            return records[0]["count"] if records else 0

    async def list_entities(
        self, cursor: int, page_size: int
    ) -> tuple[list[EntityWithContext], int]:
        """Cursor-based pagination over entities."""
        async with self._driver.session() as session:
            result = await self._run_with_timeout(
                session.run(
                    """
                    MATCH (n:Entity)
                    WHERE id(n) > $cursor
                    ORDER BY id(n)
                    LIMIT $page_size
                    RETURN n, id(n) AS internal_id
                    """,
                    {"cursor": cursor, "page_size": page_size},
                )
            )
            records = [record async for record in result]
            entities = [self._node_to_entity(record["n"]) for record in records]
            next_cursor = records[-1]["internal_id"] if records else cursor
            return entities, next_cursor

    async def ensure_indexes(self) -> None:
        """Create read-side indexes idempotently."""
        index_statements = [
            "CREATE INDEX entity_name IF NOT EXISTS FOR (n:Entity) ON (n.name)",
            "CREATE INDEX entity_type IF NOT EXISTS FOR (n:Entity) ON (n.type)",
            "CREATE INDEX entity_id IF NOT EXISTS FOR (n:Entity) ON (n.id)",
            "CREATE INDEX rel_type IF NOT EXISTS FOR ()-[r:RELATED]-() ON (r.type)",
            "CREATE FULLTEXT INDEX chunk_text_index IF NOT EXISTS FOR (n:Chunk) ON EACH [n.text]",
        ]
        async with self._driver.session() as session:
            for statement in index_statements:
                await self._run_with_timeout(session.run(statement))
