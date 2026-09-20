"""Read-side port for community summaries."""

from __future__ import annotations

import abc

from book_graph_rag.domain.mcp_security import ScopeContext
from book_graph_rag.domain.models import CommunitySummary, Entity, Relationship


class CommunityReadPort(abc.ABC):
    """Contract for reading the base graph and already-persisted summaries."""

    @abc.abstractmethod
    async def load_entity_graph(self) -> tuple[list[Entity], list[Relationship]]:
        """Return all :Entity nodes and :RELATED edges from the base graph."""
        ...

    @abc.abstractmethod
    async def get_summaries_by_level(
        self,
        level: int,
        *,
        scope: ScopeContext | None = None,
    ) -> list[CommunitySummary]:
        """Return community summaries for ``level``, optionally namespace-filtered.

        When ``scope`` is provided, summaries are filtered by the namespaced
        ``entity_ids`` prefix (Entity ids are namespaced per ``SCOPE_KEYS_BY_LABEL``).
        The unscoped path (default ``None``) returns all summaries for the level —
        the legacy contract used by ``infrastructure/neo4j_retrieval_adapter.py``.
        """
        ...

    @abc.abstractmethod
    async def count_summaries(self) -> int:
        """Return the total number of persisted community summaries."""
        ...
