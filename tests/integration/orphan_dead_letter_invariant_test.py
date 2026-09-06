"""Orphan-dead-letter invariant: missing endpoints are logged, not dropped silently."""

from __future__ import annotations

import json
from pathlib import Path
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
from book_graph_rag.infrastructure.dead_letter import JSONLDeadLetter
from book_graph_rag.infrastructure.neo4j_command_adapter import Neo4jCommandAdapter

_V = VersionDimensions(
    source_version="orphan-src-v1",
    pipeline_version="1.0.0",
    model_version="openai:gpt-4o-mini:2026-09-01",
    schema_version="1.0.0",
)


@pytest.mark.neo4j_integration
async def test_orphan_dead_letter_invariant(
    neo4j_settings: Settings,
    neo4j_driver: Any,
    tmp_path: Path,
) -> None:
    """A relationship with a missing endpoint is dead-lettered, not silently dropped.

    The atomic transaction commits the chunk, its entity, and the valid MENTIONS
    edge. The invalid RELATED edge is omitted from the graph and appended to the
    orphan dead-letter log instead.
    """
    source_id = "test:orphan-invariant"
    orphan_path = tmp_path / "orphans.jsonl"
    dead_letter_port = JSONLDeadLetter(orphan_path)

    # Configure the adapter to log orphans instead of failing loud.
    settings = neo4j_settings.model_copy(
        update={"relationship_orphan_policy": "log_orphan"}
    )
    adapter = Neo4jCommandAdapter(settings, dead_letter_port=dead_letter_port)

    book = Book(
        id=source_id,
        title="Orphan Book",
        author="Test",
        pdf_path="/tmp/orphan.pdf",
        page_count=5,
    )
    chapter = Chapter(number=1, title="Chapter 1", page_start=1)
    section = Section(
        chapter_number=1,
        level=2,
        title="Section",
        page_start=1,
        parent_section_title=None,
    )
    entity = Entity(id="e-present", name="Present Entity", type="concept")
    relationship = Relationship(
        source_entity_id="e-present",
        target_entity_id="e-missing",  # missing endpoint
        type="requires",
        description="present requires missing",
        source_page=1,
        chunk_index=0,
    )
    chunk = KnowledgeGraphChunk(
        text="This chunk mentions one entity and a missing one.",
        chunk_index=0,
        book=book,
        chapter=chapter,
        section=section,
        page_ref=PageRef(start=1, end=2),
        entities=[entity],
        relationships=[relationship],
    )

    try:
        checkpoint = await adapter.commit_chunk_atomic(
            chunk,
            entity_ids=["e-present"],
            versions=_V,
            attempt=1,
        )
    finally:
        await adapter.close()

    assert checkpoint.status == CheckpointStatus.PROCESSED

    # The chunk and its entity were committed.
    async with neo4j_driver.session() as session:
        chunk_count = await session.run(
            "MATCH (k:Chunk {source_id: $source_id, chunk_index: 0}) RETURN count(k) AS c",
            {"source_id": source_id},
        )
        record = await chunk_count.single()
        assert record is not None
        assert record["c"] == 1

        entity_count = await session.run("MATCH (n:Entity) RETURN count(n) AS c")
        record = await entity_count.single()
        assert record is not None
        assert record["c"] == 1

        mention_count = await session.run(
            "MATCH (:Chunk)-[:MENTIONS]->(:Entity) RETURN count(*) AS c"
        )
        record = await mention_count.single()
        assert record is not None
        assert record["c"] == 1

        related_count = await session.run("MATCH ()-[r:RELATED]->() RETURN count(r) AS c")
        record = await related_count.single()
        assert record is not None
        assert record["c"] == 0

    # The orphan relationship landed in the dead-letter log.
    lines = orphan_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    orphan = json.loads(lines[0])
    assert orphan["reason"] == "orphan_endpoint"
    assert orphan["source_entity_id"] == "e-present"
    assert orphan["target_entity_id"] == "e-missing"
    assert orphan["missing_endpoint"] == "target"
