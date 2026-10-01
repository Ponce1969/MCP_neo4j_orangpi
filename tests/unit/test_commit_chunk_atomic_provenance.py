"""Unit tests for the provenance contract of ``commit_chunk_atomic``.

The resumable path (``book-graph-rag index``) and the dead-letter replay path
persist through ``commit_chunk_atomic``, whose provenance used to be whatever
the LLM reported — with no fallback and no protection against overwriting an
existing value with ``null``.  These tests lock the write-time invariant:

1. a payload without ``source_page``/``chunk_index`` is backfilled from the
   chunk it came from, and
2. an already-persisted provenance value is never nulled by a later chunk.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from book_graph_rag.config import Settings
from book_graph_rag.domain.checkpoint_models import VersionDimensions
from book_graph_rag.domain.models import (
    Book,
    Entity,
    KnowledgeGraphChunk,
    PageRef,
    Relationship,
)
from book_graph_rag.infrastructure.neo4j_command_adapter import Neo4jCommandAdapter

_V = VersionDimensions(
    source_version="src",
    pipeline_version="pipe",
    model_version="model",
    schema_version="schema",
)


def _make_settings() -> Settings:
    return Settings.model_validate(
        {
            "neo4j_uri": "bolt://localhost:7687",
            "neo4j_user": "neo4j",
            "neo4j_password": "secret",
            "graph_llm_base_url": "https://graph.example.test/v1",
            "graph_llm_model_name": "graph-model",
            "query_llm_base_url": "https://query.example.test/v1",
            "query_llm_model_name": "query-model",
        }
    )


def _checkpoint_record() -> dict[str, Any]:
    return {
        "c": {
            "source_id": "knowledge:ns:book",
            "chunk_index": 7,
            "status": "PROCESSED",
            "attempt": 1,
            "source_version": "src",
            "pipeline_version": "pipe",
            "model_version": "model",
            "schema_version": "schema",
            "leased_at": None,
            "processed_at": None,
            "failed_at": None,
            "error_type": None,
            "error_message": None,
            "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
        }
    }


class _FakeResult:
    def __init__(self, records: list[dict[str, Any]] | None = None) -> None:
        self._iter = iter(records or [])

    async def single(self) -> dict[str, Any] | None:
        try:
            return next(self._iter)
        except StopIteration:
            return None


class _FakeTransaction:
    def __init__(self, calls: list[tuple[str, dict[str, Any] | None]]) -> None:
        self._calls = calls

    async def run(self, query: str, parameters: dict[str, Any] | None = None) -> _FakeResult:
        self._calls.append((query, parameters))
        if "MERGE (c:Checkpoint" in query:
            return _FakeResult([_checkpoint_record()])
        return _FakeResult()


class _FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def execute_write(self, tx_func: Any) -> Any:
        return await tx_func(_FakeTransaction(self.calls))

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        pass


class _FakeDriver:
    def __init__(self, session: _FakeSession | None = None) -> None:
        self._session = session or _FakeSession()

    def session(self) -> _FakeSession:
        return self._session

    async def close(self) -> None:
        pass


class _FakeGraphDatabase:
    def __init__(self) -> None:
        self.driver_instance = _FakeDriver()

    def driver(self, *args: Any, **kwargs: Any) -> _FakeDriver:
        return self.driver_instance


async def _commit_with_missing_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, dict[str, Any] | None]]:
    fake_db = _FakeGraphDatabase()
    monkeypatch.setattr(
        "book_graph_rag.infrastructure.neo4j_command_adapter.AsyncGraphDatabase",
        fake_db,
    )
    adapter = Neo4jCommandAdapter(_make_settings())

    book = Book(id="knowledge:ns:book", title="B", author="", pdf_path="/tmp/b.pdf", page_count=100)
    chunk = KnowledgeGraphChunk(
        text="a chunk",
        chunk_index=7,
        book=book,
        chapter=None,
        section=None,
        section_ancestors=(),
        page_ref=PageRef(start=41, end=43),
        entities=[Entity(id="e-1", name="E1", type="concept")],
        relationships=[
            Relationship(source_entity_id="e-1", target_entity_id="e-1", type="depends_on")
        ],
    )

    await adapter.commit_chunk_atomic(chunk, entity_ids=["e-1"], versions=_V)
    return fake_db.driver_instance.session().calls


def _query(
    calls: list[tuple[str, dict[str, Any] | None]], marker: str
) -> tuple[str, dict[str, Any] | None]:
    return next((query, params) for query, params in calls if marker in query)


async def test_commit_chunk_atomic_backfills_entity_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-PROV-01: an entity without ``source_page`` gets the chunk's first page."""
    calls = await _commit_with_missing_provenance(monkeypatch)

    _query_string, params = _query(calls, "UNWIND $entities AS e")

    assert params is not None
    assert params["entities"][0]["source_page"] == 41


async def test_commit_chunk_atomic_backfills_relationship_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-PROV-02: relationships get both ``source_page`` and ``chunk_index``."""
    calls = await _commit_with_missing_provenance(monkeypatch)

    _query_string, params = _query(calls, "UNWIND $rels AS r")

    assert params is not None
    assert params["rels"][0]["source_page"] == 41
    assert params["rels"][0]["chunk_index"] == 7


async def test_commit_chunk_atomic_never_overwrites_provenance_with_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-PROV-03: persisted provenance survives a later chunk that omits it."""
    calls = await _commit_with_missing_provenance(monkeypatch)

    entity_query, _entity_params = _query(calls, "UNWIND $entities AS e")
    rel_query, _rel_params = _query(calls, "UNWIND $rels AS r")

    assert "coalesce(n.source_page, e.source_page)" in entity_query
    assert "coalesce(rel.source_page, r.source_page)" in rel_query
    assert "coalesce(rel.chunk_index, r.chunk_index)" in rel_query


async def test_commit_chunk_atomic_mentions_carry_chunk_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-PROV-04: the MENTIONS edge records the chunk index that produced it."""
    calls = await _commit_with_missing_provenance(monkeypatch)

    mentions_query, _params = _query(calls, "MERGE (k)-[m:MENTIONS]->(e)")

    assert "coalesce(m.source_page, e.source_page)" in mentions_query
    assert "coalesce(m.chunk_index, $chunk_index)" in mentions_query
