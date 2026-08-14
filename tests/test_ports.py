"""Tests for ports (AC-02.3 and AC-02.4)."""

import inspect
import json
import typing
from collections.abc import Iterator
from pathlib import Path

import pytest

import book_graph_rag.ports as ports
from book_graph_rag.domain.models import (
    Book,
    Chapter,
    Entity,
    KnowledgeGraphChunk,
    Relationship,
    Section,
)
from book_graph_rag.infrastructure.dead_letter import JSONLDeadLetter
from book_graph_rag.ports.dead_letter_port import DeadLetterPort
from book_graph_rag.ports.graph_db_port import GraphDatabasePort
from book_graph_rag.ports.llm_port import LLMProviderPort
from book_graph_rag.ports.pdf_port import PDFReaderPort
from book_graph_rag.ports.query_logger_port import QueryLoggerPort


def test_llm_port_abstract_cannot_instantiate() -> None:
    with pytest.raises(TypeError):
        LLMProviderPort()


def test_graph_db_port_abstract_cannot_instantiate() -> None:
    with pytest.raises(TypeError):
        GraphDatabasePort()


def test_pdf_port_abstract_cannot_instantiate() -> None:
    with pytest.raises(TypeError):
        PDFReaderPort()


class _DummyLLM(LLMProviderPort):
    async def extract_graph(self, chunk: KnowledgeGraphChunk) -> KnowledgeGraphChunk:
        return chunk


class _DummyGraphDB(GraphDatabasePort):
    async def upsert_book(self, book: Book) -> None:
        return None

    async def upsert_entities(self, entities: list[Entity]) -> None:
        return None

    async def upsert_relationships(self, relationships: list[Relationship]) -> None:
        return None

    async def upsert_mentions(
        self, chunk_index: int, book_id: str | None, entity_ids: list[str]
    ) -> None:
        return None

    async def upsert_editorial_structure(
        self, chapter: Chapter, sections: list[Section], chunk: KnowledgeGraphChunk
    ) -> None:
        return None


class _DummyPDF(PDFReaderPort):
    def extract_chunks(self, file_path: str) -> Iterator[KnowledgeGraphChunk]:
        yield from []


def test_llm_port_can_be_implemented() -> None:
    provider = _DummyLLM()
    assert provider is not None


def test_graph_db_port_can_be_implemented() -> None:
    db = _DummyGraphDB()
    assert db is not None


class _DummyGraphDBMissingBook(GraphDatabasePort):
    async def upsert_entities(self, entities: list[Entity]) -> None:
        return None

    async def upsert_relationships(self, relationships: list[Relationship]) -> None:
        return None

    async def upsert_mentions(
        self, chunk_index: int, book_id: str | None, entity_ids: list[str]
    ) -> None:
        return None

    async def upsert_editorial_structure(
        self, chapter: Chapter, sections: list[Section], chunk: KnowledgeGraphChunk
    ) -> None:
        return None


def test_graph_db_port_missing_upsert_book_raises() -> None:
    with pytest.raises(TypeError):
        _DummyGraphDBMissingBook()


def test_graph_db_port_complete_subclass_can_be_instantiated() -> None:
    class CompleteGraphDB(GraphDatabasePort):
        async def upsert_book(self, book: Book) -> None:
            return None

        async def upsert_entities(self, entities: list[Entity]) -> None:
            return None

        async def upsert_relationships(self, relationships: list[Relationship]) -> None:
            return None

        async def upsert_mentions(
            self, chunk_index: int, book_id: str | None, entity_ids: list[str]
        ) -> None:
            return None

        async def upsert_editorial_structure(
            self, chapter: Chapter, sections: list[Section], chunk: KnowledgeGraphChunk
        ) -> None:
            return None

    db = CompleteGraphDB()
    assert db is not None


def test_pdf_port_can_be_implemented() -> None:
    reader = _DummyPDF()
    assert reader is not None


def test_llm_and_graph_methods_are_async() -> None:
    assert inspect.iscoroutinefunction(LLMProviderPort.extract_graph)
    assert inspect.iscoroutinefunction(GraphDatabasePort.upsert_book)
    assert inspect.iscoroutinefunction(GraphDatabasePort.upsert_entities)
    assert inspect.iscoroutinefunction(GraphDatabasePort.upsert_relationships)
    assert inspect.iscoroutinefunction(GraphDatabasePort.upsert_mentions)
    assert inspect.iscoroutinefunction(GraphDatabasePort.upsert_editorial_structure)


def test_pdf_method_is_sync_and_returns_iterator() -> None:
    signature = inspect.signature(PDFReaderPort.extract_chunks)
    evaluated = typing.get_type_hints(PDFReaderPort.extract_chunks)
    assert evaluated["return"] == Iterator[KnowledgeGraphChunk]
    assert signature.return_annotation == "Iterator[KnowledgeGraphChunk]"
    assert not inspect.iscoroutinefunction(PDFReaderPort.extract_chunks)


def test_query_logger_port_is_exported_from_package() -> None:
    """QueryLoggerPort is reachable from the ports package."""
    assert hasattr(ports, "QueryLoggerPort")
    assert ports.QueryLoggerPort is QueryLoggerPort


def test_dead_letter_port_abstract_cannot_instantiate() -> None:
    with pytest.raises(TypeError):
        DeadLetterPort()


class _DummyDeadLetter(DeadLetterPort):
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    async def write_orphan_relationship(self, record: dict[str, object]) -> None:
        self.records.append(record)


def test_dead_letter_port_can_be_implemented() -> None:
    writer = _DummyDeadLetter()
    assert writer is not None


async def test_jsonl_dead_letter_appends_structured_record(tmp_path: Path) -> None:
    """JSONLDeadLetter writes required orphan fields and a timestamp."""
    path = tmp_path / "orphans.jsonl"
    writer = JSONLDeadLetter(path)

    await writer.write_orphan_relationship(
        {
            "reason": "orphan_endpoint",
            "type": "requires",
            "source_entity_id": "e1",
            "target_entity_id": "e2",
            "description": "desc",
            "source_page": 1,
            "chunk_index": 2,
            "missing_endpoint": "target",
        }
    )

    assert path.exists()
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["reason"] == "orphan_endpoint"
    assert record["type"] == "requires"
    assert record["source_entity_id"] == "e1"
    assert record["target_entity_id"] == "e2"
    assert record["description"] == "desc"
    assert record["source_page"] == 1
    assert record["chunk_index"] == 2
    assert record["missing_endpoint"] == "target"
    assert "timestamp" in record
