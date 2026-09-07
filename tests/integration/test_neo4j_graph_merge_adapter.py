"""Testcontainers integration tests for Neo4jGraphMergeAdapter.

Covers inverse-mapping capture, atomic merge, alias folding, edge re-pointing,
and rollback restoration.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import neo4j
import pytest

from book_graph_rag.config import Settings
from book_graph_rag.domain.merge_ledger_models import (
    FoldedAlias,
    MergeBand,
    MergeLedgerEntry,
)
from book_graph_rag.domain.models import Entity
from book_graph_rag.domain.resolution_models import (
    ConfidenceBand,
    ResolutionEvidence,
    S0NormalizedForm,
    S1EmbeddingSignal,
    S2TypeGateResult,
    S3ContextSignals,
)
from book_graph_rag.infrastructure.neo4j_command_adapter import Neo4jCommandAdapter
from book_graph_rag.infrastructure.neo4j_graph_merge_adapter import (
    Neo4jGraphMergeAdapter,
)


def _entity(
    entity_id: str, name: str, type_: str, aliases: list[str] | None = None
) -> Entity:
    return Entity(
        id=entity_id,
        name=name,
        type=type_,  # type: ignore[arg-type]
        aliases=aliases or [],
        description=f"desc {name}",
    )


def _dummy_evidence(anchor_id: str, candidate_id: str) -> ResolutionEvidence:
    now = datetime.now(UTC)
    return ResolutionEvidence(
        anchor_id=anchor_id,
        candidate_id=candidate_id,
        anchor_type="agent",
        candidate_type="agent",
        anchor_namespace="book:ch1",
        candidate_namespace="book:ch1",
        anchor_normalized=S0NormalizedForm(
            original="a", nfkc="a", casefold="a", compact="a", tokens=("a",)
        ),
        candidate_normalized=S0NormalizedForm(
            original="b", nfkc="b", casefold="b", compact="b", tokens=("b",)
        ),
        s0_matched_field="none",
        s1=S1EmbeddingSignal(
            cosine_similarity=0.95,
            candidate_rank=1,
            input_variant="A",
            model_id="fake",
        ),
        s2=S2TypeGateResult(
            anchor_type="agent", candidate_type="agent", passed=True, reason="same"
        ),
        s3=S3ContextSignals(
            mentions_jaccard=0.5,
            related_jaccard=0.5,
            description_overlap=0.5,
            mentions_source_count=2,
            mentions_shared_count=1,
            related_neighbor_count=2,
            related_shared_count=1,
            conflict_flag=False,
        ),
        composite_score=0.5,
        band=ConfidenceBand.HIGH,
        cross_namespace=False,
        cross_type=False,
        decided_at=now,
    )


async def _seed_with_edges(adapter: Neo4jCommandAdapter, driver: Any) -> None:
    await adapter.upsert_entities(
        [
            _entity("book:ch1:canonical", "Canonical Agent", "agent", ["Canon"]),
            _entity("book:ch1:duplicate", "Duplicate Agent", "agent", ["Dup", "Duppy"]),
            _entity("book:ch1:concept-x", "Concept X", "concept"),
        ]
    )
    async with driver.session() as session:
        # Chunk mentions duplicate.
        await session.run(
            """
            MERGE (k:Chunk {chunk_index: 0, source_id: $source_id})
            SET k.text = 'text'
            MERGE (e:Entity {id: $dup_id})
            MERGE (k)-[m:MENTIONS {source_page: 7}]->(e)
            """,
            source_id="book:ch1",
            dup_id="book:ch1:duplicate",
        )
        # Duplicate related to concept.
        await session.run(
            """
            MATCH (dup:Entity {id: $dup_id}), (concept:Entity {id: $concept_id})
            MERGE (dup)-[r:RELATED {type: 'requires', source_page: 8}]->(concept)
            """,
            dup_id="book:ch1:duplicate",
            concept_id="book:ch1:concept-x",
        )


@pytest.mark.neo4j_integration
async def test_capture_inverse_mapping_records_pre_merge_state(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """capture_inverse_mapping snapshots aliases and edge endpoints before mutation."""
    command = Neo4jCommandAdapter(neo4j_settings)
    merge_adapter = Neo4jGraphMergeAdapter(neo4j_driver)
    try:
        await _seed_with_edges(command, neo4j_driver)

        inverse = await merge_adapter.capture_inverse_mapping(["book:ch1:duplicate"])
        assert inverse.aliases_before["book:ch1:duplicate"] == ("Dup", "Duppy")

        kinds = {e.edge_kind for e in inverse.edge_inverse_map}
        assert kinds == {"MENTIONS", "RELATED"}

        mentions = next(
            e for e in inverse.edge_inverse_map if e.edge_kind == "MENTIONS"
        )
        assert mentions.duplicate_entity_id == "book:ch1:duplicate"
        assert mentions.original_other_endpoint_id.startswith("book:ch1:chunk")
        assert mentions.edge_properties.get("source_page") == 7

        related = next(
            e for e in inverse.edge_inverse_map if e.edge_kind == "RELATED"
        )
        assert related.original_other_endpoint_id == "book:ch1:concept-x"
    finally:
        await command.close()


@pytest.mark.neo4j_integration
async def test_apply_merge_soft_deletes_and_repoints_edges(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """After apply_merge, duplicate is soft-deleted and edges point to canonical."""
    command = Neo4jCommandAdapter(neo4j_settings)
    merge_adapter = Neo4jGraphMergeAdapter(neo4j_driver)
    try:
        await _seed_with_edges(command, neo4j_driver)

        inverse = await merge_adapter.capture_inverse_mapping(["book:ch1:duplicate"])
        aliases_folded = [
            FoldedAlias(from_entity_id="book:ch1:duplicate", alias_value="Dup"),
            FoldedAlias(from_entity_id="book:ch1:duplicate", alias_value="Duppy"),
        ]
        await merge_adapter.apply_merge(
            canonical_id="book:ch1:canonical",
            candidate_ids=["book:ch1:duplicate"],
            aliases_folded=aliases_folded,
            inverse_mapping=inverse,
        )

        async with neo4j_driver.session() as session:
            dup = await session.run(
                "MATCH (n:Entity {id: $id}) RETURN n.merged_into AS merged",
                id="book:ch1:duplicate",
            )
            record = await dup.single()
            assert record is not None
            assert record["merged"] == "book:ch1:canonical"

            canonical = await session.run(
                "MATCH (n:Entity {id: $id}) RETURN n.aliases AS aliases",
                id="book:ch1:canonical",
            )
            canon_record = await canonical.single()
            assert canon_record is not None
            assert "Dup" in canon_record["aliases"]
            assert "Duppy" in canon_record["aliases"]

            mentions = await session.run(
                """
                MATCH (k:Chunk)-[m:MENTIONS]->(n:Entity {id: $id})
                RETURN count(m) AS c
                """,
                id="book:ch1:canonical",
            )
            assert (await mentions.single())["c"] == 1

            related = await session.run(
                """
                MATCH (n:Entity {id: $id})-[r:RELATED]->(x:Entity {id: $concept})
                RETURN count(r) AS c
                """,
                id="book:ch1:canonical",
                concept="book:ch1:concept-x",
            )
            assert (await related.single())["c"] == 1
    finally:
        await command.close()


@pytest.mark.neo4j_integration
async def test_apply_merge_is_atomic_on_error(
    neo4j_settings: Settings,
    neo4j_driver: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure inside the merge transaction leaves the graph unchanged."""
    command = Neo4jCommandAdapter(neo4j_settings)
    merge_adapter = Neo4jGraphMergeAdapter(neo4j_driver)
    try:
        await _seed_with_edges(command, neo4j_driver)
        inverse = await merge_adapter.capture_inverse_mapping(["book:ch1:duplicate"])
        aliases_folded = [
            FoldedAlias(from_entity_id="book:ch1:duplicate", alias_value="Dup"),
        ]

        # Inject an invalid Cypher fragment mid-transaction to force rollback.
        from book_graph_rag.infrastructure import neo4j_graph_merge_adapter as cypher_module

        original = cypher_module._MARK_MERGED_INTO
        monkeypatch.setattr(
            cypher_module, "_MARK_MERGED_INTO", "RETURN 1 / $zero"
        )

        from book_graph_rag.domain.resolution_errors import ResolutionError

        with pytest.raises(ResolutionError) as exc_info:  # noqa: B017
            await merge_adapter.apply_merge(
                canonical_id="book:ch1:canonical",
                candidate_ids=["book:ch1:duplicate"],
                aliases_folded=aliases_folded,
                inverse_mapping=inverse,
            )

        # The adapter must wrap the driver error, not mask it.
        assert isinstance(exc_info.value.__cause__, neo4j.exceptions.Neo4jError)

        monkeypatch.setattr(cypher_module, "_MARK_MERGED_INTO", original)

        async with neo4j_driver.session() as session:
            dup = await session.run(
                "MATCH (n:Entity {id: $id}) RETURN n.merged_into AS merged",
                id="book:ch1:duplicate",
            )
            record = await dup.single()
            assert record is None or record["merged"] is None or record["merged"] == ""

            canon = await session.run(
                "MATCH (n:Entity {id: $id}) RETURN n.aliases AS aliases",
                id="book:ch1:canonical",
            )
            canon_record = await canon.single()
            assert canon_record is not None
            assert "Dup" not in (canon_record["aliases"] or [])
    finally:
        await command.close()


