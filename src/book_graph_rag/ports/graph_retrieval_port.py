"""Port for read-only graph retrieval used by layer-5 evaluation."""

from __future__ import annotations

import abc
from typing import Literal

from book_graph_rag.domain.evaluation_models import RetrievalContext


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
