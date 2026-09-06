"""Staleness scope isolation tests for resumable indexing."""

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

_V1 = VersionDimensions(
    source_version="stale-src-v1",
    pipeline_version="1.0.0",
    model_version="openai:gpt-4o-mini:2026-09-01",
    schema_version="1.0.0",
)
_V2 = VersionDimensions(
    source_version="stale-src-v1",
    pipeline_version="2.0.0",  # pipeline changed
    model_version="openai:gpt-4o-mini:2026-09-01",
    schema_version="1.0.0",
)


class _StubPDFPort(PDFReaderPort):
    def __init__(self, source_id: str, count: int) -> None:
        self._source_id = source_id
        self._count = count

    def extract_chunks(self, file_path: str) -> Iterator[KnowledgeGraphChunk]:
        book = Book(
            id=self._source_id,
            title="Staleness Book",
            author="Test",
            pdf_path=file_path,
            page_count=self._count + 1,
        )
        chapter = Chapter(number=1, title="Chapter 1", page_start=1)
        section = Section(
            chapter_number=1,
            level=2,
            title="Section",
            page_start=1,
            parent_section_title=None,
        )
        for index in range(self._count):
            yield KnowledgeGraphChunk(
                text=f"chunk {index}",
                chunk_index=index,
                book=book,
                chapter=chapter,
                section=section,
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


async def _run_index(
    settings: Settings,
    source_id: str,
    versions: VersionDimensions,
    chunk_count: int,
    force_reprocess: bool = False,
) -> tuple[list[int], dict[int, Checkpoint]]:
    """Run the resumable indexer and return LLM calls + final checkpoint state."""
    llm_port = _CountingLLMPort()
    checkpoint_adapter = Neo4jCheckpointAdapter(settings)
    graph_adapter = Neo4jCommandAdapter(settings)

    use_case = IndexBookUseCase(
        pdf_port=_StubPDFPort(source_id, chunk_count),
        llm_port=llm_port,
        graph_db_port=graph_adapter,
        max_concurrency=1,
        batch_size=5,
        dead_letter_path=Path("/dev/null"),
        checkpoint_port=checkpoint_adapter,
        versions=versions,
        checkpoint_enabled=True,
        resume=True,
        force_reprocess=force_reprocess,
        max_attempts=3,
        stale_lease_seconds=300,
    )

    try:
        await use_case.execute("/tmp/stale.pdf")
    finally:
        await use_case.close()

    state_adapter = Neo4jCheckpointAdapter(settings)
    try:
        state = await state_adapter.fetch_state(source_id, list(range(chunk_count)))
    finally:
        await state_adapter.close()

    return llm_port.calls, state


async def _set_pending(driver: Any, source_id: str, indices: list[int]) -> None:
    """Directly flip checkpoints to PENDING to simulate an interrupted run."""
    async with driver.session() as session:
        await session.run(
            """
            MATCH (c:Checkpoint {source_id: $source_id})
            WHERE c.chunk_index IN $indices
            SET c.status = 'PENDING',
                c.leased_at = null,
                c.processed_at = null,
                c.updated_at = datetime()
            """,
            {"source_id": source_id, "indices": indices},
        )


@pytest.mark.neo4j_integration
async def test_staleness_scope_isolation(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """A version change on source B must mark only B's checkpoints STALE."""
    source_a = "stale:isolated-a"
    source_b = "stale:isolated-b"

    # First run: index both sources under v1.
    calls_a, state_a = await _run_index(neo4j_settings, source_a, _V1, 3)
    calls_b, state_b = await _run_index(neo4j_settings, source_b, _V1, 3)
    assert sorted(calls_a) == [0, 1, 2]
    assert sorted(calls_b) == [0, 1, 2]
    for idx in range(3):
        assert state_a[idx].status == CheckpointStatus.PROCESSED
        assert state_a[idx].versions == _V1
        assert state_b[idx].status == CheckpointStatus.PROCESSED
        assert state_b[idx].versions == _V1

    # Simulate an interrupted run on source B: chunks 0 and 1 are PENDING.
    await _set_pending(neo4j_driver, source_b, [0, 1])

    # Second run: bump pipeline_version and re-index only source B.
    calls_b2, state_b2 = await _run_index(neo4j_settings, source_b, _V2, 3)
    # Only the PENDING chunks with version mismatch are re-processed.
    assert sorted(calls_b2) == [0, 1]
    for idx in [0, 1]:
        assert state_b2[idx].status == CheckpointStatus.PROCESSED
        assert state_b2[idx].versions == _V2
    # Chunk 2 was already PROCESSED under v1 and is preserved without force.
    assert state_b2[2].status == CheckpointStatus.PROCESSED
    assert state_b2[2].versions == _V1

    # Source A must remain untouched: still PROCESSED under v1.
    state_adapter = Neo4jCheckpointAdapter(neo4j_settings)
    try:
        state_a2 = await state_adapter.fetch_state(source_a, [0, 1, 2])
    finally:
        await state_adapter.close()
    for idx in range(3):
        assert state_a2[idx].status == CheckpointStatus.PROCESSED
        assert state_a2[idx].versions == _V1


@pytest.mark.neo4j_integration
async def test_staleness_processed_preserved_without_force(
    neo4j_settings: Settings,
    neo4j_driver: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Without --force-reprocess, PROCESSED rows stay PROCESSED despite version drift."""
    source_id = "stale:processed-preserved"

    calls1, state1 = await _run_index(neo4j_settings, source_id, _V1, 3)
    assert sorted(calls1) == [0, 1, 2]
    for idx in range(3):
        assert state1[idx].status == CheckpointStatus.PROCESSED
        assert state1[idx].versions == _V1

    # Re-run with a new pipeline_version but force_reprocess=False (default).
    calls2, state2 = await _run_index(neo4j_settings, source_id, _V2, 3)
    assert calls2 == []  # no LLM calls because all chunks remain PROCESSED
    for idx in range(3):
        assert state2[idx].status == CheckpointStatus.PROCESSED
        assert state2[idx].versions == _V1

    # A warning must be emitted so the operator notices the version drift.
    assert any(
        "PROCESSED" in record.message and "version" in record.message.lower()
        for record in caplog.records
    )


@pytest.mark.neo4j_integration
async def test_force_reprocess_marks_processed_stale(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """With --force-reprocess, version-mismatched PROCESSED chunks re-process."""
    source_id = "stale:force-reprocess"

    calls1, state1 = await _run_index(neo4j_settings, source_id, _V1, 3)
    assert sorted(calls1) == [0, 1, 2]
    for idx in range(3):
        assert state1[idx].status == CheckpointStatus.PROCESSED
        assert state1[idx].versions == _V1

    # Re-run with a new pipeline_version and force_reprocess=True.
    calls2, state2 = await _run_index(
        neo4j_settings, source_id, _V2, 3, force_reprocess=True
    )
    assert sorted(calls2) == [0, 1, 2]
    for idx in range(3):
        assert state2[idx].status == CheckpointStatus.PROCESSED
        assert state2[idx].versions == _V2
