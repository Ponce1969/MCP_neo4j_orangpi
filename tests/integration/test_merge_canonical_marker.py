"""Integration tests for the canonical leftover-marker rule (T7 finding).

Production shape: three rounds of round-robin merges left both members of a
pair carrying ``merged_into`` (mutual cycle). Approved semantics
(maintainer decision 2026-10-02): the canonical of a merge is the winner and
must end up live, so ``apply_merge`` removes the canonical's own leftover
``merged_into``/``merged_at`` in the same transaction. The declared limit:
``rollback_merge`` removes the candidates' markers but does NOT restore the
canonical's prior marker (the ledger does not record it).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from book_graph_rag.config import Settings
from book_graph_rag.domain.merge_ledger_models import (
    MergeBand,
    MergeLedgerEntry,
)
from book_graph_rag.domain.merged_endpoint_resolution import find_cycles
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

_LONG = "book:ch1:large-language-model-component"
_SHORT = "book:ch1:llm-component"
_CANDIDATE = "book:ch1:duplicate"
_OTHER_CANON = "book:ch1:other-canonical"
_OTHER_DUP = "book:ch1:other-duplicate"
_CONCEPT = "book:ch1:concept-x"


def _entity(entity_id: str, name: str, type_: str, aliases: list[str] | None = None) -> Entity:
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


async def _seed_mutual_pair(adapter: Neo4jCommandAdapter, driver: Any) -> None:
    """Seed the production shape: a mutual merge pair + a live second canonical.

    - ``_LONG`` and ``_SHORT`` point at each other (round-robin leftover).
    - ``_OTHER_CANON`` is a live canonical whose own candidate is soft-deleted.
    - ``_CANDIDATE`` carries MENTIONS + RELATED edges for the merge under test.
    """
    await adapter.upsert_entities(
        [
            _entity(_LONG, "LLM Component", "concept", ["Long Form"]),
            _entity(_SHORT, "LLM Comp", "concept", ["Short Alias"]),
            _entity(_CANDIDATE, "Duplicate Agent", "agent", ["Dup"]),
            _entity(_OTHER_CANON, "Other Canonical", "agent"),
            _entity(_OTHER_DUP, "Other Duplicate", "agent", ["Other Dup Alias"]),
            _entity(_CONCEPT, "Concept X", "concept"),
        ]
    )
    async with driver.session() as session:
        # Mutual pair left by three rounds of round-robin merges.
        await session.run(
            """
            MATCH (a:Entity {id: $long}), (b:Entity {id: $short})
            SET a.merged_into = $short, a.merged_at = datetime(),
                b.merged_into = $long, b.merged_at = datetime()
            """,
            long=_LONG,
            short=_SHORT,
        )
        # A second, already-live canonical with its own soft-deleted candidate.
        await session.run(
            """
            MATCH (d:Entity {id: $dup}), (c:Entity {id: $canon})
            SET d.merged_into = $canon, d.merged_at = datetime()
            """,
            dup=_OTHER_DUP,
            canon=_OTHER_CANON,
        )
        # Candidate edges: MENTIONS from a chunk and RELATED to an outside entity.
        await session.run(
            """
            MERGE (k:Chunk {chunk_index: 0, source_id: 'book:ch1'})
            SET k.text = 'text'
            MERGE (e:Entity {id: $dup_id})
            MERGE (k)-[m:MENTIONS {source_page: 7}]->(e)
            """,
            dup_id=_CANDIDATE,
        )
        await session.run(
            """
            MATCH (dup:Entity {id: $dup}), (concept:Entity {id: $concept})
            MERGE (dup)-[r:RELATED {type: 'requires', source_page: 8}]->(concept)
            """,
            dup=_CANDIDATE,
            concept=_CONCEPT,
        )


async def _read_merged_map(driver: Any) -> dict[str, str]:
    """Read the full ``merged_into`` map from the graph (ad-hoc read Cypher)."""
    async with driver.session() as session:
        result = await session.run(
            """
            MATCH (n:Entity) WHERE n.merged_into IS NOT NULL
            RETURN n.id AS id, n.merged_into AS target
            """
        )
        return {record["id"]: record["target"] async for record in result}


async def _read_marker(driver: Any, entity_id: str) -> tuple[Any, Any]:
    """Return ``(merged_into, merged_at)`` for one entity."""
    async with driver.session() as session:
        result = await session.run(
            """
            MATCH (n:Entity {id: $id})
            RETURN n.merged_into AS merged_into, n.merged_at AS merged_at
            """,
            id=entity_id,
        )
        record = await result.single()
        assert record is not None, f"entity {entity_id} missing from graph"
        return record["merged_into"], record["merged_at"]


@pytest.mark.neo4j_integration
async def test_apply_merge_clears_canonical_leftover_marker(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """The canonical ends up live; no cycle remains around it (T7 finding)."""
    command = Neo4jCommandAdapter(neo4j_settings)
    merge_adapter = Neo4jGraphMergeAdapter(neo4j_driver)
    try:
        await _seed_mutual_pair(command, neo4j_driver)
        map_before = await _read_merged_map(neo4j_driver)
        assert find_cycles(map_before) != (), (
            "seed must reproduce the production mutual pair before the merge"
        )

        inverse = await merge_adapter.capture_inverse_mapping([_CANDIDATE])
        await merge_adapter.apply_merge(
            canonical_id=_LONG,
            candidate_ids=[_CANDIDATE],
            aliases_folded=[],
            inverse_mapping=inverse,
        )

        # Canonical is the winner: its own leftover marker is gone.
        canon_merged_into, canon_merged_at = await _read_marker(neo4j_driver, _LONG)
        assert canon_merged_into is None, (
            f"canonical must be live, still carries merged_into={canon_merged_into!r}"
        )
        assert canon_merged_at is None

        # Candidate points at the now-live canonical.
        cand_merged_into, _ = await _read_marker(neo4j_driver, _CANDIDATE)
        assert cand_merged_into == _LONG

        # No cycle remains around the canonical in the merged map read back.
        map_after = await _read_merged_map(neo4j_driver)
        cycles_after = find_cycles(map_after)
        assert all(_LONG not in cycle for cycle in cycles_after), (
            f"cycle still involves the canonical: {cycles_after}"
        )

        # No other entity's marker changes: the second live canonical and its
        # candidate keep exactly what they had.
        assert _OTHER_CANON not in map_after
        other_dup_merged_into, _ = await _read_marker(neo4j_driver, _OTHER_DUP)
        assert other_dup_merged_into == _OTHER_CANON
        # The short alias was never the merge's canonical: still soft-deleted,
        # now pointing at a live node (its marker is not this merge's to clear).
        short_merged_into, _ = await _read_marker(neo4j_driver, _SHORT)
        assert short_merged_into == _LONG

        changed = {
            key: (map_before.get(key), map_after.get(key))
            for key in set(map_before) | set(map_after)
            if map_before.get(key) != map_after.get(key)
        }
        assert changed == {_LONG: (_SHORT, None), _CANDIDATE: (None, _LONG)}, (
            f"unexpected marker changes: {changed}"
        )
    finally:
        await command.close()


@pytest.mark.neo4j_integration
async def test_rollback_merge_does_not_restore_canonical_prior_marker(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """Declared limit: rollback drops the candidate marker but never restores
    the canonical's prior ``merged_into`` — the ledger does not record it."""
    command = Neo4jCommandAdapter(neo4j_settings)
    merge_adapter = Neo4jGraphMergeAdapter(neo4j_driver)
    try:
        await _seed_mutual_pair(command, neo4j_driver)
        inverse = await merge_adapter.capture_inverse_mapping([_CANDIDATE])
        await merge_adapter.apply_merge(
            canonical_id=_LONG,
            candidate_ids=[_CANDIDATE],
            aliases_folded=[],
            inverse_mapping=inverse,
        )

        entry = MergeLedgerEntry(
            seq=1,
            candidate_ids=[_CANDIDATE],
            canonical_id=_LONG,
            band=MergeBand.HIGH,
            evidence=[_dummy_evidence(_LONG, _CANDIDATE)],
            aliases_folded=[],
            edge_inverse_map=inverse.edge_inverse_map,
            approver="test",
            applied_at=datetime.now(UTC),
        )
        await merge_adapter.rollback_merge(entry)

        # Candidate marker removed by the rollback.
        cand_merged_into, cand_merged_at = await _read_marker(neo4j_driver, _CANDIDATE)
        assert cand_merged_into is None
        assert cand_merged_at is None

        # Declared limit, asserted as-is: the canonical's prior marker
        # (merged_into = _SHORT before the merge) is NOT restored.
        canon_merged_into, canon_merged_at = await _read_marker(neo4j_driver, _LONG)
        assert canon_merged_into is None, (
            "known limit: rollback does not record/restore the canonical's "
            f"prior marker, found {canon_merged_into!r}"
        )
        assert canon_merged_at is None
    finally:
        await command.close()
