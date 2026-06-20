"""Tests for IndexBookUseCase (AC-04.1 to AC-04.5)."""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import get_type_hints

from book_graph_rag.application.index_book_use_case import IndexBookUseCase
from book_graph_rag.domain.models import (
    Book,
    Chapter,
    Entity,
    KnowledgeGraphChunk,
    PageRef,
    Relationship,
    Section,
)
from book_graph_rag.ports.graph_db_port import GraphDatabasePort
from book_graph_rag.ports.llm_port import LLMProviderPort
from book_graph_rag.ports.pdf_port import PDFReaderPort


class _FakePDFPort(PDFReaderPort):
    """Fake PDF reader that materialises a pre-built chunk list."""

    def __init__(self, chunks: list[KnowledgeGraphChunk]) -> None:
        self._chunks = chunks

    def extract_chunks(self, file_path: str) -> Iterator[KnowledgeGraphChunk]:
        yield from self._chunks


class _FakeLLMPort(LLMProviderPort):
    """Fake LLM that tags chunks with mock entities and optionally fails."""

    def __init__(
        self,
        fail_indices: set[int] | None = None,
        delay: float = 0.05,
    ) -> None:
        self._fail_indices = fail_indices or set()
        self._delay = delay
        self._records: list[tuple[int, float, float]] = []

    @property
    def records(self) -> list[tuple[int, float, float]]:
        return self._records

    def max_concurrency_observed(self) -> int:
        if not self._records:
            return 0
        events: list[tuple[float, int]] = []
        for _idx, start, end in self._records:
            events.append((start, 1))
            events.append((end, -1))
        # End events with the same timestamp should be processed before start
        # events so that a finished task frees its slot before counting a new
        # one starting at the exact same instant.
        events.sort(key=lambda ev: (ev[0], ev[1]))
        current = 0
        max_concurrent = 0
        for _timestamp, delta in events:
            current += delta
            max_concurrent = max(max_concurrent, current)
        return max_concurrent

    async def extract_graph(self, chunk: KnowledgeGraphChunk) -> KnowledgeGraphChunk:
        start = time.monotonic()
        await asyncio.sleep(self._delay)
        if chunk.chunk_index in self._fail_indices:
            raise ValueError(f"Simulated LLM failure for chunk {chunk.chunk_index}")
        chunk.entities = [
            Entity(
                id=f"ent-{chunk.chunk_index}",
                name=f"Entity {chunk.chunk_index}",
                type="concept",
            )
        ]
        chunk.relationships = [
            Relationship(
                source_entity_id=f"ent-{chunk.chunk_index}",
                target_entity_id=f"ent-{chunk.chunk_index}",
                type="depends_on",
            )
        ]
        end = time.monotonic()
        self._records.append((chunk.chunk_index, start, end))
        return chunk


class _FakeGraphDBPort(GraphDatabasePort):
    """Fake graph DB that records every upsert call for inspection."""

    def __init__(self) -> None:
        self.books_upserted: list[Book] = []
        self.entity_batches_upserted: list[list[Entity]] = []
        self.relationship_batches_upserted: list[list[Relationship]] = []
        self.editorial_calls: list[tuple[Chapter | None, list[Section], KnowledgeGraphChunk]] = []

    async def upsert_book(self, book: Book) -> None:
        self.books_upserted.append(book)

    async def upsert_entities(self, entities: list[Entity]) -> None:
        self.entity_batches_upserted.append(list(entities))

    async def upsert_relationships(self, relationships: list[Relationship]) -> None:
        self.relationship_batches_upserted.append(list(relationships))

    async def upsert_editorial_structure(
        self, chapter: Chapter | None, sections: list[Section], chunk: KnowledgeGraphChunk
    ) -> None:
        self.editorial_calls.append((chapter, list(sections), chunk))


def _make_book() -> Book:
    return Book(
        id="book-1",
        title="Test Book",
        author="Tester",
        pdf_path="dummy.pdf",
        page_count=100,
    )


def _make_chunks(count: int, book: Book | None = None) -> list[KnowledgeGraphChunk]:
    return [
        KnowledgeGraphChunk(
            text=f"chunk {index}",
            chunk_index=index,
            book=book,
            chapter=Chapter(number=1, title="Chapter 1", page_start=1),
            section=Section(
                chapter_number=1,
                level=2,
                title="Section",
                page_start=index + 1,
                parent_section_title=None,
            ),
            page_ref=PageRef(start=index + 1, end=index + 2),
        )
        for index in range(count)
    ]


def test_use_case_constructor_takes_primitives_not_settings(tmp_path: Path) -> None:
    """AC-04.1: constructor receives ports + primitives, not Settings."""
    hints = get_type_hints(IndexBookUseCase.__init__)
    assert hints["pdf_port"] is PDFReaderPort
    assert hints["llm_port"] is LLMProviderPort
    assert hints["graph_db_port"] is GraphDatabasePort
    assert hints["max_concurrency"] is int
    assert hints["batch_size"] is int
    assert hints["dead_letter_path"] is Path

    signature = inspect.signature(IndexBookUseCase.__init__)
    params = list(signature.parameters)
    assert "settings" not in params

    use_case = IndexBookUseCase(
        pdf_port=_FakePDFPort([]),
        llm_port=_FakeLLMPort(),
        graph_db_port=_FakeGraphDBPort(),
        max_concurrency=3,
        batch_size=5,
        dead_letter_path=tmp_path / "dl.log",
    )
    assert use_case is not None


