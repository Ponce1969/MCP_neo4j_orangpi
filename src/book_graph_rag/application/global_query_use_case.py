"""Global query use case: map-reduce over community summaries.

Given a natural-language question and a Leiden hierarchy level, the use case
(1) fetches all community summaries at that level, (2) scores each summary for
relevance, (3) keeps the top-N, and (4) asks an LLM to compose a cited answer.
"""

from __future__ import annotations

import asyncio
from typing import Any

from book_graph_rag.domain.mcp_security import ScopeContext
from book_graph_rag.domain.models import CommunitySummary
from book_graph_rag.ports.community_read_port import CommunityReadPort
from book_graph_rag.ports.llm_summary_port import LLMSummaryPort


def _empty_usage(detail_level: int) -> dict[str, int]:
    """The usage of an answer that never reached the model.

    The shape is identical to a real answer on purpose: a consumer parses one shape,
    and an empty result must not be a special case it has to guess about.
    """
    return {
        "llm_calls": 0,
        "detail_level": detail_level,
        "summaries_considered": 0,
        "summaries_used": 0,
    }


class GlobalQueryUseCase:
    """Answer a global question using community-summary map-reduce."""

    def __init__(
        self,
        read_port: CommunityReadPort,
        llm_port: LLMSummaryPort,
        max_concurrency: int = 3,
        top_n: int = 8,
        score_batch_size: int = 20,
    ) -> None:
        self._read_port = read_port
        self._llm_port = llm_port
        self._max_concurrency = max_concurrency
        self._top_n = top_n
        self._score_batch_size = score_batch_size

    async def ask(
        self,
        question: str,
        detail_level: int,
        *,
        scope: ScopeContext | None = None,
    ) -> dict[str, Any]:
        """Return a cited answer for ``question`` at ``detail_level``.

        ``scope``, when provided, namespace-filters the community summaries read
        from the port (R7). The default ``None`` preserves the unscoped legacy
        behavior.

        Raises:
            ValueError: If ``detail_level`` is outside ``[0, 3]``.
        """
        if not 0 <= detail_level <= 3:
            raise ValueError(f"detail_level must be between 0 and 3, got {detail_level}")

        summaries = await self._read_port.get_summaries_by_level(detail_level, scope=scope)
        if not summaries:
            if scope is not None:
                # A scoped miss is per-namespace, so name it and point at the tool that builds one
                # namespace: the global wording sent the reader to a pipeline that would rebuild
                # every book, and hid which one was actually missing its communities.
                source_id = scope.source.source_id
                return {
                    "answer": (
                        f"No community summaries for {source_id} at detail level {detail_level}; "
                        f"build them with: uv run python "
                        f"scripts-ops/run_communities_scoped.py --run --namespace {source_id}"
                    ),
                    "citations": [],
                    "contexts": [],
                    "usage": _empty_usage(detail_level),
                }
            return {
                "answer": "Run scripts/run_communities.py first",
                "citations": [],
                "contexts": [],
                "usage": _empty_usage(detail_level),
            }

        semaphore = asyncio.Semaphore(self._max_concurrency)
        # One LLM call per batch instead of one per summary: at level 1 a single question used
        # to spend ~164 calls. The model also sees the candidates together, so near-ties get
        # better-calibrated scores.
        batches = [
            tuple(summaries[index : index + self._score_batch_size])
            for index in range(0, len(summaries), self._score_batch_size)
        ]

        async def _score_batch(
            batch: tuple[CommunitySummary, ...],
        ) -> list[tuple[CommunitySummary, int]]:
            async with semaphore:
                scores = await self._llm_port.score_communities(question, batch)
            return [(summary, scores[summary.id]) for summary in batch]

        scored = [
            item
            for group in await asyncio.gather(*(_score_batch(batch) for batch in batches))
            for item in group
        ]
        ranked = sorted(scored, key=lambda item: item[1], reverse=True)
        top = ranked[: self._top_n]

        answer = await self._llm_port.compose_answer(question, top)
        return {
            "answer": answer,
            "citations": [summary.id for summary, _ in top],
            # The summaries the composer actually saw, with the text that supports the
            # answer. A citation id proves the summary exists; only the text lets a
            # consumer audit whether it supports the sentence.
            "contexts": [
                {
                    "id": summary.id,
                    "level": summary.level,
                    "score": score,
                    "text": summary.summary,
                }
                for summary, score in top
            ],
            # What this answer cost, so a caller reasons about it instead of scraping
            # logs: one call per batch plus the single compose call.
            "usage": {
                "llm_calls": len(batches) + 1,
                "detail_level": detail_level,
                "summaries_considered": len(summaries),
                "summaries_used": len(top),
            },
        }
