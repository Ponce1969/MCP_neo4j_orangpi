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
        """Return a 500–1000 token summary for the given community.

        ``level`` identifies the Leiden hierarchy level (0-3) of the community.
        The implementation is free to ignore relationships/entities it cannot
        summarize but must not mutate the inputs.
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
