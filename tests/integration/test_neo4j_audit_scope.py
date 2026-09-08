"""Scoped audit contract tests against a real testcontainers Neo4j.

Covers namespace-aware duplicate keys, merged-entity exclusion, and
cross-namespace edge semantics under scoped vs whole-graph audits.
"""

from __future__ import annotations

from typing import Any

import pytest

from book_graph_rag.application.audit_graph_use_case import (
    AuditGraphUseCase,
    build_audit_target,
)
from book_graph_rag.config import Settings
from book_graph_rag.domain.audit_models import AuditReport, AuditScope, AuditSnapshot, Severity
from book_graph_rag.infrastructure.neo4j_audit_adapter import (
    QUERY_PLAN,
    Neo4jAuditAdapter,
)

_NS1 = "knowledge:agentic-architectural-patterns"
_NS2 = "knowledge:other-source"


def _finding(snapshot: AuditSnapshot, rule_id: str) -> Any:
    return next(f for f in snapshot.findings if f.rule_id == rule_id)


def _snapshot_total(snapshot: AuditSnapshot, rule_id: str) -> int:
    return int(_finding(snapshot, rule_id).total)


def _report_total(report: AuditReport, rule_id: str) -> int:
    return int(next(f for f in report.findings if f.rule_id == rule_id).total)


async def _seed_two_namespace_duplicate_graph(driver: Any) -> None:
    """Seed ns1 and ns2 with same-name cross-namespace and same-namespace duplicates."""
    async with driver.session() as session:
        # Cross-namespace same name+type: should NOT be duplicate.
        await session.run(
            """
            MERGE (e1:Entity {id: $id1})
            SET e1.name = 'Attention', e1.type = 'concept', e1.source_page = 10
            MERGE (e2:Entity {id: $id2})
            SET e2.name = 'Attention', e2.type = 'concept', e2.source_page = 11
            """,
            id1=f"{_NS1}:attention-concept",
            id2=f"{_NS2}:attention-concept",
        )
        # Same-namespace duplicate in ns1.
        await session.run(
            """
            MERGE (e1:Entity {id: $id1})
            SET e1.name = 'Agent Smith', e1.type = 'agent', e1.source_page = 12
            MERGE (e2:Entity {id: $id2})
            SET e2.name = 'Agent Smith', e2.type = 'agent', e2.source_page = 13
            """,
            id1=f"{_NS1}:agent-smith-agent",
            id2=f"{_NS1}:agent-smith-agent-dup",
        )
        # Chunks that mention the ns1 entities so they are not unmentioned/isolated.
        await session.run(
            """
            MERGE (k:Chunk {id: $chunk_id})
            SET k.book_id = $book_id, k.chunk_index = 0,
                k.page_start = 1, k.page_end = 5
            """,
            chunk_id=f"{_NS1}:chunk-0",
            book_id=_NS1,
        )
        await session.run(
            """
            MATCH (k:Chunk {id: $chunk_id})
            MATCH (e1:Entity {id: $id1})
            MATCH (e2:Entity {id: $id2})
            MATCH (e3:Entity {id: $id3})
            MERGE (k)-[m1:MENTIONS]->(e1)
                SET m1.source_page = 1, m1.chunk_index = 0
            MERGE (k)-[m2:MENTIONS]->(e2)
                SET m2.source_page = 1, m2.chunk_index = 0
            MERGE (k)-[m3:MENTIONS]->(e3)
                SET m3.source_page = 1, m3.chunk_index = 0
            MERGE (e1)-[r:RELATED]->(e2)
                SET r.type = 'related', r.source_page = 1, r.chunk_index = 0
            MERGE (e1)-[r2:RELATED]->(e3)
                SET r2.type = 'related', r2.source_page = 1, r2.chunk_index = 0
            """,
            chunk_id=f"{_NS1}:chunk-0",
            id1=f"{_NS1}:agent-smith-agent",
            id2=f"{_NS1}:agent-smith-agent-dup",
            id3=f"{_NS1}:attention-concept",
        )


