"""Synthesis adapter: ``GlobalQueryUseCase.ask`` as ``GraphEvidence`` (T7).

Implements :class:`GraphEvidencePort` over the same use case the MCP
``ask_global`` tool uses (L14). ``citations`` are the community-summary ids
verbatim — that is exactly what ``GraphEvidence.citations`` carries.

The adapter never invents a summary: a blank answer or an answer without
citations (including ``ask``'s "no community summaries" diagnostics, which
carry zero citations) maps to ``None`` — S6's trigger — so the orchestrator
derives to text instead of reporting an unprovenanced synthesis (S11).
"""

from __future__ import annotations

from book_graph_rag.application.global_query_use_case import GlobalQueryUseCase
from book_graph_rag.application.orchestration.ports import GraphEvidencePort
from book_graph_rag.domain.mcp_security import ScopeContext
from book_graph_rag.domain.namespaces import SourceNamespace
from book_graph_rag.domain.orchestration_outcomes import GraphEvidence


class CommunityAnswerAdapter(GraphEvidencePort):
    """Map ``GlobalQueryUseCase.ask`` results into scoped ``GraphEvidence``.

    ``detail_level`` is a constructor argument (default 1, the use case's
    mid level); no config or ``Settings`` is read here — the caller that
    composes this adapter decides the level.
    """

    def __init__(self, use_case: GlobalQueryUseCase, detail_level: int = 1) -> None:
        self._use_case = use_case
        self._detail_level = detail_level

    async def query(self, question: str, scope: SourceNamespace) -> GraphEvidence | None:
        """Return the composed answer for ``question`` inside ``scope``, or ``None``.

        ``answer`` maps to ``summary`` and ``citations`` to ``citations``
        verbatim — no rewording, no renumbering, no reordering. A blank
        answer or an empty citation list maps to ``None`` (S6: never
        fabricate; S11: never report synthesis without provenance).

        Raises:
            Whatever ``GlobalQueryUseCase.ask`` raises — exceptions propagate.
        """
        payload = await self._use_case.ask(
            question,
            self._detail_level,
            scope=ScopeContext(source=scope),
        )
        answer = payload.get("answer")
        citations = payload.get("citations")
        if not isinstance(answer, str) or not answer.strip():
            return None
        if not isinstance(citations, list) or not citations:
            return None
        return GraphEvidence(
            namespace=scope,
            summary=answer,
            citations=tuple(citations),
        )
