"""Port for read-only graph retrieval used by layer-5 evaluation."""

from __future__ import annotations

import abc
from typing import Literal


class GraphRetrievalPort(abc.ABC):
    """Read-only retrieval over the graph for layer-5 evidence (R11.1)."""

    @abc.abstractmethod
    async def fetch_contexts(
        self,
        *,
        question: str,
        qtype: Literal["global", "local"],
        detail_level: int,
    ) -> tuple[str, ...]:
        """Return ordered contexts (community summaries for global; chunks+entities for local)."""

    @abc.abstractmethod
    async def compose_answer(
        self,
        *,
        question: str,
        contexts: tuple[str, ...],
    ) -> str:
        """Run the project's NL composition to produce the graph answer (R5.1)."""
