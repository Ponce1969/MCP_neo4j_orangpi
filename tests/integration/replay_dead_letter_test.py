"""Integration test for ReplayDeadLetterUseCase (testcontainers Neo4j)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from book_graph_rag.config import Settings
from book_graph_rag.domain.checkpoint_models import (
    CheckpointStatus,
    FailedChunkRecord,
    VersionDimensions,
)
from book_graph_rag.domain.models import Book, Entity, KnowledgeGraphChunk, PageRef
from book_graph_rag.infrastructure.dead_letter import JSONLDeadLetter
from book_graph_rag.infrastructure.neo4j_checkpoint_adapter import Neo4jCheckpointAdapter
from book_graph_rag.infrastructure.neo4j_command_adapter import Neo4jCommandAdapter
from book_graph_rag.ports.llm_port import LLMProviderPort

_V = VersionDimensions(
    source_version="replay-src-v1",
    pipeline_version="1.0.0",
    model_version="openai:gpt-4o-mini:2026-09-01",
    schema_version="1.0.0",
)
_SOURCE_ID = "replay:test"


class _StubLLMPort(LLMProviderPort):
    async def extract_graph(self, chunk: KnowledgeGraphChunk) -> KnowledgeGraphChunk:
        chunk.entities = [
            Entity(
                id=f"ent-{chunk.chunk_index}",
                name=f"Entity {chunk.chunk_index}",
                type="concept",
            )
        ]
        return chunk


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


def _dead_letter_record(chunk_index: int) -> dict[str, Any]:
    return FailedChunkRecord(
        source_id=_SOURCE_ID,
        chunk_index=chunk_index,
        page_ref=PageRef(start=chunk_index + 1, end=chunk_index + 2),
        source_version=_V.source_version,
        pipeline_version=_V.pipeline_version,
        model_version=_V.model_version,
        schema_version=_V.schema_version,
        attempt=2,
        checkpoint_status=CheckpointStatus.FAILED,
        error_type="LLMExtractionError",
        error_message="simulated failure",
    ).model_dump(mode="json")


@pytest.mark.neo4j_integration
async def test_replay_dead_letter_moves_failed_chunks_to_processed(
    neo4j_settings: Settings,
    neo4j_driver: Any,
    tmp_path: Path,
) -> None:
    """Seeding 3 FAILED checkpoints + dead-letter records and replaying them leaves
    all three chunks PROCESSED with current version dimensions."""
    from book_graph_rag.application.replay_dead_letter_use_case import (
        ReplayDeadLetterUseCase,
    )

    # Arrange: seed Chunk nodes, FAILED checkpoints, and dead-letter JSONL.
    for index in range(3):
        await _seed_chunk(neo4j_driver, index)

    checkpoint_adapter = Neo4jCheckpointAdapter(neo4j_settings)
    graph_adapter = Neo4jCommandAdapter(neo4j_settings)
    try:
        for index in range(3):
            await checkpoint_adapter.acquire_lease(_SOURCE_ID, index, _V)
            await checkpoint_adapter.release_lease_to_failed(
                _SOURCE_ID,
                index,
                error_type="LLMExtractionError",
                error_message="simulated failure",
                attempt=2,
            )
    finally:
        await checkpoint_adapter.close()

    dead_letter_path = tmp_path / "failed_chunks.jsonl"
    with dead_letter_path.open("w", encoding="utf-8") as f:
        for index in range(3):
            f.write(json.dumps(_dead_letter_record(index)) + "\n")

    async def chunk_loader(source_id: str, chunk_index: int) -> KnowledgeGraphChunk:
        return await _load_chunk_from_graph(neo4j_driver, source_id, chunk_index)

    dead_letter_port = JSONLDeadLetter(dead_letter_path)

    use_case = ReplayDeadLetterUseCase(
        checkpoint_port=Neo4jCheckpointAdapter(neo4j_settings),
        graph_db_port=graph_adapter,
        llm_port=_StubLLMPort(),
        dead_letter_port=dead_letter_port,
        versions=_V,
        chunk_loader=chunk_loader,
        dead_letter_path=dead_letter_path,
    )

    try:
        await use_case.execute(source_id=_SOURCE_ID)
    finally:
        await use_case.close()

    # Assert: every targeted checkpoint is now PROCESSED under current versions.
    checkpoint_adapter = Neo4jCheckpointAdapter(neo4j_settings)
    try:
        state = await checkpoint_adapter.fetch_state(_SOURCE_ID, list(range(3)))
    finally:
        await checkpoint_adapter.close()

    assert len(state) == 3
    for index in range(3):
        checkpoint = state[index]
        assert checkpoint.status == CheckpointStatus.PROCESSED
        assert checkpoint.versions == _V

    # Assert: the replayed chunks were actually written to the graph.
    async with neo4j_driver.session() as session:
        entity_count = await session.run("MATCH (n:Entity) RETURN count(n) AS c")
        record = await entity_count.single()
        assert record is not None
        assert record["c"] == 3

        mention_count = await session.run(
            "MATCH (:Chunk)-[:MENTIONS]->(:Entity) RETURN count(*) AS c"
        )
        record = await mention_count.single()
        assert record is not None
        assert record["c"] == 3
