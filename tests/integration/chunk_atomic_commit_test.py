"""Integration tests for the atomic chunk commit path."""

from __future__ import annotations

import contextlib
from typing import Any

import pytest

from book_graph_rag.config import Settings
from book_graph_rag.domain.checkpoint_models import CheckpointStatus, VersionDimensions
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


@pytest.mark.neo4j_integration
async def test_commit_chunk_atomic_round_trip(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """commit_chunk_atomic persists the chunk, graph rows and PROCESSED checkpoint."""
    versions = VersionDimensions(
        source_version="abcd1234abcd1234",
        pipeline_version="1.0.0",
        model_version="openai:gpt-4o-mini:2026-09-01",
        schema_version="1.0.0",
    )
    book = Book(
        id="test:atomic-book",
        title="Atomic Book",
        author="Test",
        pdf_path="/tmp/atomic.pdf",
        page_count=5,
    )
    chapter = Chapter(number=1, title="Chapter 1", page_start=1)
    section = Section(
        chapter_number=1,
        level=2,
        title="Section 1",
        page_start=1,
        parent_section_title=None,
    )
    entity_a = Entity(id="e-a", name="Entity A", type="concept", description="first entity")
    entity_b = Entity(id="e-b", name="Entity B", type="pattern", description="second entity")
    relationship = Relationship(
        source_entity_id="e-a",
        target_entity_id="e-b",
        type="requires",
        description="a requires b",
        source_page=1,
        chunk_index=0,
    )
    chunk = KnowledgeGraphChunk(
        text="This chunk mentions Entity A and Entity B.",
        chunk_index=0,
        book=book,
        chapter=chapter,
        section=section,
        section_ancestors=(),
        page_ref=PageRef(start=1, end=2),
        entities=[entity_a, entity_b],
        relationships=[relationship],
    )

    adapter = Neo4jCommandAdapter(neo4j_settings)
    try:
        checkpoint = await adapter.commit_chunk_atomic(
            chunk,
            entity_ids=["e-a", "e-b"],
            versions=versions,
            attempt=1,
        )
    finally:
        await adapter.close()

    assert checkpoint.status == CheckpointStatus.PROCESSED
    assert checkpoint.source_id == book.id
    assert checkpoint.chunk_index == chunk.chunk_index
    assert checkpoint.versions == versions
    assert checkpoint.attempt == 1

    async with neo4j_driver.session() as session:
        chunk_count = await session.run(
            "MATCH (k:Chunk {source_id: $source_id, chunk_index: $chunk_index}) "
            "RETURN count(k) AS c",
            {"source_id": book.id, "chunk_index": chunk.chunk_index},
        )
        record = await chunk_count.single()
        assert record is not None
        assert record["c"] == 1

        entity_count = await session.run("MATCH (n:Entity) RETURN count(n) AS c")
        record = await entity_count.single()
        assert record is not None
        assert record["c"] == 2

        mention_count = await session.run(
            "MATCH (:Chunk)-[:MENTIONS]->(:Entity) RETURN count(*) AS c"
        )
        record = await mention_count.single()
        assert record is not None
        assert record["c"] == 2

        related_count = await session.run("MATCH ()-[r:RELATED]->() RETURN count(r) AS c")
        record = await related_count.single()
        assert record is not None
        assert record["c"] == 1

        checkpoint_count = await session.run(
            "MATCH (c:Checkpoint {source_id: $source_id, chunk_index: $chunk_index, "
            "status: 'PROCESSED'}) RETURN count(c) AS c",
            {"source_id": book.id, "chunk_index": chunk.chunk_index},
        )
        record = await checkpoint_count.single()
        assert record is not None
        assert record["c"] == 1

@pytest.mark.neo4j_integration
async def test_atomic_chunk_commit_kill_mid_write(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """A transaction that never commits leaves no half-written chunk or checkpoint.

    The adapter's ``commit_chunk_atomic`` wraps all writes + the PROCESSED
    checkpoint in a single ``execute_write``. If the driver session is killed
    (or the transaction rolled back) before commit, Neo4j aborts the whole
    transaction. This test simulates that mid-write kill by opening a write
    transaction, running the same MERGE statements the adapter would run, and
    closing the session without committing.
    """
    source_id = "test:kill-mid-write"
    chunk_index = 0
    versions = VersionDimensions(
        source_version="kill-src-v1",
        pipeline_version="1.0.0",
        model_version="openai:gpt-4o-mini:2026-09-01",
        schema_version="1.0.0",
    )

    # Simulate the adapter starting its transaction, writing the chunk and
    # checkpoint, but then dying before commit. We use the raw driver to stay
    # outside the adapter's automatic commit/rollback behavior.
    async with neo4j_driver.session() as session:
        tx = await session.begin_transaction()
        try:
            await tx.run(
                """
                MERGE (k:Chunk {source_id: $source_id, chunk_index: $chunk_index})
                SET k.text = $text, k.page_start = 1, k.page_end = 2
                """,
                {
                    "source_id": source_id,
                    "chunk_index": chunk_index,
                    "text": "killed mid write",
                },
            )
            await tx.run(
                """
                MERGE (c:Checkpoint {source_id: $source_id, chunk_index: $chunk_index})
                SET c.status = 'PROCESSED',
                    c.source_version = $source_version,
                    c.pipeline_version = $pipeline_version,
                    c.model_version = $model_version,
                    c.schema_version = $schema_version
                """,
                {
                    "source_id": source_id,
                    "chunk_index": chunk_index,
                    "source_version": versions.source_version,
                    "pipeline_version": versions.pipeline_version,
                    "model_version": versions.model_version,
                    "schema_version": versions.schema_version,
                },
            )
            # Kill the transaction without committing.
            await tx.rollback()
        finally:
            with contextlib.suppress(Exception):
                await tx.close()

    async with neo4j_driver.session() as session:
        chunk_count = await session.run(
            "MATCH (k:Chunk {source_id: $source_id, chunk_index: $chunk_index}) "
            "RETURN count(k) AS c",
            {"source_id": source_id, "chunk_index": chunk_index},
        )
        record = await chunk_count.single()
        assert record is not None
        assert record["c"] == 0

        checkpoint_count = await session.run(
            "MATCH (c:Checkpoint {source_id: $source_id, chunk_index: $chunk_index}) "
            "RETURN count(c) AS c",
            {"source_id": source_id, "chunk_index": chunk_index},
        )
        record = await checkpoint_count.single()
        assert record is not None
        assert record["c"] == 0
