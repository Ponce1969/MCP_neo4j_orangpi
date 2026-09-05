"""Integration test for BackfillCheckpointsUseCase (testcontainers Neo4j)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from book_graph_rag.application.backfill_checkpoints_use_case import (
    BackfillCheckpointsUseCase,
)
from book_graph_rag.config import Settings
from book_graph_rag.domain.checkpoint_models import CheckpointStatus, VersionDimensions
from book_graph_rag.infrastructure.neo4j_checkpoint_adapter import Neo4jCheckpointAdapter
from book_graph_rag.infrastructure.neo4j_command_adapter import Neo4jCommandAdapter

_V = VersionDimensions(
    source_version="backfill-src-v1",
    pipeline_version="1.0.0",
    model_version="openai:gpt-4o-mini:2026-09-01",
    schema_version="1.0.0",
)
_SOURCE_ID = "backfill:test"


async def _seed_chunk(
    driver: Any,
    chunk_index: int,
    *,
    with_mention: bool = True,
) -> None:
    """Pre-seed a :Chunk node and optionally an :Entity + :MENTIONS edge."""
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
        if with_mention:
            await session.run(
                """
                MERGE (e:Entity {id: $entity_id})
                SET e.name = $name, e.type = 'concept'
                WITH e
                MATCH (k:Chunk {source_id: $source_id, chunk_index: $chunk_index})
                MERGE (k)-[:MENTIONS]->(e)
                """,
                {
                    "source_id": _SOURCE_ID,
                    "chunk_index": chunk_index,
                    "entity_id": f"ent-{chunk_index}",
                    "name": f"Entity {chunk_index}",
                },
            )


async def _count_checkpoints(driver: Any) -> int:
    async with driver.session() as session:
        result = await session.run("MATCH (c:Checkpoint) RETURN count(c) AS n")
        record = await result.single()
        assert record is not None
        return int(record["n"])


async def _load_checkpoint_state(
    settings: Settings,
) -> dict[int, Any]:
    adapter = Neo4jCheckpointAdapter(settings)
    try:
        return await adapter.fetch_state(_SOURCE_ID, list(range(5)))
    finally:
        await adapter.close()


@pytest.mark.neo4j_integration
async def test_backfill_dry_run_no_writes(
    neo4j_settings: Settings,
    neo4j_driver: Any,
    tmp_path: Path,
) -> None:
    """Dry-run reports candidates but never writes :Checkpoint rows."""
    for index in range(5):
        await _seed_chunk(neo4j_driver, index, with_mention=index < 3)

    use_case = BackfillCheckpointsUseCase(
        checkpoint_port=Neo4jCheckpointAdapter(neo4j_settings),
        graph_db_port=Neo4jCommandAdapter(neo4j_settings),
        versions=_V,
        run_id="dry-run-1",
        evidence_dir=tmp_path / "evidence",
    )
    try:
        report = await use_case.execute(_SOURCE_ID, apply=False)
    finally:
        await use_case.close()

    assert report.dry_run is True
    assert report.source_id == _SOURCE_ID
    assert report.candidate_chunk_indices == [0, 1, 2]
    assert report.processed_count == 0
    assert report.current_versions == _V
    assert report.approval_path is None
    assert report.evidence_bundle_path is None

    assert await _count_checkpoints(neo4j_driver) == 0


@pytest.mark.neo4j_integration
async def test_backfill_apply_without_approval_raises(
    neo4j_settings: Settings,
    neo4j_driver: Any,
    tmp_path: Path,
) -> None:
    """Apply is approval-gated: missing approval file raises before any write."""
    for index in range(3):
        await _seed_chunk(neo4j_driver, index)

    use_case = BackfillCheckpointsUseCase(
        checkpoint_port=Neo4jCheckpointAdapter(neo4j_settings),
        graph_db_port=Neo4jCommandAdapter(neo4j_settings),
        versions=_V,
        run_id="apply-no-approval",
        evidence_dir=tmp_path / "evidence",
    )
    try:
        with pytest.raises(ValueError, match="approval"):
            await use_case.execute(_SOURCE_ID, apply=True)
    finally:
        await use_case.close()

    assert await _count_checkpoints(neo4j_driver) == 0


@pytest.mark.neo4j_integration
async def test_backfill_apply_with_approval_creates_checkpoints(
    neo4j_settings: Settings,
    neo4j_driver: Any,
    tmp_path: Path,
) -> None:
    """Apply with an approval file creates :Checkpoint rows under current versions."""
    for index in range(5):
        await _seed_chunk(neo4j_driver, index, with_mention=index < 3)

    approval_path = tmp_path / "approval.txt"
    approval_path.write_text("approve backfill-apply-1\n", encoding="utf-8")

    evidence_dir = tmp_path / "evidence"
    use_case = BackfillCheckpointsUseCase(
        checkpoint_port=Neo4jCheckpointAdapter(neo4j_settings),
        graph_db_port=Neo4jCommandAdapter(neo4j_settings),
        versions=_V,
        run_id="backfill-apply-1",
        evidence_dir=evidence_dir,
    )
    try:
        report = await use_case.execute(
            _SOURCE_ID,
            apply=True,
            approval_path=approval_path,
        )
    finally:
        await use_case.close()

    assert report.dry_run is False
    assert report.candidate_chunk_indices == [0, 1, 2]
    assert report.processed_count == 3
    assert report.current_versions == _V
    assert report.approval_path == approval_path
    assert report.evidence_bundle_path is not None
    assert report.evidence_bundle_path.exists()

    state = await _load_checkpoint_state(neo4j_settings)
    assert sorted(state.keys()) == [0, 1, 2]
    for index in [0, 1, 2]:
        checkpoint = state[index]
        assert checkpoint.status == CheckpointStatus.PROCESSED
        assert checkpoint.versions == _V
        assert checkpoint.processed_at is not None

    bundle = json.loads(report.evidence_bundle_path.read_text(encoding="utf-8"))
    assert bundle["run_id"] == "backfill-apply-1"
    assert bundle["source_id"] == _SOURCE_ID
    assert bundle["current_versions"] == _V.model_dump(mode="json")
    assert bundle["candidate_chunk_indices"] == [0, 1, 2]
    assert bundle["processed_count"] == 3
    assert "backfilled_at" in bundle
    assert "note" in bundle
