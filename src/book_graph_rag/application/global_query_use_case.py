"""Global query use case: map-reduce over community summaries.

Given a natural-language question and a Leiden hierarchy level, the use case
(1) fetches all community summaries at that level, (2) scores each summary for
relevance, (3) keeps the top-N, and (4) asks an LLM to compose a cited answer.
"""

from __future__ import annotations

import asyncio
from typing import Any

from book_graph_rag.domain.models import CommunitySummary
from book_graph_rag.ports.community_read_port import CommunityReadPort
from book_graph_rag.ports.llm_summary_port import LLMSummaryPort


class GlobalQueryUseCase:
    """Answer a global question using community-summary map-reduce."""

    def __init__(
        self,
        read_port: CommunityReadPort,
        llm_port: LLMSummaryPort,
        max_concurrency: int = 3,
        top_n: int = 8,
    ) -> None:
        self._read_port = read_port
        self._llm_port = llm_port
        self._max_concurrency = max_concurrency
        self._top_n = top_n

    async def ask(self, question: str, detail_level: int) -> dict[str, Any]:
        """Return a cited answer for ``question`` at ``detail_level``.

        Raises:
            ValueError: If ``detail_level`` is outside ``[0, 3]``.
        """
        if not 0 <= detail_level <= 3:
            raise ValueError(
                f"detail_level must be between 0 and 3, got {detail_level}"
            )

        summaries = await self._read_port.get_summaries_by_level(detail_level)
        if not summaries:
            return {
                "answer": "Run scripts/run_communities.py first",
                "citations": [],
            }

        semaphore = asyncio.Semaphore(self._max_concurrency)

        async def _score(summary: CommunitySummary) -> tuple[CommunitySummary, int]:
            async with semaphore:
                score = await self._llm_port.score_community(question, summary)
            return summary, score

        scored = await asyncio.gather(*(_score(summary) for summary in summaries))
        ranked = sorted(scored, key=lambda item: item[1], reverse=True)
        top = ranked[: self._top_n]

        answer = await self._llm_port.compose_answer(question, top)
        return {
            "answer": answer,
            "citations": [summary.id for summary, _ in top],
        }
