"""Ports feeding the fallback resolver: graph evidence and text passages (S6/S7).

Both contracts are protocols over domain models only, so infrastructure
adapters (Neo4j, BTRAG) implement them without the application layer knowing
their internals.

Neither port encodes a "sufficiency" metric: S6's trigger is literally
``None`` — *no entity found*. Sufficiency is not measurable today and
inventing a metric is out of scope for T2.
"""

from __future__ import annotations

from typing import Protocol

from book_graph_rag.domain.namespaces import SourceNamespace
from book_graph_rag.domain.orchestration_outcomes import GraphEvidence, Passage


class GraphEvidencePort(Protocol):
    """Scoped graph evidence query; ``None`` means no entity found (S6's trigger)."""

    async def query(self, question: str, scope: SourceNamespace) -> GraphEvidence | None:
        """Return evidence for ``question`` inside ``scope``, or ``None``.

        ``None`` is S6's trigger: the graph found no entity in this scope, so
        the orchestrator must report insufficient coverage and derive to text —
        never synthesize an answer from the question.

        Implementations raise on infrastructure failure; callers let the
        exception propagate and never convert it into an outcome.
        """
        ...


class TextPassagePort(Protocol):
    """Text-store passages used when the graph does not cover the question (S7)."""

    async def passages(self, question: str, *, limit: int) -> tuple[Passage, ...]:
        """Return at most ``limit`` passages for ``question``, best-first.

        Implementations raise on infrastructure failure; callers let the
        exception propagate.
        """
        ...
