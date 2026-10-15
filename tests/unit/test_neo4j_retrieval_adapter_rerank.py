"""Unit tests for the reranker wiring inside ``Neo4jRetrievalAdapter`` (Phase 3).

Neo4j is never contacted: the read adapters and the reranker are injected fakes,
so these tests pin the orchestration contract only — over-fetch ``N``, rerank,
truncate to ``top_k``, entities appended after.
"""

from __future__ import annotations

from typing import Any, cast

from book_graph_rag.config import Settings
from book_graph_rag.domain.evaluation_models import RetrievalContext
from book_graph_rag.infrastructure.neo4j_retrieval_adapter import Neo4jRetrievalAdapter


class _FakeQueryAdapter:
    """Records the limits ``search_chunks`` was called with."""

    def __init__(self, chunks: list[dict[str, Any]], entities: list[Any] | None = None) -> None:
        self._chunks = chunks
        self._entities = entities or []
        self.search_limits: list[int] = []

    async def search_chunks(
        self, query: str, limit: int, *, scope: Any = None
    ) -> list[dict[str, Any]]:
        self.search_limits.append(limit)
        return self._chunks[:limit]

    async def find_entity(self, name: str, entity_type: Any, *, scope: Any = None) -> list[Any]:
        return self._entities

    async def close(self) -> None:
        return None


class _FakeCommunityAdapter:
    def __init__(self, summaries: list[Any] | None = None) -> None:
        self._summaries = summaries or []
        self.calls = 0

    async def get_summaries_by_level(self, level: int, *, scope: Any = None) -> list[Any]:
        self.calls += 1
        return self._summaries

    async def close(self) -> None:
        return None


class _FakeReranker:
    """Records the candidates it received; returns the LAST ``top_k``, reversed."""

    def __init__(self) -> None:
        self.received: tuple[RetrievalContext, ...] = ()
        self.top_k_seen: int | None = None

    async def rerank(
        self,
        *,
        query: str,
        candidates: tuple[RetrievalContext, ...],
        top_k: int,
    ) -> tuple[RetrievalContext, ...]:
        self.received = candidates
        self.top_k_seen = top_k
        return tuple(reversed(candidates))[:top_k]


class _FakeEntityValue:
    def __init__(self, name: str, description: str) -> None:
        self.id = name
        self.name = name
        self.description = description


class _FakeEntity:
    def __init__(self, name: str, description: str) -> None:
        self.entity = _FakeEntityValue(name, description)


class _FakeSummary:
    def __init__(self, summary: str) -> None:
        self.summary = summary


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "neo4j_uri": "bolt://localhost:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "not-a-real-password",
    }
    base.update(overrides)
    return Settings.model_validate(base)


def _chunks(count: int) -> list[dict[str, Any]]:
    return [{"chunk_id": f"a:book:{i}", "text": f"chunk {i}"} for i in range(count)]


def _adapter(
    query: _FakeQueryAdapter,
    *,
    community: _FakeCommunityAdapter | None = None,
    reranker: _FakeReranker | None = None,
    settings: Settings | None = None,
) -> Neo4jRetrievalAdapter:
    return Neo4jRetrievalAdapter(
        settings or _settings(),
        llm_adapter=cast(Any, object()),
        query_adapter=cast(Any, query),
        community_adapter=cast(Any, community or _FakeCommunityAdapter()),
        reranker=cast(Any, reranker) if reranker is not None else None,
    )


async def test_reranker_wiring_overfetches_then_truncates_to_top_k() -> None:
    """With a reranker the port is called on N candidates and only top_k survive."""
    query = _FakeQueryAdapter(_chunks(40))
    reranker = _FakeReranker()
    adapter = _adapter(query, reranker=reranker)

    contexts = await adapter.fetch_contexts(question="q", qtype="local", detail_level=1)

    assert query.search_limits == [40]
    assert len(reranker.received) == 40
    assert reranker.top_k_seen == 10
    assert [c.chunk_id for c in contexts] == [f"a:book:{i}" for i in range(39, 29, -1)]


async def test_without_reranker_keeps_the_legacy_top_k_fetch() -> None:
    """No reranker ⇒ fetch exactly top_k chunks, exactly as before this feature."""
    query = _FakeQueryAdapter(_chunks(40))
    adapter = _adapter(query)

    contexts = await adapter.fetch_contexts(question="q", qtype="local", detail_level=1)

    assert query.search_limits == [10]
    assert [c.chunk_id for c in contexts] == [f"a:book:{i}" for i in range(10)]


async def test_entities_are_appended_after_the_reranked_chunks() -> None:
    """Entities keep their place at the tail, with chunk_id=None."""
    query = _FakeQueryAdapter(
        _chunks(40),
        entities=[_FakeEntity("Entity A", "description A")],
    )
    adapter = _adapter(query, reranker=_FakeReranker())

    contexts = await adapter.fetch_contexts(question="q", qtype="local", detail_level=1)

    assert len(contexts) == 11
    assert contexts[-1].chunk_id is None
    assert contexts[-1].text == "Entity A: description A"


async def test_global_qtype_does_not_call_the_reranker() -> None:
    """Global retrieval is community summaries: the chunk reranker stays out."""
    query = _FakeQueryAdapter(_chunks(40))
    reranker = _FakeReranker()
    community = _FakeCommunityAdapter([_FakeSummary("summary one")])
    adapter = _adapter(query, community=community, reranker=reranker)

    contexts = await adapter.fetch_contexts(question="q", qtype="global", detail_level=1)

    assert [c.text for c in contexts] == ["summary one"]
    assert reranker.received == ()
    assert query.search_limits == []
