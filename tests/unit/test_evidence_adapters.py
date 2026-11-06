"""Behavior tests for the T7 evidence adapters (L14; specs S4, S6, S11).

Fake ports only — a fake ``GraphQueryPort`` and a fake ``GlobalQueryUseCase``
that record their calls. No Neo4j, no LLM, no network. Every test drives the
public ``GraphEvidencePort.query`` of one of the two adapters and asserts on
the returned ``GraphEvidence`` (or ``None``) only.
"""

from __future__ import annotations

from typing import Any

import pytest

from book_graph_rag.application.global_query_use_case import GlobalQueryUseCase
from book_graph_rag.domain.mcp_security import ScopeContext
from book_graph_rag.domain.models import (
    EntityType,
    EntityWithContext,
    GraphPath,
    Relationship,
    RelationshipType,
)
from book_graph_rag.domain.namespaces import SourceNamespace
from book_graph_rag.infrastructure.orchestration.chunk_evidence_adapter import (
    ChunkEvidenceAdapter,
)
from book_graph_rag.infrastructure.orchestration.community_answer_adapter import (
    CommunityAnswerAdapter,
)
from book_graph_rag.ports.graph_query_port import GraphQueryPort

_AAP = "knowledge:agentic-architectural-patterns"
_ESSENTIAL = "knowledge:essential-graphrag"
_QUESTION = "How does the ReAct pattern work?"


def _ns(namespace_id: str) -> SourceNamespace:
    corpus, source = namespace_id.split(":", maxsplit=1)
    return SourceNamespace(corpus=corpus, source=source)


def _chunk(book_id: str | None, chunk_id: str) -> dict[str, Any]:
    return {
        "chunk_id": chunk_id,
        "book_id": book_id,
        "chapter_id": f"{book_id}:chapter-1",
        "section_id": None,
        "page_start": 10,
        "page_end": 11,
        "text": "Retrieved prose the adapter must never paraphrase.",
        "score": 0.9,
    }


