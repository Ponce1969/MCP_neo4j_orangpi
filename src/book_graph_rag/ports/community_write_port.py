"""Write-side port for community summaries."""

from __future__ import annotations

import abc

from book_graph_rag.domain.models import CommunitySummary


class CommunityWritePort(abc.ABC):
    """Contract for persisting community summaries produced offline."""

    @abc.abstractmethod
    async def upsert_summaries(self, summaries: list[CommunitySummary]) -> None:
        """Persist ``summaries`` idempotently, replacing any with the same id."""
        ...

    @abc.abstractmethod
    async def upsert_summary(self, summary: CommunitySummary) -> None:
        """Persist a single ``summary`` idempotently, keyed by its stable id.

        Used for incremental checkpointing: each community is written as soon as
        it is produced so a crash/rate-limit mid-run does not lose prior progress.
        """
        ...

    @abc.abstractmethod
    async def clear_summaries(self) -> None:
        """Remove all persisted community summaries without touching the base graph."""
        ...
