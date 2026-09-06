"""Property: resuming from an arbitrary interruption point never loses chunks.

This fuzzes the §3.1 invariant of ``docs/spec/01-resumable-indexing.md``:
``input_chunks == processed ∪ processing ∪ failed ∪ skipped`` (no silent loss).
Each example seeds some prefix of chunks as already ``PROCESSED`` (the point at
which a previous run was "killed") and then runs the resumable use case. The
final state must contain every input chunk as ``PROCESSED`` and the graph must
contain exactly the expected chunks/entities/mentions.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from book_graph_rag.application.index_book_use_case import IndexBookUseCase
from book_graph_rag.config import Settings
from book_graph_rag.domain.checkpoint_models import (
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
    source_version="fuzz-src-v1",
    pipeline_version="1.0.0",
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
            title="Fuzz Book",
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


async def _seed_processed_chunks(
    settings: Settings,
    source_id: str,
    count: int,
) -> None:
    """Commit the first ``count`` chunks so they appear already PROCESSED."""
    if count == 0:
        return
    graph_adapter = Neo4jCommandAdapter(settings)
    try:
        pdf_port = _StubPDFPort(source_id, count)
        for chunk in pdf_port.extract_chunks("/tmp/fuzz.pdf"):
            entity = Entity(
                id=f"ent-{chunk.chunk_index}",
                name=f"Entity {chunk.chunk_index}",
                type="concept",
            )
            chunk.entities = [entity]
            await graph_adapter.commit_chunk_atomic(
                chunk,
                entity_ids=[entity.id],
                versions=_V,
                attempt=1,
            )
    finally:
        await graph_adapter.close()


async def _wipe_graph(driver: Any) -> None:
    """Delete every node and relationship in the test container.

    Examples run sequentially against the shared container, so a full wipe
    between examples keeps them isolated without relying on source_id-scoped
    cleanup that can miss relationships.
    """
    async with driver.session() as session:
        await session.run("MATCH (n) DETACH DELETE n")


async def _fetch_checkpoint_state(
    settings: Settings, source_id: str, chunk_count: int
) -> dict[int, CheckpointStatus]:
    adapter = Neo4jCheckpointAdapter(settings)
    try:
        state = await adapter.fetch_state(source_id, list(range(chunk_count)))
    finally:
        await adapter.close()
    return {idx: state[idx].status for idx in sorted(state)}


async def _count_graph_rows_for_source(driver: Any, source_id: str) -> tuple[int, int, int]:
    """Return (chunks, entities, mentions) scoped to ``source_id``."""
    async with driver.session() as session:
        chunk_result = await session.run(
            "MATCH (k:Chunk {source_id: $source_id}) RETURN count(k) AS c",
            {"source_id": source_id},
        )
        record = await chunk_result.single()
        chunks = int(record["c"]) if record else 0

        entity_result = await session.run(
            """
            MATCH (e:Entity)<-[:MENTIONS]-(k:Chunk {source_id: $source_id})
            RETURN count(DISTINCT e) AS c
            """,
            {"source_id": source_id},
        )
        record = await entity_result.single()
        entities = int(record["c"]) if record else 0

        mention_result = await session.run(
            """
            MATCH (k:Chunk {source_id: $source_id})-[:MENTIONS]->(:Entity)
            RETURN count(*) AS c
            """,
            {"source_id": source_id},
        )
        record = await mention_result.single()
        mentions = int(record["c"]) if record else 0

    return chunks, entities, mentions


@pytest.mark.neo4j_integration
@given(
    chunk_count=st.integers(min_value=1, max_value=5),
    processed_before=st.integers(min_value=0, max_value=4),
)
@settings(
    max_examples=10,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
async def test_interruption_fuzz_invariant_3_1(
    neo4j_settings: Settings,
    neo4j_driver: Any,
    chunk_count: int,
    processed_before: int,
) -> None:
    """Resuming from any interruption point accounts for every input chunk.

    The ``processed_before`` parameter models the number of chunks that were
    fully committed before a kill. The remaining chunks must be processed by the
    resumed run, and the final checkpoint set must be exactly ``PROCESSED``.
    """
    actual_processed = min(processed_before, chunk_count)
    source_id = f"prop:interruption-{chunk_count}-{actual_processed}-{uuid.uuid4().hex[:8]}"

    await _wipe_graph(neo4j_driver)
    await _seed_processed_chunks(neo4j_settings, source_id, actual_processed)

    llm_port = _CountingLLMPort()
    checkpoint_adapter = Neo4jCheckpointAdapter(neo4j_settings)
    graph_adapter = Neo4jCommandAdapter(neo4j_settings)

    use_case = IndexBookUseCase(
        pdf_port=_StubPDFPort(source_id, chunk_count),
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
        await use_case.execute("/tmp/fuzz.pdf")
    finally:
        await use_case.close()

    try:
        # No silent loss: every input chunk ends as PROCESSED.
        final_state = await _fetch_checkpoint_state(
            neo4j_settings, source_id, chunk_count
        )
        assert sorted(final_state.keys()) == list(range(chunk_count))
        for status in final_state.values():
            assert status == CheckpointStatus.PROCESSED

        # The graph is consistent with the full run.
        chunks, entities, mentions = await _count_graph_rows_for_source(
            neo4j_driver, source_id
        )
        assert chunks == chunk_count
        assert entities == chunk_count
        assert mentions == chunk_count

        # Resume semantics: only unprocessed chunks triggered LLM extraction.
        expected_calls = sorted(set(range(chunk_count)) - set(range(actual_processed)))
        assert sorted(llm_port.calls) == expected_calls
    finally:
        await _wipe_graph(neo4j_driver)