@pytest.mark.neo4j_integration
async def test_rollback_merge_restores_pre_merge_state(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """rollback_merge removes merged_into, restores edges, and removes aliases."""
    command = Neo4jCommandAdapter(neo4j_settings)
    merge_adapter = Neo4jGraphMergeAdapter(neo4j_driver)
    try:
        await _seed_with_edges(command, neo4j_driver)
        inverse = await merge_adapter.capture_inverse_mapping(["book:ch1:duplicate"])
        aliases_folded = [
            FoldedAlias(from_entity_id="book:ch1:duplicate", alias_value="Dup"),
            FoldedAlias(from_entity_id="book:ch1:duplicate", alias_value="Duppy"),
        ]
        await merge_adapter.apply_merge(
            canonical_id="book:ch1:canonical",
            candidate_ids=["book:ch1:duplicate"],
            aliases_folded=aliases_folded,
            inverse_mapping=inverse,
        )

        entry = MergeLedgerEntry(
            seq=1,
            candidate_ids=["book:ch1:duplicate"],
            canonical_id="book:ch1:canonical",
            band=MergeBand.HIGH,
            evidence=[_dummy_evidence("book:ch1:canonical", "book:ch1:duplicate")],
            aliases_folded=aliases_folded,
            edge_inverse_map=inverse.edge_inverse_map,
            approver="test",
            applied_at=datetime.now(UTC),
        )

        await merge_adapter.rollback_merge(entry)

        async with neo4j_driver.session() as session:
            dup = await session.run(
                "MATCH (n:Entity {id: $id}) RETURN n.merged_into AS merged",
                id="book:ch1:duplicate",
            )
            record = await dup.single()
            assert record is None or record["merged"] is None or record["merged"] == ""

            canon = await session.run(
                "MATCH (n:Entity {id: $id}) RETURN n.aliases AS aliases",
                id="book:ch1:canonical",
            )
            canon_record = await canon.single()
            assert canon_record is not None
            assert "Dup" not in (canon_record["aliases"] or [])
            assert "Duppy" not in (canon_record["aliases"] or [])

            mentions = await session.run(
                """
                MATCH (k:Chunk)-[m:MENTIONS]->(n:Entity {id: $id})
                RETURN count(m) AS c
                """,
                id="book:ch1:duplicate",
            )
            assert (await mentions.single())["c"] == 1

            related = await session.run(
                """
                MATCH (n:Entity {id: $id})-[r:RELATED]->(x:Entity {id: $concept})
                RETURN count(r) AS c
                """,
                id="book:ch1:duplicate",
                concept="book:ch1:concept-x",
            )
            assert (await related.single())["c"] == 1
    finally:
        await command.close()


@pytest.mark.neo4j_integration
async def test_apply_merge_is_idempotent(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """Running the same merge twice does not duplicate aliases or edges."""
    command = Neo4jCommandAdapter(neo4j_settings)
    merge_adapter = Neo4jGraphMergeAdapter(neo4j_driver)
    try:
        await _seed_with_edges(command, neo4j_driver)
        inverse = await merge_adapter.capture_inverse_mapping(["book:ch1:duplicate"])
        aliases_folded = [
            FoldedAlias(from_entity_id="book:ch1:duplicate", alias_value="Dup"),
        ]
        for _ in range(2):
            await merge_adapter.apply_merge(
                canonical_id="book:ch1:canonical",
                candidate_ids=["book:ch1:duplicate"],
                aliases_folded=aliases_folded,
                inverse_mapping=inverse,
            )

        async with neo4j_driver.session() as session:
            aliases_result = await session.run(
                "MATCH (n:Entity {id: $id}) RETURN n.aliases AS aliases",
                id="book:ch1:canonical",
            )
            aliases = (await aliases_result.single())["aliases"] or []
            assert aliases.count("Dup") == 1

            related_count = await session.run(
                """
                MATCH (n:Entity {id: $id})-[r:RELATED]->(x:Entity {id: $concept})
                RETURN count(r) AS c
                """,
                id="book:ch1:canonical",
                concept="book:ch1:concept-x",
            )
            assert (await related_count.single())["c"] == 1
    finally:
        await command.close()
