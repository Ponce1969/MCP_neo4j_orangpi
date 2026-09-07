"""NeighborhoodQueryPort — read-only port feeding S3 context scoring.

The concrete adapter (Neo4jNeighborhoodQueryAdapter) ships in slice E3 so it
can share the merge-transaction Cypher module. The port itself depends only on
stdlib + abc so it stays hexagonal.
"""

from __future__ import annotations

import abc


class NeighborhoodQueryPort(abc.ABC):
    """Return raw neighborhood sets for a single entity.

    Implementations MUST be read-only and MUST NOT mutate the graph.
    """

    @abc.abstractmethod
    async def mention_sources(self, entity_id: str) -> set[str]:
        """Return the set of source ids (``corpus:source``) that mention ``entity_id``."""

    @abc.abstractmethod
    async def related_neighbors(self, entity_id: str) -> set[str]:
        """Return the set of entity ids that share a ``:RELATED`` edge with ``entity_id``."""
