"""Tests for Neo4jCommandAdapter (AC-03.1, AC-03.5, AC-03.6)."""

from __future__ import annotations

import json
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
from book_graph_rag.infrastructure.neo4j_command_adapter import Neo4jCommandAdapter


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


class _FakeRecord:
    """Single record returned by ``_FakeResult``."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)


class _FakeResult:
    """Async result that yields the configured records."""

    def __init__(self, records: list[_FakeRecord] | None = None) -> None:
        self._records = records or []
        self._iter = iter(self._records)

    async def single(self) -> _FakeRecord | None:
        try:
            return next(self._iter)
        except StopIteration:
            return None


class _FakeSession:
    """Records Cypher queries and parameters passed to ``session.run``."""

    def __init__(self, records: list[_FakeRecord] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self._records = records or []

    async def run(self, query: str, parameters: dict[str, Any] | None = None) -> _FakeResult:
        self.calls.append((query, parameters))
        return _FakeResult(list(self._records))

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
    """Stand-in for ``neo4j.AsyncGraphDatabase`` that records driver construction."""

    def __init__(self) -> None:
        self.driver_instance = _FakeDriver()
        self.driver_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def driver(self, *args: Any, **kwargs: Any) -> _FakeDriver:
        self.driver_calls.append((args, kwargs))
        return self.driver_instance


def test_neo4j_adapter_requires_settings() -> None:
    """AC-03.1: Neo4jCommandAdapter requires Settings to construct."""
    with pytest.raises(TypeError):
        Neo4jCommandAdapter()  # type: ignore[call-arg]


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
        "book_graph_rag.infrastructure.neo4j_command_adapter.AsyncGraphDatabase",
        fake_db,
    )

    Neo4jCommandAdapter(settings)

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
        "book_graph_rag.infrastructure.neo4j_command_adapter.AsyncGraphDatabase",
        fake_db,
    )
    adapter = Neo4jCommandAdapter(settings)

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
        "book_graph_rag.infrastructure.neo4j_command_adapter.AsyncGraphDatabase",
        fake_db,
    )
    adapter = Neo4jCommandAdapter(settings)

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
        "book_graph_rag.infrastructure.neo4j_command_adapter.AsyncGraphDatabase",
        fake_db,
    )
    adapter = Neo4jCommandAdapter(settings)

    relationship = Relationship(
        source_entity_id="e1",
        target_entity_id="e2",
        type="requires",
        description="e1 requires e2",
    )
    await adapter.upsert_relationships([relationship])

    calls = adapter._driver.session().calls
    assert len(calls) == 2
    endpoint_query, _ = calls[0]
    assert "OPTIONAL MATCH (n:Entity {id: id})" in endpoint_query
    query, params = calls[1]
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
        "book_graph_rag.infrastructure.neo4j_command_adapter.AsyncGraphDatabase",
        fake_db,
    )
    adapter = Neo4jCommandAdapter(settings)

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


# ── :MENTIONS provenance (REQ-PROV, SCEN-PROV-01..04, AC-PROV-02) ─────────────


async def test_neo4j_adapter_upsert_mentions_uses_chunk_guard_and_merge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCEN-PROV-01/02: mention write matches chunk by guarded book_id and MERGEs."""
    settings = _make_settings(tmp_path, monkeypatch)
    fake_db = _FakeGraphDatabase()
    monkeypatch.setattr(
        "book_graph_rag.infrastructure.neo4j_command_adapter.AsyncGraphDatabase",
        fake_db,
    )
    adapter = Neo4jCommandAdapter(settings)

    await adapter.upsert_mentions(chunk_index=4, book_id="book-1", entity_ids=["e1", "e2"])

    query, params = adapter._driver.session().calls[-1]
    assert params is not None
    assert "MATCH (c:Chunk {chunk_index: $chunk_index})" in query
    assert "($book_id IS NULL AND c.book_id IS NULL) OR c.book_id = $book_id" in query
    assert "MERGE (c)-[m:MENTIONS]->(e)" in query
    assert "coalesce(m.source_page, e.source_page)" in query
    assert params["chunk_index"] == 4
    assert params["book_id"] == "book-1"
    assert params["entity_ids"] == ["e1", "e2"]


