"""Tests for Neo4jAdapter (AC-03.1, AC-03.5, AC-03.6)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from book_graph_rag.config import Settings
from book_graph_rag.domain.models import (
    Book,
    Chapter,
    Entity,
    KnowledgeGraphChunk,
    PageRef,
    Relationship,
    Section,
)
from book_graph_rag.infrastructure.neo4j_adapter import Neo4jAdapter


def _make_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> Settings:
    """Build Settings in a hermetic tmp directory without external env vars."""
    monkeypatch.chdir(tmp_path)
    for var in ("NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    data = {
        "neo4j_uri": "bolt://localhost:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "secret",
    }
    data.update(overrides)
    return Settings.model_validate(data)


class _FakeSession:
    """Records Cypher queries and parameters passed to ``session.run``."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def run(self, query: str, parameters: dict[str, Any] | None = None) -> None:
        self.calls.append((query, parameters))

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        pass


class _FakeDriver:
    def __init__(self) -> None:
        self._session = _FakeSession()

    def session(self) -> _FakeSession:
        return self._session

    async def close(self) -> None:
        pass


class _FakeGraphDatabase:
    """Stand-in for ``neo4j.AsyncGraphDatabase`` that records driver construction."""

    def __init__(self) -> None:
        self.driver_instance = _FakeDriver()
        self.driver_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def driver(self, *args: Any, **kwargs: Any) -> _FakeDriver:
        self.driver_calls.append((args, kwargs))
        return self.driver_instance


def test_neo4j_adapter_requires_settings() -> None:
    """AC-03.1: Neo4jAdapter requires Settings to construct."""
    with pytest.raises(TypeError):
        Neo4jAdapter()  # type: ignore[call-arg]


def test_neo4j_adapter_uses_secret_value_for_auth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-03.6: the Neo4j driver is constructed with the deserialized SecretStr."""
    settings = _make_settings(
        tmp_path,
        monkeypatch,
        neo4j_password="super-secret-password",
    )
    fake_db = _FakeGraphDatabase()
    monkeypatch.setattr(
        "book_graph_rag.infrastructure.neo4j_adapter.AsyncGraphDatabase",
        fake_db,
    )

    Neo4jAdapter(settings)

    assert len(fake_db.driver_calls) == 1
    args, kwargs = fake_db.driver_calls[0]
    assert args[0] == settings.neo4j_uri
    assert kwargs["auth"] == (settings.neo4j_user, "super-secret-password")


async def test_neo4j_adapter_upsert_entities_idempotent_on_repeats(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-03.5: calling upsert_entities twice emits identical MERGE Cypher."""
    settings = _make_settings(tmp_path, monkeypatch)
    fake_db = _FakeGraphDatabase()
    monkeypatch.setattr(
        "book_graph_rag.infrastructure.neo4j_adapter.AsyncGraphDatabase",
        fake_db,
    )
    adapter = Neo4jAdapter(settings)

    entity = Entity(id="e1", name="Agent", type="agent")
    await adapter.upsert_entities([entity])
    first_call = adapter._driver.session().calls[-1]

    await adapter.upsert_entities([entity])
    second_call = adapter._driver.session().calls[-1]

    assert first_call == second_call
    query, _ = first_call
    assert "MERGE (n:Entity {id: e.id})" in query


async def test_neo4j_adapter_upsert_book_merges_by_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-03.5: book persistence uses MERGE keyed by id."""
    settings = _make_settings(tmp_path, monkeypatch)
    fake_db = _FakeGraphDatabase()
    monkeypatch.setattr(
        "book_graph_rag.infrastructure.neo4j_adapter.AsyncGraphDatabase",
        fake_db,
    )
    adapter = Neo4jAdapter(settings)

    book = Book(
        id="agentic-patterns",
        title="Agentic Architectural Patterns",
        author="",
        pdf_path="/tmp/book.pdf",
        page_count=100,
    )
    await adapter.upsert_book(book)

    query, params = adapter._driver.session().calls[0]
    assert params is not None
    assert "MERGE (b:Book {id: $id})" in query
    assert params["id"] == book.id


async def test_neo4j_adapter_upsert_relationships_uses_merges_and_matches_entities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-03.5: relationships MATCH endpoints and MERGE the edge."""
    settings = _make_settings(tmp_path, monkeypatch)
    fake_db = _FakeGraphDatabase()
    monkeypatch.setattr(
        "book_graph_rag.infrastructure.neo4j_adapter.AsyncGraphDatabase",
        fake_db,
    )
    adapter = Neo4jAdapter(settings)

    relationship = Relationship(
        source_entity_id="e1",
        target_entity_id="e2",
        type="requires",
        description="e1 requires e2",
    )
    await adapter.upsert_relationships([relationship])

    query, params = adapter._driver.session().calls[0]
    assert params is not None
    assert "MATCH (src:Entity" in query
    assert "MERGE (src)-[rel:RELATED" in query
    assert params["rels"][0]["source_entity_id"] == "e1"
    assert params["rels"][0]["target_entity_id"] == "e2"


async def test_neo4j_adapter_upsert_editorial_structure_links_chunk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The editorial hierarchy is persisted and the chunk is linked."""
    settings = _make_settings(tmp_path, monkeypatch)
    fake_db = _FakeGraphDatabase()
    monkeypatch.setattr(
        "book_graph_rag.infrastructure.neo4j_adapter.AsyncGraphDatabase",
        fake_db,
    )
    adapter = Neo4jAdapter(settings)

    book = Book(
        id="agentic-patterns",
        title="Agentic Architectural Patterns",
        author="",
        pdf_path="/tmp/book.pdf",
        page_count=100,
    )
    chapter = Chapter(number=1, title="Introduction", page_start=1)
    section = Section(
        chapter_number=1,
        level=2,
        title="Why Multi-Agent Systems",
        page_start=1,
        parent_section_title=None,
    )
    chunk = KnowledgeGraphChunk(
        text="chunk text",
        chunk_index=0,
        book=book,
        chapter=chapter,
        section=section,
        page_ref=PageRef(start=1, end=2),
    )
    await adapter.upsert_editorial_structure(chapter, [section], chunk)

    queries = [call[0] for call in adapter._driver.session().calls]
    assert any("MERGE (b:Book {id: $book_id})" in q for q in queries)
    assert any("MERGE (ch:Chapter" in q for q in queries)
    assert any("MERGE (sec:Section" in q for q in queries)
    assert any("MERGE (k:Chunk" in q for q in queries)
    assert any("HAS_CHUNK" in q for q in queries)
