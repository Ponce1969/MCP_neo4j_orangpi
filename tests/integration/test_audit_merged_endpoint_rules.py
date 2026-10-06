"""Merged-endpoint audit rules against a real testcontainers Neo4j.

``ENDPOINT_MENTIONS_MERGED_INVALID`` and ``ENDPOINT_RELATED_MERGED_INVALID``
make the "edge points at a soft-deleted (``merged_into``) entity" class visible
to the audit (spec: ``odd/specs/tech-debt-essential-graphrag-endpoints.md`` §5,
decision D5). The rules inherit category ``endpoints`` -> severity ``blocking``
from the ``ENDPOINT_`` prefix; no manual ``RULE_CATEGORY`` entry exists.

These are testcontainers integration tests and never touch production.
"""

from __future__ import annotations

from typing import Any

import pytest

from book_graph_rag.application.audit_graph_use_case import build_audit_target
from book_graph_rag.config import Settings
from book_graph_rag.domain.audit_models import AuditScope, AuditSnapshot, Severity
from book_graph_rag.infrastructure.neo4j_audit_adapter import (
    QUERY_PLAN,
    Neo4jAuditAdapter,
    _scope_predicate,
)

_NS1 = "knowledge:alpha"
_NS2 = "knowledge:beta"

_MENTIONS_RULE = "ENDPOINT_MENTIONS_MERGED_INVALID"
_RELATED_RULE = "ENDPOINT_RELATED_MERGED_INVALID"

# Global (whole-graph) expectations for the seeded graph below.
_EXPECTED_GLOBAL_MENTIONS = 2  # one dangling MENTIONS per namespace
_EXPECTED_GLOBAL_RELATED = 3  # ns1: target-merged + source-merged; ns2: target-merged
# Scoped to ns1: only the ns1 chunk (book_id) and ns1<->ns1 RELATED edges count.
_EXPECTED_NS1_MENTIONS = 1
_EXPECTED_NS1_RELATED = 2


def _finding(snapshot: AuditSnapshot, rule_id: str) -> Any:
    return next(f for f in snapshot.findings if f.rule_id == rule_id)


def _finding_total(snapshot: AuditSnapshot, rule_id: str) -> int:
    return int(_finding(snapshot, rule_id).total)


async def _seed_merged_endpoint_graph(driver: Any) -> None:
    """Seed two namespaces with dangling MENTIONS/RELATED plus live-only edges.

    ns1 carries both dangling flavours (RELATED with a merged target and with a
    merged source); ns2 carries one of each so scoping is observable.
    """
    async with driver.session() as session:
        await session.run(
            """
            MERGE (canon:Entity {id: $canon})
            SET canon.name = 'Canonical Alpha', canon.type = 'concept',
                canon.source_page = 1
            MERGE (merged:Entity {id: $merged})
            SET merged.name = 'Canonical Alpha', merged.type = 'concept',
                merged.merged_into = $canon, merged.merged_at = datetime()
            MERGE (live:Entity {id: $live})
            SET live.name = 'Live Alpha', live.type = 'concept',
                live.source_page = 2
            MERGE (k:Chunk {id: $chunk})
            SET k.book_id = $book_id, k.chunk_index = 0,
                k.page_start = 1, k.page_end = 5
            """,
            canon=f"{_NS1}:canonical-alpha",
            merged=f"{_NS1}:merged-alpha",
            live=f"{_NS1}:live-alpha",
            chunk=f"{_NS1}:chunk-0",
            book_id=_NS1,
        )
        await session.run(
            """
            MATCH (k:Chunk {id: $chunk})
            MATCH (merged:Entity {id: $merged})
            MATCH (live:Entity {id: $live})
            MERGE (k)-[m1:MENTIONS]->(merged)
                SET m1.source_page = 1, m1.chunk_index = 0
            MERGE (k)-[m2:MENTIONS]->(live)
                SET m2.source_page = 2, m2.chunk_index = 0
            MERGE (live)-[r1:RELATED]->(merged)
                SET r1.type = 'related', r1.source_page = 1, r1.chunk_index = 0
            MERGE (merged)-[r2:RELATED]->(live)
                SET r2.type = 'related', r2.source_page = 1, r2.chunk_index = 0
            """,
            chunk=f"{_NS1}:chunk-0",
            merged=f"{_NS1}:merged-alpha",
            live=f"{_NS1}:live-alpha",
        )
        await session.run(
            """
            MERGE (canon:Entity {id: $canon})
            SET canon.name = 'Canonical Beta', canon.type = 'concept',
                canon.source_page = 10
            MERGE (merged:Entity {id: $merged})
            SET merged.name = 'Canonical Beta', merged.type = 'concept',
                merged.merged_into = $canon, merged.merged_at = datetime()
            MERGE (live:Entity {id: $live})
            SET live.name = 'Live Beta', live.type = 'concept',
                live.source_page = 11
            MERGE (k:Chunk {id: $chunk})
            SET k.book_id = $book_id, k.chunk_index = 0,
                k.page_start = 1, k.page_end = 5
            MERGE (k)-[m1:MENTIONS]->(merged)
                SET m1.source_page = 10, m1.chunk_index = 0
            MERGE (k)-[m2:MENTIONS]->(live)
                SET m2.source_page = 11, m2.chunk_index = 0
            MERGE (live)-[r1:RELATED]->(merged)
                SET r1.type = 'related', r1.source_page = 10, r1.chunk_index = 0
            """,
            canon=f"{_NS2}:canonical-beta",
            merged=f"{_NS2}:merged-beta",
            live=f"{_NS2}:live-beta",
            chunk=f"{_NS2}:chunk-0",
            book_id=_NS2,
        )


