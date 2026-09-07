"""Lease-orchestration tests for the resumable IndexBookUseCase path."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from book_graph_rag.application.index_book_use_case import IndexBookUseCase
from book_graph_rag.domain.checkpoint_models import (
    Checkpoint,
    CheckpointStatus,
    LeaseResult,
    VersionDimensions,
)
from book_graph_rag.domain.models import (
    Book,
    Chapter,
    Entity,
    KnowledgeGraphChunk,
    PageRef,
    Relationship,
    Section,
)
from book_graph_rag.ports.checkpoint_port import CheckpointPort
from book_graph_rag.ports.dead_letter_port import DeadLetterPort
from book_graph_rag.ports.graph_db_port import GraphDatabasePort
from book_graph_rag.ports.llm_port import LLMProviderPort
from book_graph_rag.ports.pdf_port import PDFReaderPort

_V = VersionDimensions(
    source_version="abcd1234abcd1234",
    pipeline_version="1.0.0",
    model_version="openai:gpt-4o-mini:2026-09-01",
    schema_version="1.0.0",
)


class _StubPDFPort(PDFReaderPort):
    def __init__(self, chunks: list[KnowledgeGraphChunk]) -> None:
        self._chunks = chunks

    def extract_chunks(self, file_path: str) -> Iterator[KnowledgeGraphChunk]:
        yield from self._chunks


class _StubLLMPort(LLMProviderPort):
    def __init__(self, fail_indices: set[int] | None = None) -> None:
        self._fail_indices = fail_indices or set()
        self.calls: list[int] = []

    async def extract_graph(self, chunk: KnowledgeGraphChunk) -> KnowledgeGraphChunk:
        self.calls.append(chunk.chunk_index)
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
        return chunk


class _StubGraphDBPort(GraphDatabasePort):
    def __init__(self) -> None:
        self.commit_calls: list[tuple[KnowledgeGraphChunk, list[str], VersionDimensions, int]] = []
        self.upserted_books: list[Book] = []

    async def upsert_book(self, book: Book) -> None:
        self.upserted_books.append(book)

    async def upsert_entities(self, entities: list[Entity]) -> None:
        return None

    async def upsert_relationships(self, relationships: list[Relationship]) -> None:
        return None

    async def upsert_mentions(
        self, chunk_index: int, book_id: str | None, entity_ids: list[str]
    ) -> None:
        return None

    async def upsert_editorial_structure(
        self, chapter: Chapter | None, sections: list[Section], chunk: KnowledgeGraphChunk
    ) -> None:
        return None

    async def commit_chunk_atomic(
        self,
        chunk: KnowledgeGraphChunk,
        entity_ids: list[str],
        versions: VersionDimensions,
        *,
        attempt: int = 1,
        lease_owned: bool = True,
    ) -> Checkpoint:
        self.commit_calls.append((chunk, list(entity_ids), versions, attempt))
        return Checkpoint(
            source_id=chunk.book.id if chunk.book is not None else "",
            chunk_index=chunk.chunk_index,
            status=CheckpointStatus.PROCESSED,
            attempt=attempt,
            versions=versions,
            updated_at=datetime.now(UTC),
        )

    async def clear_index(self) -> None:
        return None

    async def load_active_entities(self, *, batch_size: int = 500) -> list[Entity]:
        return []

    async def count_chunks(self) -> int:
        return 0

    async def count_entities(self) -> int:
        return 0

    async def count_mentions(self) -> int:
        return 0


class _StubCheckpointPort(CheckpointPort):
    def __init__(self, state: dict[int, Checkpoint] | None = None) -> None:
        self._state = state or {}
        self.acquire_calls: list[tuple[str, int, VersionDimensions]] = []
        self.release_calls: list[tuple[str, int, str, str, int]] = []
        self.mark_stale_calls: list[tuple[str, VersionDimensions]] = []
        self.reclaim_calls: list[tuple[str, datetime, int]] = []
        self.fetch_calls: list[tuple[str, list[int]]] = []

    async def acquire_lease(
        self, source_id: str, chunk_index: int, versions: VersionDimensions
    ) -> LeaseResult:
        self.acquire_calls.append((source_id, chunk_index, versions))
        checkpoint = self._state.get(
            chunk_index,
            Checkpoint(
                source_id=source_id,
                chunk_index=chunk_index,
                status=CheckpointStatus.PROCESSING,
                attempt=0,
                versions=versions,
                updated_at=datetime.now(UTC),
            ),
        )
        return LeaseResult(checkpoint=checkpoint, is_new_lease=True)

    async def release_lease_to_failed(
        self,
        source_id: str,
        chunk_index: int,
        error_type: str,
        error_message: str,
        attempt: int,
    ) -> Checkpoint:
        self.release_calls.append((source_id, chunk_index, error_type, error_message, attempt))
        return Checkpoint(
            source_id=source_id,
            chunk_index=chunk_index,
            status=CheckpointStatus.FAILED,
            attempt=attempt,
            versions=_V,
            updated_at=datetime.now(UTC),
        )

    async def mark_stale_and_reset(
        self, source_id: str, expected_versions: VersionDimensions
    ) -> int:
        self.mark_stale_calls.append((source_id, expected_versions))
        return 0

    async def fetch_state(
        self, source_id: str, chunk_indices: list[int]
    ) -> dict[int, Checkpoint]:
        self.fetch_calls.append((source_id, list(chunk_indices)))
        return {idx: cp for idx, cp in self._state.items() if idx in chunk_indices}

    async def reclaim_stale_leases(
        self, source_id: str, now: datetime, stale_seconds: int
    ) -> int:
        self.reclaim_calls.append((source_id, now, stale_seconds))
        return 0


class _StubDeadLetterPort(DeadLetterPort):
    def __init__(self) -> None:
        self.orphan_records: list[dict[str, Any]] = []
        self.chunk_records: list[dict[str, Any]] = []

    async def write_orphan_relationship(self, record: dict[str, Any]) -> None:
        self.orphan_records.append(record)

    async def write_failed_chunk(self, record: dict[str, Any]) -> None:
        self.chunk_records.append(record)


def _make_book() -> Book:
    return Book(
        id="book-1",
        title="Test Book",
        author="Tester",
        pdf_path="dummy.pdf",
        page_count=100,
    )


def _make_chunk(index: int, book: Book | None = None) -> KnowledgeGraphChunk:
    return KnowledgeGraphChunk(
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


def _use_case(
    *,
    chunks: list[KnowledgeGraphChunk],
    llm: LLMProviderPort | None = None,
    graph: GraphDatabasePort | None = None,
    checkpoint: CheckpointPort | None = None,
    dead_letter: DeadLetterPort | None = None,
    checkpoint_enabled: bool = True,
    resume: bool = True,
    max_attempts: int = 3,
) -> IndexBookUseCase:
    return IndexBookUseCase(
        pdf_port=_StubPDFPort(chunks),
        llm_port=llm or _StubLLMPort(),
        graph_db_port=graph or _StubGraphDBPort(),
        max_concurrency=1,
        batch_size=5,
        dead_letter_path=Path("/dev/null"),
        checkpoint_port=checkpoint,
        dead_letter_port=dead_letter,
        versions=_V,
        checkpoint_enabled=checkpoint_enabled,
        resume=resume,
        max_attempts=max_attempts,
        stale_lease_seconds=300,
    )


async def test_resume_short_circuits_processed_chunks_with_zero_llm_calls() -> None:
    """Already-PROCESSED chunks must not trigger LLM extraction or graph writes."""
    book = _make_book()
    chunks = [_make_chunk(0, book), _make_chunk(1, book)]
    state = {
        0: Checkpoint(
            source_id=book.id,
            chunk_index=0,
            status=CheckpointStatus.PROCESSED,
            attempt=0,
            versions=_V,
            updated_at=datetime.now(UTC),
        ),
        1: Checkpoint(
            source_id=book.id,
            chunk_index=1,
            status=CheckpointStatus.PROCESSED,
            attempt=0,
            versions=_V,
            updated_at=datetime.now(UTC),
        ),
    }
    llm = _StubLLMPort()
    graph = _StubGraphDBPort()
    checkpoint = _StubCheckpointPort(state)
    use_case = _use_case(chunks=chunks, llm=llm, graph=graph, checkpoint=checkpoint)

    await use_case.execute("dummy.pdf")

    assert llm.calls == []
    assert graph.commit_calls == []
    assert checkpoint.acquire_calls == []
    assert checkpoint.fetch_calls == [(book.id, [0, 1])]


async def test_resume_acquires_lease_then_commits_then_releases() -> None:
    """A PENDING chunk follows acquire → extract → commit, with no failure release."""
    book = _make_book()
    chunks = [_make_chunk(0, book)]
    llm = _StubLLMPort()
    graph = _StubGraphDBPort()
    checkpoint = _StubCheckpointPort()
    use_case = _use_case(chunks=chunks, llm=llm, graph=graph, checkpoint=checkpoint)

    await use_case.execute("dummy.pdf")

    assert checkpoint.acquire_calls == [(book.id, 0, _V)]
    assert llm.calls == [0]
    assert len(graph.commit_calls) == 1
    committed_chunk, entity_ids, versions, attempt = graph.commit_calls[0]
    assert committed_chunk.chunk_index == 0
    assert entity_ids == ["ent-0"]
    assert versions == _V
    assert attempt == 1
    assert checkpoint.release_calls == []


async def test_simulated_failure_increments_attempt_and_dead_letters() -> None:
    """A processing failure transitions to FAILED, bumps attempt, and dead-letters."""
    book = _make_book()
    chunks = [_make_chunk(0, book)]
    llm = _StubLLMPort(fail_indices={0})
    graph = _StubGraphDBPort()
    checkpoint = _StubCheckpointPort()
    dead_letter = _StubDeadLetterPort()
    use_case = _use_case(
        chunks=chunks, llm=llm, graph=graph, checkpoint=checkpoint, dead_letter=dead_letter
    )

    await use_case.execute("dummy.pdf")

    assert graph.commit_calls == []
    assert len(checkpoint.release_calls) == 1
    source_id, chunk_index, error_type, error_message, attempt = checkpoint.release_calls[0]
    assert source_id == book.id
    assert chunk_index == 0
    assert error_type == "ValueError"
    assert attempt == 1

    assert len(dead_letter.chunk_records) == 1
    record = dead_letter.chunk_records[0]
    assert record["source_id"] == book.id
    assert record["chunk_index"] == 0
    assert record["attempt"] == 1
    assert record["checkpoint_status"] == "FAILED"
    assert record["error_type"] == "ValueError"


async def test_no_resume_skips_checkpoint_checks() -> None:
    """--no-resume processes every chunk regardless of existing PROCESSED state."""
    book = _make_book()
    chunks = [_make_chunk(0, book)]
    state = {
        0: Checkpoint(
            source_id=book.id,
            chunk_index=0,
            status=CheckpointStatus.PROCESSED,
            attempt=0,
            versions=_V,
            updated_at=datetime.now(UTC),
        ),
    }
    llm = _StubLLMPort()
    graph = _StubGraphDBPort()
    checkpoint = _StubCheckpointPort(state)
    use_case = _use_case(
        chunks=chunks, llm=llm, graph=graph, checkpoint=checkpoint, resume=False
    )

    await use_case.execute("dummy.pdf")

    assert checkpoint.fetch_calls == []
    assert llm.calls == [0]
    assert len(graph.commit_calls) == 1