async def test_neo4j_adapter_upsert_mentions_with_null_book_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCEN-PROV-03: TOC-less PDFs pass book_id=None and the guard keeps the match."""
    settings = _make_settings(tmp_path, monkeypatch)
    fake_db = _FakeGraphDatabase()
    monkeypatch.setattr(
        "book_graph_rag.infrastructure.neo4j_command_adapter.AsyncGraphDatabase",
        fake_db,
    )
    adapter = Neo4jCommandAdapter(settings)

    await adapter.upsert_mentions(chunk_index=7, book_id=None, entity_ids=["e3"])

    query, params = adapter._driver.session().calls[-1]
    assert params is not None
    assert "($book_id IS NULL AND c.book_id IS NULL) OR c.book_id = $book_id" in query
    assert params["book_id"] is None
    assert params["chunk_index"] == 7


async def test_neo4j_adapter_upsert_mentions_idempotent_on_repeats(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-PROV-02: re-flushing the same chunk produces identical Cypher/parameters."""
    settings = _make_settings(tmp_path, monkeypatch)
    fake_db = _FakeGraphDatabase()
    monkeypatch.setattr(
        "book_graph_rag.infrastructure.neo4j_command_adapter.AsyncGraphDatabase",
        fake_db,
    )
    adapter = Neo4jCommandAdapter(settings)

    await adapter.upsert_mentions(chunk_index=2, book_id="book-1", entity_ids=["e1"])
    first_call = adapter._driver.session().calls[-1]

    await adapter.upsert_mentions(chunk_index=2, book_id="book-1", entity_ids=["e1"])
    second_call = adapter._driver.session().calls[-1]

    assert first_call == second_call
    query, _ = first_call
    assert "MERGE (c)-[m:MENTIONS]->(e)" in query


# ── Endpoint detection + dead-letter (REQ-REL, SCEN-REL-01..05, AC-REL-01..03) ─


def _relationships_with_missing() -> tuple[Relationship, Relationship]:
    valid = Relationship(
        source_entity_id="e1",
        target_entity_id="e2",
        type="requires",
        description="valid",
        source_page=1,
        chunk_index=0,
    )
    orphan = Relationship(
        source_entity_id="e1",
        target_entity_id="missing",
        type="depends_on",
        description="orphan",
        source_page=2,
        chunk_index=1,
    )
    return valid, orphan


async def test_upsert_relationships_runs_batched_endpoint_query_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCEN-REL-03/AC-REL-01: endpoint detection runs as one batched query."""
    settings = _make_settings(tmp_path, monkeypatch)
    fake_db = _FakeGraphDatabase()
    monkeypatch.setattr(
        "book_graph_rag.infrastructure.neo4j_command_adapter.AsyncGraphDatabase",
        fake_db,
    )
    adapter = Neo4jCommandAdapter(settings)

    session = _FakeSession(
        records=[_FakeRecord({"requested": ["e1", "e2"], "found_ids": ["e1", "e2"]})]
    )
    adapter._driver = _FakeDriver(session)

    rel = Relationship(
        source_entity_id="e1",
        target_entity_id="e2",
        type="requires",
        description="valid",
        source_page=1,
        chunk_index=0,
    )
    await adapter.upsert_relationships([rel])

    assert len(session.calls) == 2
    endpoint_query, params = session.calls[0]
    assert "OPTIONAL MATCH (n:Entity {id: id})" in endpoint_query
    assert params is not None
    assert params["source_ids"] == ["e1"]
    assert params["target_ids"] == ["e2"]


async def test_upsert_relationships_fail_loud_raises_with_missing_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCEN-REL-02/AC-REL-02: fail_loud raises and aborts the batch."""
    settings = _make_settings(
        tmp_path, monkeypatch, relationship_orphan_policy="fail_loud"
    )
    fake_db = _FakeGraphDatabase()
    monkeypatch.setattr(
        "book_graph_rag.infrastructure.neo4j_command_adapter.AsyncGraphDatabase",
        fake_db,
    )
    adapter = Neo4jCommandAdapter(settings)

    session = _FakeSession(
        records=[_FakeRecord({"requested": ["e1", "missing"], "found_ids": ["e1"]})]
    )
    adapter._driver = _FakeDriver(session)

    valid, orphan = _relationships_with_missing()
    with pytest.raises(ValueError, match="missing endpoints"):
        await adapter.upsert_relationships([valid, orphan])

    queries = [call[0] for call in session.calls]
    assert not any("MERGE (src)-[rel:RELATED" in q for q in queries)