@pytest.mark.neo4j_integration
async def test_duplicate_entity_logical_groups_by_namespace_name_type(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """DUPLICATE_ENTITY_LOGICAL groups by (namespace, name, type)."""
    await _seed_two_namespace_duplicate_graph(neo4j_driver)
    adapter = Neo4jAuditAdapter(neo4j_settings)
    try:
        target = build_audit_target(
            "bookgraph-neo4j", neo4j_settings.neo4j_uri, "neo4j"
        )
        snapshot = await adapter.collect_snapshot(target, sample_limit=10)
    finally:
        await adapter.close()

    finding = _finding(snapshot, "DUPLICATE_ENTITY_LOGICAL")
    assert finding.total == 1
    sample = finding.samples[0]
    assert sample.namespace == _NS1


@pytest.mark.neo4j_integration
async def test_cross_namespace_same_name_not_duplicate(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """Same name+type in different namespaces produces zero duplicate findings."""
    await _seed_two_namespace_duplicate_graph(neo4j_driver)
    adapter = Neo4jAuditAdapter(neo4j_settings)
    try:
        target = build_audit_target(
            "bookgraph-neo4j", neo4j_settings.neo4j_uri, "neo4j"
        )
        snapshot = await adapter.collect_snapshot(target, sample_limit=10)
    finally:
        await adapter.close()

    sample = _finding(snapshot, "DUPLICATE_ENTITY_LOGICAL").samples[0]
    subject_ids = set(sample.subject_ids)
    assert f"{_NS2}:attention-concept" not in subject_ids


@pytest.mark.neo4j_integration
async def test_same_namespace_duplicates_still_detected(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """Same (name, type) inside the same namespace is still flagged."""
    await _seed_two_namespace_duplicate_graph(neo4j_driver)
    adapter = Neo4jAuditAdapter(neo4j_settings)
    try:
        target = build_audit_target(
            "bookgraph-neo4j", neo4j_settings.neo4j_uri, "neo4j"
        )
        snapshot = await adapter.collect_snapshot(target, sample_limit=10)
    finally:
        await adapter.close()

    finding = _finding(snapshot, "DUPLICATE_ENTITY_LOGICAL")
    assert finding.total == 1
    assert finding.severity == Severity.WARNING
    sample = finding.samples[0]
    assert sample.properties.get("name") == "Agent Smith"


@pytest.mark.neo4j_integration
async def test_relationship_duplicates_unchanged_key_on_full_ids(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """Relationship duplicate keys still use full namespace-bearing ids."""
    async with neo4j_driver.session() as session:
        await session.run(
            """
            MERGE (a:Entity {id: $a}) SET a.name = 'A', a.type = 'concept'
            MERGE (b:Entity {id: $b}) SET b.name = 'B', b.type = 'concept'
            CREATE (a)-[r1:RELATED]->(b) SET r1.type = 'related'
            CREATE (a)-[r2:RELATED]->(b) SET r2.type = 'related'
            """,
            a=f"{_NS1}:a-concept",
            b=f"{_NS1}:b-concept",
        )
    adapter = Neo4jAuditAdapter(neo4j_settings)
    try:
        target = build_audit_target(
            "bookgraph-neo4j", neo4j_settings.neo4j_uri, "neo4j"
        )
        snapshot = await adapter.collect_snapshot(target, sample_limit=10)
    finally:
        await adapter.close()

    assert _snapshot_total(snapshot, "DUPLICATE_RELATIONSHIP_LOGICAL") == 1


async def _seed_merged_entity_graph(driver: Any) -> None:
    """Seed ns1 with a merged duplicate; ns2 with a similar active pair."""
    async with driver.session() as session:
        await session.run(
            """
            MERGE (canon:Entity {id: $canon_id})
            SET canon.name = 'Canonical Agent', canon.type = 'agent',
                canon.source_page = 1
            MERGE (dup:Entity {id: $dup_id})
            SET dup.name = 'Canonical Agent', dup.type = 'agent',
                dup.merged_into = $canon_id, dup.merged_at = datetime()
            MERGE (other:Entity {id: $other_id})
            SET other.name = 'Other', other.type = 'concept',
                other.source_page = 2
            MERGE (k:Chunk {id: $chunk_id})
            SET k.book_id = $book_id, k.chunk_index = 0,
                k.page_start = 1, k.page_end = 5
            """,
            canon_id=f"{_NS1}:canonical-agent",
            dup_id=f"{_NS1}:duplicate-agent",
            other_id=f"{_NS1}:other-concept",
            chunk_id=f"{_NS1}:chunk-0",
            book_id=_NS1,
        )
        await session.run(
            """
            MATCH (k:Chunk {id: $chunk_id})
            MATCH (canon:Entity {id: $canon_id})
            MATCH (other:Entity {id: $other_id})
            MERGE (k)-[m:MENTIONS]->(canon)
                SET m.source_page = 1, m.chunk_index = 0
            MERGE (k)-[m2:MENTIONS]->(other)
                SET m2.source_page = 1, m2.chunk_index = 0
            MERGE (canon)-[r:RELATED]->(other)
                SET r.type = 'related', r.source_page = 1, r.chunk_index = 0
            """,
            chunk_id=f"{_NS1}:chunk-0",
            canon_id=f"{_NS1}:canonical-agent",
            other_id=f"{_NS1}:other-concept",
        )
        await session.run(
            """
            MERGE (e1:Entity {id: $id1})
            SET e1.name = 'NS2 Agent', e1.type = 'agent', e1.source_page = 10
            MERGE (e2:Entity {id: $id2})
            SET e2.name = 'NS2 Agent', e2.type = 'agent', e2.source_page = 11
            """,
            id1=f"{_NS2}:agent-ns2",
            id2=f"{_NS2}:agent-ns2-dup",
        )


@pytest.mark.neo4j_integration
async def test_merged_entity_excluded_from_scoped_audit(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """A merged entity produces no findings under a scoped audit."""
    await _seed_merged_entity_graph(neo4j_driver)
    adapter = Neo4jAuditAdapter(neo4j_settings)
    try:
        target = build_audit_target(
            "bookgraph-neo4j", neo4j_settings.neo4j_uri, "neo4j"
        )
        scope = AuditScope(corpus="knowledge", source="agentic-architectural-patterns")
        snapshot = await adapter.collect_snapshot(
            target, sample_limit=10, scope=scope
        )
    finally:
        await adapter.close()

    assert snapshot.failure_state is None
    entity_rules = (
        "DUPLICATE_ENTITY_LOGICAL",
        "ENTITY_UNMENTIONED",
        "ENTITY_ISOLATED_RELATED",
        "PROVENANCE_ENTITY_MISSING",
    )
    for rule_id in entity_rules:
        for finding in snapshot.findings:
            if finding.rule_id == rule_id:
                for sample in finding.samples:
                    assert f"{_NS1}:duplicate-agent" not in sample.subject_ids


@pytest.mark.neo4j_integration
async def test_merged_entity_excluded_from_whole_graph_audit(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """A merged entity produces no findings in a whole-graph audit."""
    await _seed_merged_entity_graph(neo4j_driver)
    adapter = Neo4jAuditAdapter(neo4j_settings)
    try:
        target = build_audit_target(
            "bookgraph-neo4j", neo4j_settings.neo4j_uri, "neo4j"
        )
        snapshot = await adapter.collect_snapshot(target, sample_limit=10)
    finally:
        await adapter.close()

    assert snapshot.failure_state is None
    entity_rules = (
        "DUPLICATE_ENTITY_LOGICAL",
        "ENTITY_UNMENTIONED",
        "ENTITY_ISOLATED_RELATED",
        "PROVENANCE_ENTITY_MISSING",
    )
    for rule_id in entity_rules:
        for finding in snapshot.findings:
            if finding.rule_id == rule_id:
                for sample in finding.samples:
                    assert f"{_NS1}:duplicate-agent" not in sample.subject_ids


@pytest.mark.neo4j_integration
async def test_scoped_audit_merged_and_active_both_correct(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """Scoped audit counts only active entities; ns2 duplicates are invisible."""
    await _seed_merged_entity_graph(neo4j_driver)
    adapter = Neo4jAuditAdapter(neo4j_settings)
    try:
        target = build_audit_target(
            "bookgraph-neo4j", neo4j_settings.neo4j_uri, "neo4j"
        )
        scope = AuditScope(corpus="knowledge", source="agentic-architectural-patterns")
        snapshot = await adapter.collect_snapshot(
            target, sample_limit=10, scope=scope
        )
    finally:
        await adapter.close()

    assert _snapshot_total(snapshot, "DUPLICATE_ENTITY_LOGICAL") == 0
    assert _snapshot_total(snapshot, "ENTITY_UNMENTIONED") == 0
    assert _snapshot_total(snapshot, "ENTITY_ISOLATED_RELATED") == 0
    assert _snapshot_total(snapshot, "PROVENANCE_ENTITY_MISSING") == 0


@pytest.mark.neo4j_integration
async def test_scope_prefix_and_merged_into_combined_in_same_statement(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """Entity-scoped queries carry both scope prefix and merged_into in one WHERE."""
    from book_graph_rag.infrastructure.neo4j_audit_adapter import _scoped_query

    scope = AuditScope(corpus="knowledge", source="agentic-architectural-patterns")
    queries = dict(QUERY_PLAN)
    entity_queries = (
        "entity_unmentioned",
        "entity_isolated_related",
        "provenance_entity",
        "duplicates_entity",
    )
    for name in entity_queries:
        query = _scoped_query(name, queries[name], scope)
        assert "STARTS WITH $scope_prefix" in query, name
        assert "merged_into IS NULL OR n.merged_into = ''" in query, name


async def _seed_cross_namespace_edge_graph(driver: Any) -> None:
    """Seed ns1 and ns2 entities with a cross-namespace RELATED edge."""
    async with driver.session() as session:
        await session.run(
            """
            MERGE (a:Entity {id: $a}) SET a.name = 'A', a.type = 'concept',
                a.source_page = 1
            MERGE (b:Entity {id: $b}) SET b.name = 'B', b.type = 'concept',
                b.source_page = 2
            MERGE (c:Chunk {id: $chunk_id})
            SET c.book_id = $book_id, c.chunk_index = 0,
                c.page_start = 1, c.page_end = 5
            """,
            a=f"{_NS1}:a-concept",
            b=f"{_NS2}:b-concept",
            chunk_id=f"{_NS1}:chunk-0",
            book_id=_NS1,
        )
        await session.run(
            """
            MATCH (a:Entity {id: $a})
            MATCH (b:Entity {id: $b})
            MATCH (c:Chunk {id: $chunk_id})
            MERGE (c)-[m:MENTIONS]->(a)
                SET m.source_page = 1, m.chunk_index = 0
            MERGE (a)-[r:RELATED]->(b)
                SET r.type = 'related', r.source_page = 1, r.chunk_index = 0
            """,
            a=f"{_NS1}:a-concept",
            b=f"{_NS2}:b-concept",
            chunk_id=f"{_NS1}:chunk-0",
        )


@pytest.mark.neo4j_integration
async def test_cross_namespace_related_edge_not_endpoint_violation_under_scope(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """A cross-namespace RELATED edge is not an endpoint violation under scope."""
    await _seed_cross_namespace_edge_graph(neo4j_driver)
    adapter = Neo4jAuditAdapter(neo4j_settings)
    try:
        target = build_audit_target(
            "bookgraph-neo4j", neo4j_settings.neo4j_uri, "neo4j"
        )
        scope = AuditScope(corpus="knowledge", source="agentic-architectural-patterns")
        snapshot = await adapter.collect_snapshot(
            target, sample_limit=10, scope=scope
        )
    finally:
        await adapter.close()

    assert _snapshot_total(snapshot, "ENDPOINT_RELATED_INVALID") == 0


async def _seed_invalid_in_scope_edge_graph(driver: Any) -> None:
    """Seed an invalid RELATED edge inside ns1 plus a cross-namespace edge."""
    async with driver.session() as session:
        await session.run(
            """
            MERGE (a:Entity {id: $a}) SET a.name = 'A', a.type = 'concept',
                a.source_page = 1
            MERGE (b:Entity {id: $b}) SET b.name = 'B', b.type = 'concept',
                b.source_page = 2
            MERGE (c:Chunk {id: $chunk_id})
            SET c.book_id = $book_id, c.chunk_index = 0,
                c.page_start = 1, c.page_end = 5
            """,
            a=f"{_NS1}:a-concept",
            b=f"{_NS2}:b-concept",
            chunk_id=f"{_NS1}:chunk-0",
            book_id=_NS1,
        )
        await session.run(
            """
            MATCH (a:Entity {id: $a})
            MATCH (b:Entity {id: $b})
            MATCH (c:Chunk {id: $chunk_id})
            MERGE (c)-[r:RELATED]->(a) SET r.type = 'related'
            MERGE (a)-[r2:RELATED]->(b) SET r2.type = 'related'
            """,
            a=f"{_NS1}:a-concept",
            b=f"{_NS2}:b-concept",
            chunk_id=f"{_NS1}:chunk-0",
        )


@pytest.mark.neo4j_integration
async def test_in_scope_invalid_edge_still_reported(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """Genuinely invalid in-scope edges are still reported."""
    await _seed_invalid_in_scope_edge_graph(neo4j_driver)
    adapter = Neo4jAuditAdapter(neo4j_settings)
    try:
        target = build_audit_target(
            "bookgraph-neo4j", neo4j_settings.neo4j_uri, "neo4j"
        )
        scope = AuditScope(corpus="knowledge", source="agentic-architectural-patterns")
        snapshot = await adapter.collect_snapshot(
            target, sample_limit=10, scope=scope
        )
    finally:
        await adapter.close()

    finding = _finding(snapshot, "ENDPOINT_RELATED_INVALID")
    assert finding.total == 1
    sample = finding.samples[0]
    assert f"{_NS1}:chunk-0" in sample.subject_ids


@pytest.mark.neo4j_integration
async def test_scoped_audit_counts_only_in_scope_nodes(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """A scoped audit returns only in-scope counts and marks the scope field."""
    await _seed_two_namespace_duplicate_graph(neo4j_driver)
    adapter = Neo4jAuditAdapter(neo4j_settings)
    use_case = AuditGraphUseCase(adapter)
    target = build_audit_target(
        "bookgraph-neo4j", neo4j_settings.neo4j_uri, "neo4j"
    )
    scope = AuditScope(corpus="knowledge", source="agentic-architectural-patterns")
    try:
        scoped_report = await use_case.execute(target, sample_limit=10, scope=scope)
        whole_report = await use_case.execute(target, sample_limit=10)
    finally:
        await adapter.close()

    assert scoped_report.scope == _NS1
    assert whole_report.scope is None
    assert _report_total(scoped_report, "DUPLICATE_ENTITY_LOGICAL") == 1
    assert _report_total(whole_report, "DUPLICATE_ENTITY_LOGICAL") == 1
    assert _report_total(scoped_report, "ENTITY_UNMENTIONED") == 0
    assert _report_total(scoped_report, "ENTITY_ISOLATED_RELATED") == 0


@pytest.mark.neo4j_integration
async def test_scoped_inventory_node_count_matches_active_set(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """Inventory counts under scope include only active in-scope nodes."""
    await _seed_two_namespace_duplicate_graph(neo4j_driver)
    adapter = Neo4jAuditAdapter(neo4j_settings)
    try:
        target = build_audit_target(
            "bookgraph-neo4j", neo4j_settings.neo4j_uri, "neo4j"
        )
        scope = AuditScope(corpus="knowledge", source="agentic-architectural-patterns")
        snapshot = await adapter.collect_snapshot(
            target, sample_limit=10, scope=scope
        )
    finally:
        await adapter.close()

    assert snapshot.inventory["Entity"].value == 3
    assert snapshot.inventory["Chunk"].value == 1


@pytest.mark.neo4j_integration
async def test_scoped_inventory_relationship_count_only_when_endpoint_in_scope(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """Inventory relationship counts under scope include only in-scope edges."""
    await _seed_cross_namespace_edge_graph(neo4j_driver)
    adapter = Neo4jAuditAdapter(neo4j_settings)
    try:
        target = build_audit_target(
            "bookgraph-neo4j", neo4j_settings.neo4j_uri, "neo4j"
        )
        scope = AuditScope(corpus="knowledge", source="agentic-architectural-patterns")
        snapshot = await adapter.collect_snapshot(
            target, sample_limit=10, scope=scope
        )
    finally:
        await adapter.close()

    assert snapshot.inventory["RELATED"].value == 0
    assert snapshot.inventory["MENTIONS"].value == 1
