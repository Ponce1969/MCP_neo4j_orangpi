"""Evidence adapter: scoped ``search_chunks`` results as ``GraphEvidence`` (T7).

Implements :class:`GraphEvidencePort` over the same
:class:`GraphQueryPort.search_chunks` the MCP ``search_rag`` tool uses (L14):
the graph port is reused, ``McpServerAdapter`` is not touched — that class is
the MCP surface with 16 construction parameters, and the whole point of T7 is
to sit above it.

Scope integrity: the adapter recovers every chunk's namespace with
``SourceNamespace.parse_book_id`` and drops any chunk whose namespace
disagrees with the requested scope (S4 posture — a foreign chunk is never
reported as evidence for this scope). A chunk whose ``book_id`` is missing or
malformed cannot be scope-verified either, so it is dropped for the same
reason.
"""

from __future__ import annotations

from book_graph_rag.application.orchestration.ports import GraphEvidencePort
from book_graph_rag.domain.mcp_security import ScopeContext
from book_graph_rag.domain.namespaces import SourceNamespace, UnknownNamespaceError
from book_graph_rag.domain.orchestration_outcomes import GraphEvidence
from book_graph_rag.ports.graph_query_port import GraphQueryPort


class ChunkEvidenceAdapter(GraphEvidencePort):
    """Map ``GraphQueryPort.search_chunks`` dicts into scoped ``GraphEvidence``.

    The constructor takes the port and a result limit — no ``Settings`, no
    configuration reading: this adapter is a pure mapping over a port the
    caller already owns.
    """

    def __init__(self, port: GraphQueryPort, limit: int = 10) -> None:
        self._port = port
        self._limit = limit

    async def query(self, question: str, scope: SourceNamespace) -> GraphEvidence | None:
        """Return chunk evidence for ``question`` inside ``scope``, or ``None``.

        ``None`` is S6's trigger: an empty result set (or a result set whose
        every chunk failed scope verification) is never turned into a
        fabricated ``GraphEvidence`` — the orchestrator derives to text
        instead.

        ``summary`` is a **deterministic count line** (how many chunks matched
        in which scope). It deliberately does not paraphrase, quote or
        summarise the chunk text: S6 says the orchestrator must never invent,
        and a paraphrase of retrieved prose would be exactly that. The prose
        itself travels in the citations' chunks, not in this summary.

        Citations keep the port's rank order (best-first) as ``chunk_id``
        strings — never renumbered, never re-sorted.

        Raises:
            Whatever ``search_chunks`` raises — exceptions propagate.
        """
        chunks = await self._port.search_chunks(
            question,
            self._limit,
            scope=ScopeContext(source=scope),
        )
        citations: list[str] = []
        for chunk in chunks:
            book_id = chunk.get("book_id")
            chunk_id = chunk.get("chunk_id")
            # Scope integrity (S4): only a chunk we can positively attribute to
            # the requested namespace counts as evidence for it. Unverifiable
            # (missing/malformed book_id or chunk_id) or foreign namespaces are
            # dropped rather than reported under this scope.
            if not isinstance(book_id, str) or not isinstance(chunk_id, str):
                continue
            try:
                chunk_namespace = SourceNamespace.parse_book_id(book_id)
            except UnknownNamespaceError:
                continue
            if chunk_namespace != scope:
                # A chunk from another namespace (or a stale/foreign book_id)
                # would be cross-scope evidence — drop it, never re-label it.
                continue
            citations.append(chunk_id)
        if not citations:
            return None
        return GraphEvidence(
            namespace=scope,
            summary=f"{len(citations)} matched chunk(s) in {scope.source_id}",
            citations=tuple(citations),
        )
