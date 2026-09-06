"""Enforce checkpoint_max_attempts in resumable indexing and dead-letter replay."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from book_graph_rag.application.index_book_use_case import IndexBookUseCase
from book_graph_rag.application.replay_dead_letter_use_case import ReplayDeadLetterUseCase
from book_graph_rag.config import Settings
from book_graph_rag.domain.checkpoint_models import (
    Checkpoint,
    CheckpointStatus,
    FailedChunkRecord,
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
from book_graph_rag.infrastructure.dead_letter import JSONLDeadLetter
from book_graph_rag.infrastructure.neo4j_checkpoint_adapter import Neo4jCheckpointAdapter
from book_graph_rag.infrastructure.neo4j_command_adapter import Neo4jCommandAdapter
from book_graph_rag.ports.llm_port import LLMProviderPort
from book_graph_rag.ports.pdf_port import PDFReaderPort

_V = VersionDimensions(
    source_version="max-attempts-src-v1",
    pipeline_version="1.0.0",
    model_version="openai:gpt-4o-mini:2026-09-01",
    schema_version="1.0.0",
)
_SOURCE_ID = "max-attempts:test"
_BOOK = Book(
    id=_SOURCE_ID,
    title="Max Attempts Book",
    author="Test",
    pdf_path="/tmp/max-attempts.pdf",
    page_count=20,
)
_CHAPTER = Chapter(number=1, title="Chapter 1", page_start=1)
_SECTION = Section(
    chapter_number=1,
    level=2,
    title="Section",
    page_start=1,
    parent_section_title=None,
)


class _StubPDFPort(PDFReaderPort):
    def extract_chunks(self, file_path: str) -> Iterator[KnowledgeGraphChunk]:
        for index in range(2):
            yield KnowledgeGraphChunk(
                text=f"chunk {index}",
                chunk_index=index,
                book=_BOOK,
                chapter=_CHAPTER,
                section=_SECTION,
                page_ref=PageRef(start=index + 1, end=index + 2),
            )


class _CountingLLMPort(LLMProviderPort):
    def __init__(self) -> None:
        self.calls: list[int] = []

    async def extract_graph(self, chunk: KnowledgeGraphChunk) -> KnowledgeGraphChunk:
        self.calls.append(chunk.chunk_index)
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


async def _seed_failed_checkpoint(
    driver: Any,
    chunk_index: int,
    attempt: int,
) -> None:
    """Directly create a FAILED checkpoint with a given attempt count."""
    async with driver.session() as session:
        await session.run(
            """
            MERGE (c:Checkpoint {source_id: $source_id, chunk_index: $chunk_index})
            ON CREATE SET c.created_at = datetime()
            SET c.status = 'FAILED',
                c.attempt = $attempt,
                c.source_version = $source_version,
                c.pipeline_version = $pipeline_version,
                c.model_version = $model_version,
                c.schema_version = $schema_version,
                c.failed_at = datetime(),
                c.updated_at = datetime()
            """,
            {
                "source_id": _SOURCE_ID,
                "chunk_index": chunk_index,
                "attempt": attempt,
                "source_version": _V.source_version,
                "pipeline_version": _V.pipeline_version,
                "model_version": _V.model_version,
                "schema_version": _V.schema_version,
            },
        )


async def _fetch_state(settings: Settings) -> dict[int, Checkpoint]:
    adapter = Neo4jCheckpointAdapter(settings)
    try:
        return await adapter.fetch_state(_SOURCE_ID, list(range(2)))
    finally:
        await adapter.close()


@pytest.mark.neo4j_integration
async def test_index_use_case_skips_chunks_at_max_attempts(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """A chunk whose stored attempt count has reached the limit is not retried."""
    # Arrange: chunk 0 has already exhausted its retry budget; chunk 1 is fresh.
    await _seed_failed_checkpoint(neo4j_driver, chunk_index=0, attempt=3)

    llm_port = _CountingLLMPort()
    checkpoint_adapter = Neo4jCheckpointAdapter(neo4j_settings)
    graph_adapter = Neo4jCommandAdapter(neo4j_settings)

    use_case = IndexBookUseCase(
        pdf_port=_StubPDFPort(),
        llm_port=llm_port,
        graph_db_port=graph_adapter,
        max_concurrency=1,
        batch_size=5,
        dead_letter_path=Path("/dev/null"),
        checkpoint_port=checkpoint_adapter,
        versions=_V,
        checkpoint_enabled=True,
        resume=True,
        max_attempts=3,
        stale_lease_seconds=300,
    )

    try:
        await use_case.execute("/tmp/max-attempts.pdf")
    finally:
        await use_case.close()

    # The exhausted chunk must not have triggered another LLM call.
    assert llm_port.calls == [1]

    state = await _fetch_state(neo4j_settings)
    assert state[0].status == CheckpointStatus.FAILED
    assert state[0].attempt == 3
    assert state[1].status == CheckpointStatus.PROCESSED
    assert state[1].versions == _V


async def _seed_chunk(driver: Any, chunk_index: int) -> None:
    async with driver.session() as session:
        await session.run(
            """
            MERGE (k:Chunk {source_id: $source_id, chunk_index: $chunk_index})
            SET k.text = $text,
                k.page_start = $page_start,
                k.page_end = $page_end
            """,
            {
                "source_id": _SOURCE_ID,
                "chunk_index": chunk_index,
                "text": f"Text for chunk {chunk_index}",
                "page_start": chunk_index + 1,
                "page_end": chunk_index + 2,
            },
        )


async def _load_chunk_from_graph(
    driver: Any, source_id: str, chunk_index: int
) -> KnowledgeGraphChunk:
    async with driver.session() as session:
        result = await session.run(
            """
            MATCH (k:Chunk {source_id: $source_id, chunk_index: $chunk_index})
            RETURN k.text AS text, k.page_start AS page_start, k.page_end AS page_end
            """,
            {"source_id": source_id, "chunk_index": chunk_index},
        )
        record = await result.single()
        assert record is not None
    return KnowledgeGraphChunk(
        text=record["text"],
        chunk_index=chunk_index,
        book=Book(id=source_id, title="", author="", pdf_path="", page_count=0),
        page_ref=PageRef(start=record["page_start"], end=record["page_end"]),
    )


def _dead_letter_record(chunk_index: int, attempt: int) -> dict[str, Any]:
    return FailedChunkRecord(
        source_id=_SOURCE_ID,
        chunk_index=chunk_index,
        page_ref=PageRef(start=chunk_index + 1, end=chunk_index + 2),
        source_version=_V.source_version,
        pipeline_version=_V.pipeline_version,
        model_version=_V.model_version,
        schema_version=_V.schema_version,
        attempt=attempt,
        checkpoint_status=CheckpointStatus.FAILED,
        error_type="LLMExtractionError",
        error_message="simulated failure",
    ).model_dump(mode="json")


@pytest.mark.neo4j_integration
async def test_replay_dead_letter_skips_chunks_at_max_attempts(
    neo4j_settings: Settings,
    neo4j_driver: Any,
    tmp_path: Path,
) -> None:
    """Replay respects checkpoint_max_attempts and skips exhausted chunks."""
    # Arrange: two FAILED checkpoints, one exhausted and one with budget left.
    for index in range(2):
        await _seed_chunk(neo4j_driver, index)

    checkpoint_adapter = Neo4jCheckpointAdapter(neo4j_settings)
    try:
        for index, attempt in ((0, 3), (1, 2)):
            await checkpoint_adapter.acquire_lease(_SOURCE_ID, index, _V)
            await checkpoint_adapter.release_lease_to_failed(
                _SOURCE_ID,
                index,
                error_type="LLMExtractionError",
                error_message="simulated failure",
                attempt=attempt,
            )
    finally:
        await checkpoint_adapter.close()

    dead_letter_path = tmp_path / "failed_chunks.jsonl"
    with dead_letter_path.open("w", encoding="utf-8") as f:
        for index, attempt in ((0, 3), (1, 2)):
            f.write(json.dumps(_dead_letter_record(index, attempt)) + "\n")

    async def chunk_loader(source_id: str, chunk_index: int) -> KnowledgeGraphChunk:
        return await _load_chunk_from_graph(neo4j_driver, source_id, chunk_index)

    llm_port = _CountingLLMPort()
    dead_letter_port = JSONLDeadLetter(dead_letter_path)

    use_case = ReplayDeadLetterUseCase(
        checkpoint_port=Neo4jCheckpointAdapter(neo4j_settings),
        graph_db_port=Neo4jCommandAdapter(neo4j_settings),
        llm_port=llm_port,
        dead_letter_port=dead_letter_port,
        versions=_V,
        chunk_loader=chunk_loader,
        dead_letter_path=dead_letter_path,
        max_attempts=3,
    )

    try:
        processed = await use_case.execute(source_id=_SOURCE_ID)
    finally:
        await use_case.close()

    assert processed == 1
    assert llm_port.calls == [1]

    checkpoint_adapter = Neo4jCheckpointAdapter(neo4j_settings)
    try:
        state = await checkpoint_adapter.fetch_state(_SOURCE_ID, list(range(2)))
    finally:
        await checkpoint_adapter.close()

    assert state[0].status == CheckpointStatus.FAILED
    assert state[0].attempt == 3
    assert state[1].status == CheckpointStatus.PROCESSED
    assert state[1].versions == _V
