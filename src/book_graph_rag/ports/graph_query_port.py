"""Read-side port for graph queries (CQRS query layer)."""

from __future__ import annotations

import abc
from typing import Any

from book_graph_rag.domain.models import (
    EntityType,
    EntityWithContext,
    GraphPath,
    Relationship,
    RelationshipType,
)


class GraphQueryPort(abc.ABC):
    """Contract for read-only graph queries.

    Implementations are expected to be MATCH-only adapters (e.g. Neo4j) with
    safety guards such as depth limits and query timeouts.
    """

    @abc.abstractmethod
    async def find_entity(
        self, name: str, entity_type: EntityType | None
    ) -> list[EntityWithContext]:
        """Return entities matching ``name`` and optional ``entity_type``."""
        ...

    @abc.abstractmethod
    async def find_entities_batch(self, ids: list[str]) -> list[EntityWithContext]:
        """Return entities for the given list of ids (max 200)."""
        ...

    @abc.abstractmethod
    async def traverse_relationships(
        self, source_id: str, rel_type: RelationshipType | None, depth: int
    ) -> tuple[list[EntityWithContext], list[Relationship]]:
        """Traverse outgoing relationships up to ``depth`` levels."""
        ...

    @abc.abstractmethod
    async def find_path(
        self, start_id: str, end_id: str, max_depth: int
    ) -> list[GraphPath]:
        """Return shortest paths between two entities within ``max_depth``."""
        ...

    @abc.abstractmethod
    async def search_chunks(self, query: str, limit: int) -> list[dict[str, Any]]:
        """Full-text search over chunk nodes."""
        ...

    @abc.abstractmethod
    async def count_entities(self, entity_type: str | None) -> int:
        """Return the number of entities, optionally filtered by type."""
        ...

    @abc.abstractmethod
    async def list_entities(
        self, cursor: int, page_size: int
    ) -> tuple[list[EntityWithContext], int]:
        """Cursor-based pagination over entities.

        Returns a tuple of (page_entities, next_cursor).
        """
        ...

    @abc.abstractmethod
    async def ensure_indexes(self) -> None:
        """Create read-side indexes idempotently."""
        ...
