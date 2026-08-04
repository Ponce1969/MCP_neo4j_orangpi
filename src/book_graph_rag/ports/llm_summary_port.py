"""Port for LLM-driven community summary generation, scoring, and answer composition."""

from __future__ import annotations

import abc

from book_graph_rag.domain.models import CommunitySummary, Entity, Relationship


class LLMSummaryPort(abc.ABC):
    """Contract for generating natural-language summaries of a community.

    Also supports the global-query map-reduce flow: scoring summaries against a
    question and composing the final answer with citations.
    """

    @abc.abstractmethod
    async def generate_community_summary(
        self,
        entities: list[Entity],
        relationships: list[Relationship],
        level: int,
    ) -> str:
        """Return a 500–1000 token summary for a LEAF community.

        Used for the finest level (no child communities), where the input is the
        raw entities/relationships of the community.  ``level`` identifies the
        Leiden hierarchy level (0-3).
        """
        ...

    @abc.abstractmethod
    async def generate_summary_from_children(
        self, child_summaries: list[str], level: int
    ) -> str:
        """Return a summary for a PARENT community from its children's summaries.

        Implements the bottom-up map-reduce: a coarse community is summarized from
        the already-synthesized texts of its immediately-finer child communities,
        never from raw entities.  This keeps every LLM call within the context
        window.  ``child_summaries`` is the list of ``summary`` texts of the
        child CommunitySummary nodes (order is not significant).
        """
        ...

    @abc.abstractmethod
    async def score_community(self, question: str, summary: CommunitySummary) -> int:
        """Return a relevance score (0-100) for ``summary`` against ``question``."""
        ...

    @abc.abstractmethod
    async def compose_answer(
        self,
        question: str,
        ranked: list[tuple[CommunitySummary, int]],
    ) -> str:
        """Compose a final answer from ``ranked`` summaries using citations.

        Citations MUST follow the format ``[Data: CommunitySummary(id)]``.
        """
        ...