async def _collect(
    settings: Settings,
    scope: AuditScope | None = None,
) -> AuditSnapshot:
    adapter = Neo4jAuditAdapter(settings)
    try:
        target = build_audit_target("bookgraph-neo4j", settings.neo4j_uri, "neo4j")
        return await adapter.collect_snapshot(target, sample_limit=10, scope=scope)
    finally:
        await adapter.close()


@pytest.mark.neo4j_integration
async def test_merged_endpoint_rules_report_seeded_dangling_edges(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """Both new rules exist, are blocking, and count exactly the seeded edges."""
    await _seed_merged_endpoint_graph(neo4j_driver)

    snapshot = await _collect(neo4j_settings)

    assert snapshot.failure_state is None
    assert len(snapshot.findings) == 23, "catalog must carry the two new rules"
    for rule_id in (_MENTIONS_RULE, _RELATED_RULE):
        finding = _finding(snapshot, rule_id)
        assert finding.category == "endpoints", rule_id
        assert finding.severity == Severity.BLOCKING, rule_id
    assert _finding_total(snapshot, _MENTIONS_RULE) == _EXPECTED_GLOBAL_MENTIONS
    assert _finding_total(snapshot, _RELATED_RULE) == _EXPECTED_GLOBAL_RELATED


@pytest.mark.neo4j_integration
async def test_merged_endpoint_rules_do_not_inflate_shape_rules(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """Soft-deleted endpoints stay invisible to the shape-only endpoint rules.

    ``endpoints_mentions`` / ``endpoints_related`` check labels and null ids,
    not liveness, so the seeded merged endpoints must not move their totals.
    """
    await _seed_merged_endpoint_graph(neo4j_driver)

    snapshot = await _collect(neo4j_settings)

    assert snapshot.failure_state is None
    assert _finding_total(snapshot, "ENDPOINT_MENTIONS_INVALID") == 0
    assert _finding_total(snapshot, "ENDPOINT_RELATED_INVALID") == 0


@pytest.mark.neo4j_integration
async def test_merged_endpoint_rules_scope_to_single_namespace(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """A scoped audit counts only the scoped namespace for both new rules."""
    await _seed_merged_endpoint_graph(neo4j_driver)

    scope = AuditScope(corpus="knowledge", source="alpha")
    snapshot = await _collect(neo4j_settings, scope=scope)

    assert snapshot.failure_state is None
    assert _finding_total(snapshot, _MENTIONS_RULE) == _EXPECTED_NS1_MENTIONS
    assert _finding_total(snapshot, _RELATED_RULE) == _EXPECTED_NS1_RELATED
    assert _finding_total(snapshot, "ENDPOINT_MENTIONS_INVALID") == 0
    assert _finding_total(snapshot, "ENDPOINT_RELATED_INVALID") == 0


@pytest.mark.neo4j_integration
async def test_merged_endpoint_rules_zero_on_live_only_edges(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """Live endpoints and an empty-string ``merged_into`` are not violations."""
    async with neo4j_driver.session() as session:
        await session.run(
            """
            MERGE (a:Entity {id: 'knowledge:gamma:live-a'})
            SET a.name = 'A', a.type = 'concept', a.source_page = 1
            MERGE (b:Entity {id: 'knowledge:gamma:live-b'})
            SET b.name = 'B', b.type = 'concept', b.source_page = 2,
                b.merged_into = ''
            MERGE (k:Chunk {id: 'knowledge:gamma:chunk-0'})
            SET k.book_id = 'knowledge:gamma', k.chunk_index = 0,
                k.page_start = 1, k.page_end = 5
            MERGE (k)-[m:MENTIONS]->(a)
                SET m.source_page = 1, m.chunk_index = 0
            MERGE (k)-[m2:MENTIONS]->(b)
                SET m2.source_page = 2, m2.chunk_index = 0
            MERGE (a)-[r:RELATED]->(b)
                SET r.type = 'related', r.source_page = 1, r.chunk_index = 0
            """
        )

    snapshot = await _collect(neo4j_settings)

    assert snapshot.failure_state is None
    assert _finding_total(snapshot, _MENTIONS_RULE) == 0
    assert _finding_total(snapshot, _RELATED_RULE) == 0
    assert _finding_total(snapshot, "ENDPOINT_MENTIONS_INVALID") == 0
    assert _finding_total(snapshot, "ENDPOINT_RELATED_INVALID") == 0


def test_merged_endpoint_scope_predicates_match_their_edge_kind() -> None:
    """Static contract: MENTIONS scopes by book_id, RELATED by entity prefix."""
    mentions = _scope_predicate("endpoints_mentions_merged")
    related = _scope_predicate("endpoints_related_merged")
    assert mentions is not None
    assert "book_id" in mentions
    assert "$scope_source_id" in mentions
    assert related is not None
    assert "STARTS WITH $scope_prefix" in related
    assert related.count("STARTS WITH") == 2  # both RELATED endpoints scoped
    queries = dict(QUERY_PLAN)
    for name in ("endpoints_mentions_merged", "endpoints_related_merged"):
        assert "merged_into" in queries[name], name
        assert "$sample_limit" in queries[name], name
        assert queries[name].index("ORDER BY") < queries[name].index("collect(")