async def test_upsert_relationships_log_orphan_writes_jsonl_and_persists_valid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCEN-REL-01/AC-REL-01: log_orphan writes JSONL and persists valid subset."""
    settings = _make_settings(
        tmp_path,
        monkeypatch,
        relationship_orphan_policy="log_orphan",
        dead_letter_path_orphans=tmp_path / "orphans.jsonl",
    )
    fake_db = _FakeGraphDatabase()
    monkeypatch.setattr(
        "book_graph_rag.infrastructure.neo4j_command_adapter.AsyncGraphDatabase",
        fake_db,
    )
    adapter = Neo4jCommandAdapter(settings)

    session = _FakeSession(
        records=[_FakeRecord({"requested": ["e1", "e2", "missing"], "found_ids": ["e1", "e2"]})]
    )
    adapter._driver = _FakeDriver(session)

    valid, orphan = _relationships_with_missing()
    await adapter.upsert_relationships([valid, orphan])

    # Valid relationship is persisted.
    rel_query = session.calls[-1][0]
    assert "MERGE (src)-[rel:RELATED" in rel_query

    # Orphan is written with all required fields.
    orphan_path = settings.dead_letter_path_orphans
    assert orphan_path.exists()
    lines = orphan_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["reason"] == "orphan_endpoint"
    assert record["type"] == "depends_on"
    assert record["source_entity_id"] == "e1"
    assert record["target_entity_id"] == "missing"
    assert record["description"] == "orphan"
    assert record["source_page"] == 2
    assert record["chunk_index"] == 1
    assert record["missing_endpoint"] == "target"
    assert "timestamp" in record


async def test_upsert_relationships_log_orphan_invariant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCEN-REL-06/AC-REL-01: input == persisted + dead_lettered_orphans."""
    settings = _make_settings(
        tmp_path,
        monkeypatch,
        relationship_orphan_policy="log_orphan",
        dead_letter_path_orphans=tmp_path / "orphans.jsonl",
    )
    fake_db = _FakeGraphDatabase()
    monkeypatch.setattr(
        "book_graph_rag.infrastructure.neo4j_command_adapter.AsyncGraphDatabase",
        fake_db,
    )
    adapter = Neo4jCommandAdapter(settings)

    session = _FakeSession(
        records=[_FakeRecord({"requested": ["e1", "e2", "missing"], "found_ids": ["e1", "e2"]})]
    )
    adapter._driver = _FakeDriver(session)

    valid, orphan = _relationships_with_missing()
    await adapter.upsert_relationships([valid, orphan])

    orphan_lines = (
        settings.dead_letter_path_orphans.read_text(encoding="utf-8").strip().splitlines()
    )
    assert len(orphan_lines) == 1

    rel_call = session.calls[-1]
    rel_params = rel_call[1] or {}
    persisted_count = len(rel_params.get("rels", []))
    assert persisted_count + len(orphan_lines) == 2
