"""Kill-switch integration test: resuming skips already-PROCESSED chunks."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from book_graph_rag.application.index_book_use_case import IndexBookUseCase
from book_graph_rag.config import Settings
from book_graph_rag.domain.checkpoint_models import (
    Checkpoint,
    CheckpointStatus,
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
from book_graph_rag.infrastructure.neo4j_checkpoint_adapter import Neo4jCheckpointAdapter
from book_graph_rag.infrastructure.neo4j_command_adapter import Neo4jCommandAdapter
from book_graph_rag.ports.llm_port import LLMProviderPort
from book_graph_rag.ports.pdf_port import PDFReaderPort

_V = VersionDimensions(
    source_version="resume-src-v1",
    pipeline_version="1.0.0",
    model_version="openai:gpt-4o-mini:2026-09-01",
    schema_version="1.0.0",
)
_SOURCE_ID = "resume:kill-switch"
_BOOK = Book(
    id=_SOURCE_ID,
    title="Kill Switch Book",
    author="Test",
    pdf_path="/tmp/kill.pdf",
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
        for index in range(10):
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


async def _seed_processed_checkpoints(driver: Any, count: int) -> None:
    """Directly create ``count`` PROCESSED checkpoints via the driver."""
    async with driver.session() as session:
        for index in range(count):
            await session.run(
                """
                MERGE (c:Checkpoint {source_id: $source_id, chunk_index: $chunk_index})
                ON CREATE SET c.created_at = datetime(), c.attempt = 1
                SET c.status = 'PROCESSED',
                    c.source_version = $source_version,
                    c.pipeline_version = $pipeline_version,
                    c.model_version = $model_version,
                    c.schema_version = $schema_version,
                    c.processed_at = datetime(),
                    c.updated_at = datetime()
                """,
                {
                    "source_id": _SOURCE_ID,
                    "chunk_index": index,
                    "source_version": _V.source_version,
                    "pipeline_version": _V.pipeline_version,
                    "model_version": _V.model_version,
                    "schema_version": _V.schema_version,
                },
            )


async def _fetch_state(settings: Settings) -> dict[int, Checkpoint]:
    adapter = Neo4jCheckpointAdapter(settings)
    try:
        return await adapter.fetch_state(_SOURCE_ID, list(range(10)))
    finally:
        await adapter.close()


@pytest.mark.neo4j_integration
async def test_resume_kill_switch_llm_calls_six_of_ten(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """A run killed after 4 PROCESSED chunks resumes with exactly 6 LLM calls."""
    await _seed_processed_checkpoints(neo4j_driver, 4)

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
        await use_case.execute("/tmp/kill.pdf")
    finally:
        await use_case.close()

    # The resume must not re-extract the first 4 already-PROCESSED chunks.
    assert sorted(llm_port.calls) == [4, 5, 6, 7, 8, 9]

    # Every chunk must now be PROCESSED.
    state = await _fetch_state(neo4j_settings)
    assert sorted(state.keys()) == list(range(10))
    for index in range(10):
        assert state[index].status == CheckpointStatus.PROCESSED
        assert state[index].versions == _V

    # The graph must contain the 6 entities written during the resumed run.
    async with neo4j_driver.session() as session:
        entity_count = await session.run("MATCH (n:Entity) RETURN count(n) AS c")
        record = await entity_count.single()
        assert record is not None
        assert record["c"] == 6

        mention_count = await session.run(
            "MATCH (:Chunk)-[:MENTIONS]->(:Entity) RETURN count(*) AS c"
        )
        record = await mention_count.single()
        assert record is not None
        assert record["c"] == 6
