"""QuarantineReviewPort — read-only graph facts for the review sheet (T6).

The application layer must not import ``infrastructure``: every graph read
that feeds ``quarantine list``/``render`` crosses this port.
"""

from __future__ import annotations

import abc
from collections.abc import Sequence

from book_graph_rag.domain.quarantine_review_models import PairReviewFacts


class QuarantineReviewPort(abc.ABC):
    """Read-only facts for one entity pair's quarantine decision sheet."""

    @abc.abstractmethod
    async def read_pair_facts(self, anchor_id: str, candidate_id: str) -> PairReviewFacts:
        """Return both entities with mention/neighbour sets, the first
        mentioning chunk's text per entity, their shared neighbours and any
        prior ledger merge between the two ids.

        Raises ``LookupError`` when either entity id does not exist.
        """

    @abc.abstractmethod
    async def read_descriptions(self, entity_ids: Sequence[str]) -> dict[str, str]:
        """Return ``{entity_id: description}`` for the ids found in the graph."""

    @abc.abstractmethod
    async def close(self) -> None:
        """Release the underlying resources."""
