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
    EntityWithContext,
    EntityType,
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
        except asyncio.TimeoutError:
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
        raise NotImplementedError

    async def find_entities_batch(self, ids: list[str]) -> list[EntityWithContext]:
        """Return entities for the given list of ids (max 200)."""
        raise NotImplementedError

    async def traverse_relationships(
        self, source_id: str, rel_type: RelationshipType | None, depth: int
    ) -> tuple[list[EntityWithContext], list[Relationship]]:
        """Traverse outgoing relationships up to ``depth`` levels."""
        raise NotImplementedError

    async def find_path(
        self, start_id: str, end_id: str, max_depth: int
    ) -> list[GraphPath]:
        """Return shortest paths between two entities within ``max_depth``."""
        raise NotImplementedError

    async def search_chunks(self, query: str, limit: int) -> list[dict[str, Any]]:
        """Full-text search over chunk nodes."""
        raise NotImplementedError

    async def count_entities(self, entity_type: str | None) -> int:
        """Return the number of entities, optionally filtered by type."""
        raise NotImplementedError

    async def list_entities(
        self, cursor: int, page_size: int
    ) -> tuple[list[EntityWithContext], int]:
        """Cursor-based pagination over entities."""
        raise NotImplementedError

    async def ensure_indexes(self) -> None:
        """Create read-side indexes idempotently."""
        raise NotImplementedError