async def test_use_case_never_exceeds_max_concurrency(tmp_path: Path) -> None:
    """AC-04.2: semaphore keeps LLM concurrency within max_concurrency."""
    chunks = _make_chunks(10, book=_make_book())
    pdf = _FakePDFPort(chunks)
    llm = _FakeLLMPort(delay=0.05)
    graph = _FakeGraphDBPort()
    use_case = IndexBookUseCase(
        pdf_port=pdf,
        llm_port=llm,
        graph_db_port=graph,
        max_concurrency=3,
        batch_size=5,
        dead_letter_path=tmp_path / "dl.log",
    )

    await use_case.execute("dummy.pdf")

    assert llm.max_concurrency_observed() <= 3


async def test_use_case_produces_3_batches_for_12_chunks(tmp_path: Path) -> None:
    """AC-04.3: batch_size=5 over 12 chunks yields exactly 3 upsert batches."""
    chunks = _make_chunks(12, book=_make_book())
    pdf = _FakePDFPort(chunks)
    llm = _FakeLLMPort()
    graph = _FakeGraphDBPort()
    use_case = IndexBookUseCase(
        pdf_port=pdf,
        llm_port=llm,
        graph_db_port=graph,
        max_concurrency=3,
        batch_size=5,
        dead_letter_path=tmp_path / "dl.log",
    )

    await use_case.execute("dummy.pdf")

    assert len(graph.entity_batches_upserted) == 3
    assert [len(batch) for batch in graph.entity_batches_upserted] == [5, 5, 2]
    assert [len(batch) for batch in graph.relationship_batches_upserted] == [5, 5, 2]


async def test_use_case_end_to_end_with_mocks(tmp_path: Path) -> None:
    """AC-04.4: full pipeline completes and upserts entities/relationships."""
    chunks = _make_chunks(5, book=_make_book())
    pdf = _FakePDFPort(chunks)
    llm = _FakeLLMPort()
    graph = _FakeGraphDBPort()
    use_case = IndexBookUseCase(
        pdf_port=pdf,
        llm_port=llm,
        graph_db_port=graph,
        max_concurrency=3,
        batch_size=5,
        dead_letter_path=tmp_path / "dl.log",
    )

    await use_case.execute("dummy.pdf")

    assert len(graph.books_upserted) >= 1
    assert len(graph.entity_batches_upserted) >= 1
    assert len(graph.relationship_batches_upserted) >= 1
    assert len(graph.editorial_calls) == 5


async def test_use_case_skips_failed_chunks_to_dead_letter(tmp_path: Path) -> None:
    """AC-04.5: failed chunks are logged to dead-letter and skipped from graph."""
    chunks = _make_chunks(10, book=_make_book())
    pdf = _FakePDFPort(chunks)
    llm = _FakeLLMPort(fail_indices={3, 7})
    graph = _FakeGraphDBPort()
    dl_path = tmp_path / "dead_letter.log"
    use_case = IndexBookUseCase(
        pdf_port=pdf,
        llm_port=llm,
        graph_db_port=graph,
        max_concurrency=3,
        batch_size=5,
        dead_letter_path=dl_path,
    )

    await use_case.execute("dummy.pdf")

    assert dl_path.exists()
    lines = dl_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    records = [json.loads(line) for line in lines]
    failed_indices = {record["chunk_index"] for record in records}
    assert failed_indices == {3, 7}
    for record in records:
        assert "page_ref" in record
        assert "error_type" in record
        assert "error_message" in record
        assert "timestamp" in record

    all_entity_ids = {entity.id for batch in graph.entity_batches_upserted for entity in batch}
    assert "ent-3" not in all_entity_ids
    assert "ent-7" not in all_entity_ids


async def test_use_case_flushes_partial_batch_at_end(tmp_path: Path) -> None:
    """AC-04.3 edge case: remaining successful chunks are flushed on sentinel."""
    chunks = _make_chunks(12, book=_make_book())
    pdf = _FakePDFPort(chunks)
    llm = _FakeLLMPort(fail_indices={11})  # last chunk fails
    graph = _FakeGraphDBPort()
    use_case = IndexBookUseCase(
        pdf_port=pdf,
        llm_port=llm,
        graph_db_port=graph,
        max_concurrency=3,
        batch_size=5,
        dead_letter_path=tmp_path / "dl.log",
    )

    await use_case.execute("dummy.pdf")

    # 11 successful chunks → batches of 5, 5, 1
    assert len(graph.entity_batches_upserted) == 3
    assert [len(batch) for batch in graph.entity_batches_upserted] == [5, 5, 1]


def test_use_case_does_not_accept_settings() -> None:
    """Extra safety: constructor signature does not mention Settings."""
    from book_graph_rag.config import Settings

    hints = get_type_hints(IndexBookUseCase.__init__)
    assert Settings not in hints.values()
