"""Regression tests for the Neo4j audit adapter against soft-deleted entities.

Phase 3 entity resolution soft-deletes duplicate ``Entity`` nodes by setting a
non-empty ``merged_into`` property (never ``DETACH DELETE``). Every entity-level
audit rule must treat those nodes as inactive; otherwise any production merge
produces false positives (``DUPLICATE_ENTITY_LOGICAL``, ``ENTITY_UNMENTIONED``,
``ENTITY_ISOLATED_RELATED``, ``PROVENANCE_ENTITY_MISSING``).

These are testcontainers integration tests and never touch production.
"""

from __future__ import annotations

from typing import Any

import pytest

from book_graph_rag.application.audit_graph_use_case import build_audit_target
from book_graph_rag.config import Settings
from book_graph_rag.domain.audit_models import AuditSnapshot
from book_graph_rag.infrastructure.neo4j_audit_adapter import (
    QUERY_PLAN,
    Neo4jAuditAdapter,
)

_ENTITY_RULES = (
    "DUPLICATE_ENTITY_LOGICAL",
    "ENTITY_UNMENTIONED",
    "ENTITY_ISOLATED_RELATED",
    "PROVENANCE_ENTITY_MISSING",
)

_CANON_ID = "book:ch1:canonical-agent"
_DUP_ID = "book:ch1:duplicate-agent"
_OTHER_ID = "book:ch1:other-concept"
_CHUNK_ID = "book:ch1:chunk-0"


def _finding_total(snapshot: AuditSnapshot, rule_id: str) -> int:
    return int(next(f.total for f in snapshot.findings if f.rule_id == rule_id))


def _subject_ids(snapshot: AuditSnapshot, rule_id: str) -> set[str]:
    finding = next(f for f in snapshot.findings if f.rule_id == rule_id)
    return {sid for sample in finding.samples for sid in sample.subject_ids}


async def _seed_clean_canonical_plus_soft_deleted_duplicate(driver: Any) -> None:
    """Seed a fully-clean canonical entity plus a soft-deleted same-name duplicate."""
    async with driver.session() as session:
        await session.run(
            """
            MERGE (canon:Entity {id: $canon_id})
            SET canon.name = 'Agent Smith', canon.type = 'agent', canon.source_page = 12
            """,
            canon_id=_CANON_ID,
        )
        await session.run(
            """
            MERGE (dup:Entity {id: $dup_id})
            SET dup.name = 'Agent Smith', dup.type = 'agent',
                dup.merged_into = $canon_id, dup.merged_at = datetime()
            """,
            dup_id=_DUP_ID,
            canon_id=_CANON_ID,
        )
        await session.run(
            """
            MERGE (other:Entity {id: $other_id})
            SET other.name = 'The Matrix', other.type = 'concept', other.source_page = 13
            """,
            other_id=_OTHER_ID,
        )
        await session.run(
            """
            MERGE (k:Chunk {id: $chunk_id})
            SET k.book_id = 'book:ch1', k.chunk_index = 0,
                k.page_start = 10, k.page_end = 14
            """,
            chunk_id=_CHUNK_ID,
        )
        await session.run(
            """
            MATCH (k:Chunk {id: $chunk_id})
            MATCH (canon:Entity {id: $canon_id})
            MATCH (other:Entity {id: $other_id})
            MERGE (k)-[m1:MENTIONS]->(canon)
              SET m1.source_page = 12, m1.chunk_index = 0
            MERGE (k)-[m2:MENTIONS]->(other)
              SET m2.source_page = 13, m2.chunk_index = 0
            MERGE (canon)-[r:RELATED]->(other)
              SET r.type = 'related', r.source_page = 12, r.chunk_index = 0
            """,
            chunk_id=_CHUNK_ID,
            canon_id=_CANON_ID,
            other_id=_OTHER_ID,
        )


@pytest.mark.neo4j_integration
async def test_audit_entity_rules_exclude_soft_deleted_entities(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """A non-empty ``merged_into`` entity causes no entity-rule finding.

    The canonical counterpart is fully clean (mentioned, related, has a source
    page, unique name+type), so any non-zero total in an entity rule can only be
    caused by the soft-deleted duplicate.
    """
    await _seed_clean_canonical_plus_soft_deleted_duplicate(neo4j_driver)

    adapter = Neo4jAuditAdapter(neo4j_settings)
    try:
        target = build_audit_target("bookgraph-neo4j", neo4j_settings.neo4j_uri, "neo4j")
        snapshot = await adapter.collect_snapshot(target, sample_limit=10)
    finally:
        await adapter.close()

    assert snapshot.failure_state is None
    for rule_id in _ENTITY_RULES:
        assert _finding_total(snapshot, rule_id) == 0, (
            f"{rule_id} reported the soft-deleted entity {_DUP_ID}"
        )


@pytest.mark.neo4j_integration
async def test_audit_entity_rules_treat_empty_merged_into_as_active(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """An empty-string ``merged_into`` remains active (defensive compatibility).

    Guard against over-filtering: the read paths treat ``merged_into = ''`` as
    active, so the audit must keep such entities visible.
    """
    async with neo4j_driver.session() as session:
        await session.run(
            """
            MERGE (ghost:Entity {id: $ghost_id})
            SET ghost.name = 'Lonely Ghost', ghost.type = 'concept',
                ghost.merged_into = ''
            """,
            ghost_id="book:ch1:empty-merged",
        )

    adapter = Neo4jAuditAdapter(neo4j_settings)
    try:
        target = build_audit_target("bookgraph-neo4j", neo4j_settings.neo4j_uri, "neo4j")
        snapshot = await adapter.collect_snapshot(target, sample_limit=10)
    finally:
        await adapter.close()

    assert snapshot.failure_state is None
    for rule_id in (
        "ENTITY_UNMENTIONED",
        "ENTITY_ISOLATED_RELATED",
        "PROVENANCE_ENTITY_MISSING",
    ):
        assert "book:ch1:empty-merged" in _subject_ids(snapshot, rule_id), (
            f"{rule_id} must still report an empty-merged_into entity"
        )


def test_entity_scanning_queries_filter_soft_deleted_entities() -> None:
    """Static contract: every entity-scanning rule filters merged_into.

    ``duplicates_relationship`` keys on entity ids, which the Phase 3 merge never
    rewrites; merged duplicates keep zero RELATED edges after edge re-pointing,
    so no false duplicate group can form and the filter is intentionally absent.
    """
    queries = dict(QUERY_PLAN)
    for name in (
        "entity_unmentioned",
        "entity_isolated_related",
        "provenance_entity",
        "duplicates_entity",
    ):
        assert "merged_into" in queries[name], f"{name} must filter merged_into"
    assert "merged_into" not in queries["duplicates_relationship"]
