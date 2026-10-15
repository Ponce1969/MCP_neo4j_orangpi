"""Port for read-only graph retrieval used by layer-5 evaluation."""

from __future__ import annotations

import abc
from typing import Literal

from book_graph_rag.domain.evaluation_models import GlobalRetrievalAnswer, RetrievalContext


class GraphRetrievalPort(abc.ABC):
    """Read-only retrieval over the graph for layer-5 evidence (R11.1)."""

    @abc.abstractmethod
    async def fetch_contexts(
        self,
        *,
        question: str,
        qtype: Literal["global", "local"],
        detail_level: int,
    ) -> tuple[RetrievalContext, ...]:
        """Return ordered contexts (community summaries for global; chunks+entities for local).

        Each ``RetrievalContext`` carries its real graph ``chunk_id`` when the
        hit is a chunk; summaries and entities use ``chunk_id=None``.
        """

    @abc.abstractmethod
    async def compose_answer(
        self,
        *,
        question: str,
        contexts: tuple[RetrievalContext, ...],
    ) -> str:
        """Run the project's NL composition to produce the graph answer (R5.1)."""

    @abc.abstractmethod
    async def answer_global(
        self,
        *,
        question: str,
        detail_level: int,
    ) -> GlobalRetrievalAnswer:
        """Return the production global answer **and** the summaries it was composed from.

        Global questions are selected and composed in one pass by the use case production
        runs, so the evaluation asks for both halves together: fetching every summary at the
        level and composing a parallel answer measured a path production never runs, and did
        it without the question. ``citation_ids`` stay aligned with ``contexts`` index by
        index, which is what lets a caller score citations against the evidence it was given.
        """
