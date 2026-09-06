"""Property: backfill dry-run reports exactly the candidate subset.

For an arbitrary subset of chunks that have ``(:Chunk)-[:MENTIONS]->(:Entity)``
edges, ``BackfillCheckpointsUseCase.execute(..., apply=False)`` must report
exactly those chunk indices as candidates and must write no ``:Checkpoint``
rows.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from book_graph_rag.application.backfill_checkpoints_use_case import (
    BackfillCheckpointsUseCase,
)
from book_graph_rag.config import Settings
from book_graph_rag.domain.checkpoint_models import VersionDimensions
from book_graph_rag.infrastructure.neo4j_checkpoint_adapter import Neo4jCheckpointAdapter
from book_graph_rag.infrastructure.neo4j_command_adapter import Neo4jCommandAdapter

_V = VersionDimensions(
    source_version="backfill-prop-v1",
    pipeline_version="1.0.0",
    model_version="openai:gpt-4o-mini:2026-09-01",
    schema_version="1.0.0",
)


async def _seed_chunks(driver: Any, source_id: str, indices: set[int]) -> None:
    """Create :Chunk nodes with :MENTIONS edges for the given indices."""
    async with driver.session() as session:
        for index in sorted(indices):
            await session.run(
                """
                MERGE (k:Chunk {source_id: $source_id, chunk_index: $chunk_index})
                SET k.text = $text,
                    k.page_start = $page_start,
                    k.page_end = $page_end
                """,
                {
                    "source_id": source_id,
                    "chunk_index": index,
                    "text": f"Text for chunk {index}",
                    "page_start": index + 1,
                    "page_end": index + 2,
                },
            )
            await session.run(
                """
                MERGE (e:Entity {id: $entity_id})
                SET e.name = $name, e.type = 'concept'
                WITH e
                MATCH (k:Chunk {source_id: $source_id, chunk_index: $chunk_index})
                MERGE (k)-[:MENTIONS]->(e)
                """,
                {
                    "source_id": source_id,
                    "chunk_index": index,
                    "entity_id": f"ent-{source_id}-{index}",
                    "name": f"Entity {index}",
                },
            )


async def _count_checkpoints(driver: Any, source_id: str) -> int:
    async with driver.session() as session:
        result = await session.run(
            "MATCH (c:Checkpoint {source_id: $source_id}) RETURN count(c) AS n",
            {"source_id": source_id},
        )
        record = await result.single()
        assert record is not None
        return int(record["n"])


async def _cleanup_source(driver: Any, source_id: str) -> None:
    """Remove all graph data and checkpoints owned by ``source_id``."""
    async with driver.session() as session:
        await session.run(
            """
            MATCH (k:Chunk {source_id: $source_id})
            OPTIONAL MATCH (k)-[r]->()
            DELETE r, k
            """,
            {"source_id": source_id},
        )
        await session.run(
            "MATCH (c:Checkpoint {source_id: $source_id}) DETACH DELETE c",
            {"source_id": source_id},
        )
        await session.run(
            """
            MATCH (e:Entity)
            WHERE e.id STARTS WITH $prefix
            DETACH DELETE e
            """,
            {"prefix": f"ent-{source_id}-"},
        )


@pytest.mark.neo4j_integration
@given(
    total_chunks=st.integers(min_value=0, max_value=8),
    candidate_indices=st.sets(st.integers(min_value=0, max_value=7), max_size=8),
)
@settings(
    max_examples=10,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
async def test_backfill_dry_run_reports_exact_subset(
    neo4j_settings: Settings,
    neo4j_driver: Any,
    total_chunks: int,
    candidate_indices: set[int],
) -> None:
    """Dry-run candidates are exactly the chunks that have MENTIONS edges."""
    # Clamp generated indices to the actual range of chunks that will exist.
    expected = sorted(candidate_indices & set(range(total_chunks)))
    source_id = f"prop:backfill-{total_chunks}-{len(expected)}-{uuid.uuid4().hex[:8]}"

    await _seed_chunks(neo4j_driver, source_id, set(expected))

    use_case = BackfillCheckpointsUseCase(
        checkpoint_port=Neo4jCheckpointAdapter(neo4j_settings),
        graph_db_port=Neo4jCommandAdapter(neo4j_settings),
        versions=_V,
        run_id=f"dry-run-{source_id}",
        evidence_dir=Path("/tmp/backfill-evidence"),
    )
    try:
        report = await use_case.execute(source_id, apply=False)
    finally:
        await use_case.close()

    try:
        assert report.dry_run is True
        assert report.source_id == source_id
        assert report.candidate_chunk_indices == expected
        assert report.processed_count == 0
        assert report.current_versions == _V
        assert report.approval_path is None
        assert report.evidence_bundle_path is None

        # The critical safety property: dry-run must not mutate checkpoints.
        assert await _count_checkpoints(neo4j_driver, source_id) == 0
    finally:
        await _cleanup_source(neo4j_driver, source_id)

