"""Read-side port for community summaries."""

from __future__ import annotations

import abc

from book_graph_rag.domain.models import CommunitySummary, Entity, Relationship


class CommunityReadPort(abc.ABC):
    """Contract for reading the base graph and already-persisted summaries."""

    @abc.abstractmethod
    async def load_entity_graph(self) -> tuple[list[Entity], list[Relationship]]:
        """Return all :Entity nodes and :RELATED edges from the base graph."""
        ...

    @abc.abstractmethod
    async def get_summaries_by_level(self, level: int) -> list[CommunitySummary]:
        """Return all community summaries for the requested hierarchy level."""
        ...

    @abc.abstractmethod
    async def count_summaries(self) -> int:
        """Return the total number of persisted community summaries."""
        ...
