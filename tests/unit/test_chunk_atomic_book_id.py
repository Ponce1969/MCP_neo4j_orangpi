"""Unit tests for the ``book_id`` contract on the namespaced chunk write path.

``commit_chunk_atomic`` keeps its durable identity on ``(source_id, chunk_index)``
but must also emit ``book_id`` on the chunk node so the audit and scoped query
adapters (which only read ``book_id``) can see namespaced chunks.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from book_graph_rag.config import Settings
from book_graph_rag.domain.checkpoint_models import VersionDimensions
from book_graph_rag.domain.models import Book, KnowledgeGraphChunk, PageRef
from book_graph_rag.infrastructure.neo4j_command_adapter import Neo4jCommandAdapter


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
            "source_id": "knowledge:agentic-architectural-patterns",
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
    """Async result whose ``single`` yields the configured records."""

    def __init__(self, records: list[dict[str, Any]] | None = None) -> None:
        self._records = records or []
        self._iter = iter(self._records)

    async def single(self) -> dict[str, Any] | None:
        try:
            return next(self._iter)
        except StopIteration:
            return None


class _FakeTransaction:
    """Records Cypher queries and returns a checkpoint record for the checkpoint query."""

    def __init__(self, calls: list[tuple[str, dict[str, Any] | None]]) -> None:
        self._calls = calls

    async def run(self, query: str, parameters: dict[str, Any] | None = None) -> _FakeResult:
        self._calls.append((query, parameters))
        if "MERGE (c:Checkpoint" in query:
            return _FakeResult([_checkpoint_record()])
        return _FakeResult()


class _FakeSession:
    """Session with ``execute_write`` support for the atomic transaction."""

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
    """Stand-in for ``neo4j.AsyncGraphDatabase``."""

    def __init__(self) -> None:
        self.driver_instance = _FakeDriver()

    def driver(self, *args: Any, **kwargs: Any) -> _FakeDriver:
        return self.driver_instance


async def test_commit_chunk_atomic_emits_book_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """The namespaced chunk MERGE keeps its identity and SETs ``book_id``."""
    fake_db = _FakeGraphDatabase()
    monkeypatch.setattr(
        "book_graph_rag.infrastructure.neo4j_command_adapter.AsyncGraphDatabase",
        fake_db,
    )
    adapter = Neo4jCommandAdapter(_make_settings())

    book = Book(
        id="knowledge:agentic-architectural-patterns",
        title="Agentic Architectural Patterns",
        author="",
        pdf_path="/tmp/book.pdf",
        page_count=100,
    )
    chunk = KnowledgeGraphChunk(
        text="a chunk",
        chunk_index=7,
        book=book,
        chapter=None,
        section=None,
        section_ancestors=(),
        page_ref=PageRef(start=1, end=2),
        entities=[],
        relationships=[],
    )
    versions = VersionDimensions(
        source_version="src",
        pipeline_version="pipe",
        model_version="model",
        schema_version="schema",
    )

    checkpoint = await adapter.commit_chunk_atomic(chunk, entity_ids=[], versions=versions)

    session = fake_db.driver_instance.session()
    chunk_query, chunk_params = session.calls[0]
    assert "MERGE (k:Chunk {source_id: $source_id, chunk_index: $chunk_index})" in chunk_query
    assert "k.book_id = $book_id" in chunk_query
    assert chunk_params is not None
    assert chunk_params["source_id"] == book.id
    assert chunk_params["book_id"] == book.id
    assert checkpoint.status.value == "PROCESSED"


async def test_commit_chunk_atomic_mentions_keeps_source_id_matcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Internal MERGEs keep matching by source_id and never null the new book_id."""
    fake_db = _FakeGraphDatabase()
    monkeypatch.setattr(
        "book_graph_rag.infrastructure.neo4j_command_adapter.AsyncGraphDatabase",
        fake_db,
    )
    adapter = Neo4jCommandAdapter(_make_settings())

    book = Book(
        id="knowledge:agentic-architectural-patterns",
        title="Agentic Architectural Patterns",
        author="",
        pdf_path="/tmp/book.pdf",
        page_count=100,
    )
    chunk = KnowledgeGraphChunk(
        text="a chunk",
        chunk_index=7,
        book=book,
        chapter=None,
        section=None,
        section_ancestors=(),
        page_ref=PageRef(start=1, end=2),
        entities=[],
        relationships=[],
    )
    versions = VersionDimensions(
        source_version="src",
        pipeline_version="pipe",
        model_version="model",
        schema_version="schema",
    )

    await adapter.commit_chunk_atomic(chunk, entity_ids=["e1"], versions=versions)

    session = fake_db.driver_instance.session()
    mentions_query, mentions_params = next(
        (query, params) for query, params in session.calls if "MERGE (k)-[m:MENTIONS]->(e)" in query
    )
    assert "MATCH (k:Chunk {source_id: $source_id, chunk_index: $chunk_index})" in mentions_query
    assert mentions_params is not None
    assert mentions_params["source_id"] == book.id

    assert all("k.book_id = null" not in query for query, _ in session.calls)