class _FakeGraphQuery(GraphQueryPort):
    """Fake ``GraphQueryPort``: records ``search_chunks``, rejects every other call."""

    def __init__(self, chunks: list[dict[str, Any]]) -> None:
        self._chunks = chunks
        self.calls: list[tuple[str, int, ScopeContext | None]] = []

    async def search_chunks(
        self, query: str, limit: int, *, scope: ScopeContext | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append((query, limit, scope))
        return self._chunks

    async def find_entity(
        self,
        name: str,
        entity_type: EntityType | None,
        *,
        scope: ScopeContext | None = None,
    ) -> list[EntityWithContext]:
        raise AssertionError("ChunkEvidenceAdapter must not call find_entity")

    async def find_entities_batch(self, ids: list[str]) -> list[EntityWithContext]:
        raise AssertionError("ChunkEvidenceAdapter must not call find_entities_batch")

    async def traverse_relationships(
        self,
        source_id: str,
        rel_type: RelationshipType | None,
        depth: int,
        *,
        scope: ScopeContext | None = None,
    ) -> tuple[list[EntityWithContext], list[Relationship]]:
        raise AssertionError("ChunkEvidenceAdapter must not traverse relationships")

    async def find_path(self, start_id: str, end_id: str, max_depth: int) -> list[GraphPath]:
        raise AssertionError("ChunkEvidenceAdapter must not call find_path")

    async def count_entities(
        self, entity_type: str | None, *, scope: ScopeContext | None = None
    ) -> int:
        raise AssertionError("ChunkEvidenceAdapter must not call count_entities")

    async def list_entities(
        self,
        cursor: int,
        page_size: int,
        *,
        scope: ScopeContext | None = None,
    ) -> tuple[list[EntityWithContext], int]:
        raise AssertionError("ChunkEvidenceAdapter must not call list_entities")

    async def ensure_indexes(self) -> None:
        raise AssertionError("ChunkEvidenceAdapter must not ensure indexes")


class _FakeGlobalQuery(GlobalQueryUseCase):
    """Fake ``GlobalQueryUseCase``: records ``ask``, returns a fixed payload."""

    def __init__(self, answer: str, citations: list[str]) -> None:
        # No super().__init__: ``ask`` is overridden, so the read/LLM ports are
        # never reached and must not be constructed by the test.
        self._answer = answer
        self._citations = citations
        self.calls: list[tuple[str, int, SourceNamespace | None]] = []

    async def ask(
        self,
        question: str,
        detail_level: int,
        *,
        scope: ScopeContext | None = None,
    ) -> dict[str, Any]:
        namespace = scope.source if scope is not None else None
        self.calls.append((question, detail_level, namespace))
        return {
            "answer": self._answer,
            "citations": list(self._citations),
            "contexts": [],
            "usage": {},
        }


# ── ChunkEvidenceAdapter (L14: reuses search_chunks, the search_rag port) ────


async def test_chunks_map_to_citations_in_rank_order_with_namespace_from_book_id() -> None:
    """L14: chunk_ids in rank order; namespace recovered from ``book_id``."""
    # Deliberately non-ascending ids: rank order must survive verbatim.
    first = _chunk(_AAP, f"{_AAP}:12")
    second = _chunk(_AAP, f"{_AAP}:3")
    port = _FakeGraphQuery([first, second])
    adapter = ChunkEvidenceAdapter(port, limit=7)

    evidence = await adapter.query(_QUESTION, _ns(_AAP))

    assert evidence is not None
    assert evidence.citations == (f"{_AAP}:12", f"{_AAP}:3")  # rank order, never re-sorted
    # The namespace comes from the chunk's own book_id (L14) and matches the scope.
    assert evidence.namespace == SourceNamespace.parse_book_id(str(first["book_id"]))
    assert evidence.namespace == _ns(_AAP)
    # The summary is a deterministic count line — a paraphrase would be inventing.
    assert evidence.summary == f"2 matched chunk(s) in {_AAP}"
    # One call, with the configured limit and the scope bound as a ScopeContext.
    assert len(port.calls) == 1
    query, limit, scope = port.calls[0]
    assert (query, limit) == (_QUESTION, 7)
    assert scope is not None
    assert scope.source == _ns(_AAP)


async def test_empty_chunk_results_map_to_none() -> None:
    """S6: no chunks → None, never a fabricated ``GraphEvidence``."""
    adapter = ChunkEvidenceAdapter(_FakeGraphQuery([]))

    assert await adapter.query(_QUESTION, _ns(_AAP)) is None


async def test_chunk_from_another_namespace_is_dropped() -> None:
    """S4/scope integrity: a foreign ``book_id`` is dropped, never re-labelled."""
    own = _chunk(_AAP, f"{_AAP}:4")
    foreign = _chunk(_ESSENTIAL, f"{_ESSENTIAL}:9")
    adapter = ChunkEvidenceAdapter(_FakeGraphQuery([own, foreign]))

    evidence = await adapter.query(_QUESTION, _ns(_AAP))

    assert evidence is not None
    assert evidence.citations == (f"{_AAP}:4",)  # foreign chunk dropped
    assert f"{_ESSENTIAL}:9" not in evidence.citations
    assert evidence.summary == f"1 matched chunk(s) in {_AAP}"  # count reflects the drop


async def test_only_foreign_chunks_map_to_none() -> None:
    """S6 + scope integrity: a result set that is all foreign is no evidence."""
    adapter = ChunkEvidenceAdapter(_FakeGraphQuery([_chunk(_ESSENTIAL, f"{_ESSENTIAL}:9")]))

    assert await adapter.query(_QUESTION, _ns(_AAP)) is None


async def test_chunk_without_verifiable_provenance_is_dropped() -> None:
    """S4: a chunk whose ``book_id`` cannot be parsed cannot be scope-verified."""
    unverifiable = {"chunk_id": "orphan", "book_id": None, "text": "...", "score": 0.1}
    malformed = _chunk("not-a-namespace", "malformed")
    adapter = ChunkEvidenceAdapter(_FakeGraphQuery([unverifiable, malformed]))

    assert await adapter.query(_QUESTION, _ns(_AAP)) is None


async def test_chunk_evidence_adapter_calls_the_port_once_per_query() -> None:
    """No blind retry (S7): exactly one ``search_chunks`` call per query."""
    port = _FakeGraphQuery([_chunk(_AAP, f"{_AAP}:1")])
    adapter = ChunkEvidenceAdapter(port)

    await adapter.query(_QUESTION, _ns(_AAP))
    assert len(port.calls) == 1

    await adapter.query("another question", _ns(_AAP))
    assert len(port.calls) == 2  # still exactly one call per query


# ── CommunityAnswerAdapter (L14: reuses GlobalQueryUseCase.ask) ──────────────


async def test_synthesis_maps_answer_and_citations_verbatim() -> None:
    """L14: ``answer`` → summary and community-summary ids → citations, verbatim."""
    use_case = _FakeGlobalQuery("Architectural answer.", ["comm-7", "comm-2"])
    adapter = CommunityAnswerAdapter(use_case)

    evidence = await adapter.query(_QUESTION, _ns(_AAP))

    assert evidence is not None
    assert evidence.summary == "Architectural answer."  # verbatim, no rewording
    assert evidence.citations == ("comm-7", "comm-2")  # verbatim order, not re-sorted
    assert evidence.namespace == _ns(_AAP)
    assert use_case.calls == [(_QUESTION, 1, _ns(_AAP))]  # default detail_level, scope bound


@pytest.mark.parametrize(
    ("citations", "answer"),
    [
        (["comm-1"], ""),
        (["comm-1"], "   "),
        ([], "Non-empty diagnostic answer with zero citations"),
        ([], ""),
    ],
)
async def test_blank_answer_or_empty_citations_map_to_none(
    citations: list[str], answer: str
) -> None:
    """S6/S11: no answer or no provenance → None; the adapter never invents."""
    adapter = CommunityAnswerAdapter(_FakeGlobalQuery(answer, citations))

    assert await adapter.query(_QUESTION, _ns(_AAP)) is None


async def test_detail_level_is_a_constructor_argument() -> None:
    """The adapter reads no config: the level comes from its constructor."""
    use_case = _FakeGlobalQuery("answer", ["comm-1"])
    adapter = CommunityAnswerAdapter(use_case, 2)

    await adapter.query(_QUESTION, _ns(_AAP))

    assert use_case.calls == [(_QUESTION, 2, _ns(_AAP))]
