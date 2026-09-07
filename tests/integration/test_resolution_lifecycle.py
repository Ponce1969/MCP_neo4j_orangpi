"""Full lifecycle integration test for semantic entity resolution.

Seeds a testcontainers Neo4j with three entity pairs (exact, high, medium),
runs the hybrid ``ResolveEntitiesUseCase``, approves the high-band quarantine
record, verifies idempotence, then rolls back the applied merge.

The embedding provider is fake/deterministic and retrieval is brute-force over
a precomputed cache; neighborhood queries and merge/rollback hit the real
Neo4j container.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from book_graph_rag.application.apply_merge_use_case import ApplyMergeUseCase
from book_graph_rag.application.approve_quarantine_use_case import (
    ApproveQuarantineUseCase,
)
from book_graph_rag.application.resolve_entities_use_case import ResolveEntitiesUseCase
from book_graph_rag.application.rollback_merge_use_case import RollbackMergeUseCase
from book_graph_rag.config import Settings
from book_graph_rag.domain.models import Entity
from book_graph_rag.domain.resolution_models import ConfidenceBand
from book_graph_rag.domain.s4_band_assignment import BandThresholds
from book_graph_rag.infrastructure.brute_force_candidate_retrieval import (
    BruteForceCandidateRetrieval,
)
from book_graph_rag.infrastructure.jsonl_merge_ledger import JSONLMergeLedger
from book_graph_rag.infrastructure.jsonl_quarantine_writer import JSONLQuarantineWriter
from book_graph_rag.infrastructure.neo4j_command_adapter import Neo4jCommandAdapter
from book_graph_rag.infrastructure.neo4j_graph_merge_adapter import (
    Neo4jGraphMergeAdapter,
)
from book_graph_rag.infrastructure.neo4j_neighborhood_query_adapter import (
    Neo4jNeighborhoodQueryAdapter,
)
from book_graph_rag.ports.embedding_provider_port import (
    EmbeddingBatch,
    EmbeddingProviderPort,
    EmbeddingRequest,
    EmbeddingVector,
)

# ── Deterministic fake embedding provider ─────────────────────────────────────


class _FakeEmbeddingProvider(EmbeddingProviderPort):
    """Maps fixed texts to unit vectors with controlled pairwise cosine."""

    def __init__(self) -> None:
        self._dim = 4

    async def embed(self, request: EmbeddingRequest) -> EmbeddingBatch:
        return EmbeddingBatch(
            model_id=request.model_id,
            vectors=[
                EmbeddingVector(values=(0.0,) * self._dim, model_id=request.model_id)
                for _ in request.texts
            ],
        )

    def model_dim(self, model_id: str) -> int:
        return self._dim


# ── Entity fixture helper ─────────────────────────────────────────────────────


def _entity(
    entity_id: str,
    name: str,
    entity_type: str,
    aliases: tuple[str, ...] = (),
    description: str = "",
) -> Entity:
    return Entity(
        id=entity_id,
        name=name,
        type=entity_type,  # type: ignore[arg-type]
        aliases=list(aliases),
        description=description,
    )


# ── Graph seeding ─────────────────────────────────────────────────────────────


async def _seed_graph(
    driver: Any, command: Neo4jCommandAdapter
) -> tuple[str, str, str, str, str, str]:
    """Create three entity pairs plus a shared neighbor and two source chunks."""
    exact_anchor_id = "book:ch1:lang-graph"
    exact_dup_id = "book:ch1:langgraph"
    high_anchor_id = "book:ch1:semantic-kernel"
    high_dup_id = "book:ch1:semantic-kernel-alt"
    medium_anchor_id = "book:ch1:autogen-framework"
    medium_dup_id = "book:ch1:autogen-multiagent"
    shared_neighbor_id = "book:ch1:shared-neighbor"

    entities = [
        _entity(exact_anchor_id, "Lang Graph", "framework"),
        _entity(exact_dup_id, "LangGraph", "framework", aliases=("Lang Graph",)),
        _entity(
            high_anchor_id,
            "Semantic Kernel",
            "framework",
            description="Microsoft semantic AI SDK",
        ),
        _entity(
            high_dup_id,
            "Semantic-Kernel Framework",
            "framework",
            aliases=("SK",),
            description="Microsoft semantic kernel SDK",
        ),
        _entity(
            medium_anchor_id,
            "AutoGen Framework",
            "framework",
            description="Microsoft multi-agent framework",
        ),
        _entity(
            medium_dup_id,
            "AutoGen Multi-Agent",
            "framework",
            description="Autogen conversational agents",
        ),
        _entity(shared_neighbor_id, "Shared Neighbor", "concept"),
    ]
    await command.upsert_entities(entities)

    async with driver.session() as session:
        await session.run(
            """
            MERGE (c1:Chunk {source_id: $source_id, chunk_index: 1})
            SET c1.text = 'high pair context'
            MERGE (c2:Chunk {source_id: $source_id, chunk_index: 2})
            SET c2.text = 'medium pair context'
            """,
            source_id="book:ch1",
        )

        # HIGH pair shares a mention source and a related neighbor.
        await session.run(
            """
            MATCH (c1:Chunk {source_id: $source_id, chunk_index: 1})
            MATCH (ha:Entity {id: $high_anchor}), (hd:Entity {id: $high_dup})
            MERGE (c1)-[:MENTIONS]->(ha)
            MERGE (c1)-[:MENTIONS]->(hd)
            """,
            source_id="book:ch1",
            high_anchor=high_anchor_id,
            high_dup=high_dup_id,
        )
        await session.run(
            """
            MATCH (ha:Entity {id: $high_anchor}), (hd:Entity {id: $high_dup}),
                  (n:Entity {id: $neighbor})
            MERGE (ha)-[:RELATED {type: 'requires'}]->(n)
            MERGE (hd)-[:RELATED {type: 'requires'}]->(n)
            """,
            high_anchor=high_anchor_id,
            high_dup=high_dup_id,
            neighbor=shared_neighbor_id,
        )

        # MEDIUM pair uses disjoint mention sources and disjoint related neighbors.
        await session.run(
            """
            MATCH (c2:Chunk {source_id: $source_id, chunk_index: 2})
            MATCH (ma:Entity {id: $medium_anchor})
            MERGE (c2)-[:MENTIONS]->(ma)
            """,
            source_id="book:ch1",
            medium_anchor=medium_anchor_id,
        )
        await session.run(
            """
            MATCH (md:Entity {id: $medium_dup}), (n:Entity {id: $neighbor})
            MERGE (md)-[:RELATED {type: 'requires'}]->(n)
            """,
            medium_dup=medium_dup_id,
            neighbor=shared_neighbor_id,
        )

    return (
        exact_anchor_id,
        exact_dup_id,
        high_anchor_id,
        high_dup_id,
        medium_anchor_id,
        medium_dup_id,
    )


async def _build_retrieval() -> BruteForceCandidateRetrieval:
    """Populate brute-force cache with deterministic, pair-specific vectors."""
    retrieval = BruteForceCandidateRetrieval()

    # Unit vectors chosen so each pair has the intended cosine and pairs are
    # mutually orthogonal (no cross-pair retrieval above the threshold).
    high_dup_component = 0.31224989991992  # sqrt(1 - 0.95^2)
    medium_dup_component = 0.52678268764257  # sqrt(1 - 0.85^2)

    embeddings: list[tuple[str, tuple[float, ...], str, str]] = [
        ("book:ch1:lang-graph", (0.0, 0.0, 1.0, 0.0), "framework", "book:ch1"),
        ("book:ch1:langgraph", (0.0, 0.0, 1.0, 0.0), "framework", "book:ch1"),
        ("book:ch1:semantic-kernel", (1.0, 0.0, 0.0, 0.0), "framework", "book:ch1"),
        (
            "book:ch1:semantic-kernel-alt",
            (0.95, high_dup_component, 0.0, 0.0),
            "framework",
            "book:ch1",
        ),
        ("book:ch1:autogen-framework", (0.0, 1.0, 0.0, 0.0), "framework", "book:ch1"),
        (
            "book:ch1:autogen-multiagent",
            (0.0, 0.85, medium_dup_component, 0.0),
            "framework",
            "book:ch1",
        ),
    ]

    for entity_id, values, entity_type, namespace in embeddings:
        await retrieval.upsert_entity_embedding(
            entity_id,
            EmbeddingVector(values=values, model_id="fake"),
        )
        retrieval.upsert_entity_metadata(entity_id, entity_type, namespace)  # type: ignore[arg-type]

    return retrieval


def _build_thresholds() -> BandThresholds:
    return BandThresholds(
        high_cosine=0.90,
        high_context=0.50,
        medium_cosine=0.80,
        conflict_floor=0.10,
    )


# ── Lifecycle test ────────────────────────────────────────────────────────────


@pytest.mark.neo4j_integration
async def test_resolution_lifecycle_analyze_approve_idempotence_rollback(
    neo4j_settings: Settings,
    neo4j_driver: Any,
    tmp_path: Path,
) -> None:
    """End-to-end: exact auto-merge, high/medium quarantine, approve, rollback."""
    command = Neo4jCommandAdapter(neo4j_settings)
    graph_merge = Neo4jGraphMergeAdapter(neo4j_driver)

    try:
        (
            exact_anchor_id,
            exact_dup_id,
            high_anchor_id,
            high_dup_id,
            medium_anchor_id,
            medium_dup_id,
        ) = await _seed_graph(neo4j_driver, command)

        retrieval = await _build_retrieval()
        quarantine_path = tmp_path / "quarantine.jsonl"
        ledger_path = tmp_path / "merge_ledger.jsonl"
        quarantine_writer = JSONLQuarantineWriter(quarantine_path)

        resolve_use_case = ResolveEntitiesUseCase(
            retrieval=retrieval,
            embedding=_FakeEmbeddingProvider(),
            entity_loader=command,
            neighborhood=Neo4jNeighborhoodQueryAdapter(neo4j_driver),
            quarantine_writer=quarantine_writer,
            thresholds=_build_thresholds(),
            input_variant="A",
            model_id="fake",
            top_k=10,
            min_similarity=0.60,
        )

        # 1. Dry-run analysis: exact → auto-merge, high/medium → quarantine.
        dry_result = await resolve_use_case.analyze(dry_run=True)

        exact_groups = [
            g for g in dry_result.auto_merge_groups if exact_dup_id in g.duplicate_ids
        ]
        assert len(exact_groups) == 1, "EXACT pair must be staged for auto-merge"
        assert exact_groups[0].band == ConfidenceBand.EXACT

        high_records = [
            r for r in dry_result.quarantine_records if r.candidate_id == high_dup_id
        ]
        assert len(high_records) == 1, "HIGH pair must be quarantined"
        assert high_records[0].band == ConfidenceBand.HIGH

        medium_records = [
            r for r in dry_result.quarantine_records if r.candidate_id == medium_dup_id
        ]
        assert len(medium_records) == 1, "MEDIUM pair must be quarantined"
        assert medium_records[0].band == ConfidenceBand.MEDIUM

        # 2. Persist quarantine records so the approval step can load the high one.
        await resolve_use_case.analyze(dry_run=False)

        # 3. Approve the high-band record and apply the merge.
        ledger = JSONLMergeLedger(ledger_path)
        apply_use_case = ApplyMergeUseCase(
            graph_merge=graph_merge,
            ledger=ledger,
            entity_loader=command,
        )
        approve_use_case = ApproveQuarantineUseCase(
            quarantine=quarantine_writer,
            apply=apply_use_case,
        )
        entry = await approve_use_case.approve(
            seq=high_records[0].seq,
            reviewer="human",
            approver_for_apply="human",
        )

        assert entry.seq == 1
        assert ledger.read_all()[-1].seq == entry.seq

        async with neo4j_driver.session() as session:
            result = await session.run(
                "MATCH (n:Entity {id: $id}) RETURN n.merged_into AS merged",
                id=high_dup_id,
            )
            record = await result.single()
            assert record is not None
            assert record["merged"] == high_anchor_id

        # 4. Re-run analysis: the merged high duplicate is invisible.
        rerun = await resolve_use_case.analyze(dry_run=True)
        rerun_candidate_ids = {
            cid for g in rerun.auto_merge_groups for cid in g.duplicate_ids
        } | {r.candidate_id for r in rerun.quarantine_records}
        assert high_dup_id not in rerun_candidate_ids, (
            "merged duplicate must not reappear in analysis"
        )
        assert any(g.band == ConfidenceBand.EXACT for g in rerun.auto_merge_groups)
        assert any(
            r.candidate_id == medium_dup_id for r in rerun.quarantine_records
        )

        # 5. Rollback the applied merge.
        rollback_use_case = RollbackMergeUseCase(
            ledger=ledger,
            graph_merge=graph_merge,
        )
        await rollback_use_case.rollback(seq=entry.seq)

        async with neo4j_driver.session() as session:
            result = await session.run(
                "MATCH (n:Entity {id: $id}) RETURN n.merged_into AS merged",
                id=high_dup_id,
            )
            record = await result.single()
            assert record is None or record["merged"] is None or record["merged"] == ""

            mentions = await session.run(
                """
                MATCH (c:Chunk)-[:MENTIONS]->(n:Entity {id: $id})
                RETURN count(*) AS c
                """,
                id=high_dup_id,
            )
            assert (await mentions.single())["c"] == 1

            related = await session.run(
                """
                MATCH (n:Entity {id: $id})-[:RELATED]->(:Entity)
                RETURN count(*) AS c
                """,
                id=high_dup_id,
            )
            assert (await related.single())["c"] == 1

        all_entries = ledger.read_all()
        assert len(all_entries) == 2
        compensating = all_entries[-1]
        assert compensating.rollback_of == entry.seq
        assert compensating.prev_seq_sha256 == entry.entry_sha256
    finally:
        await command.close()
