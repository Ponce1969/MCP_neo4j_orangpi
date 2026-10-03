"""Cross-namespace duplicate and self-loop audit rules on testcontainers Neo4j.

``DUPLICATE_ENTITY_CROSS_NAMESPACE`` (ODD task T3) reports active entities whose
normalized name + type appear in two or more ``corpus:source`` namespaces. Its
severity ``WARNING`` and category ``duplicates`` derive from the ``DUPLICATE_``
prefix in ``RULE_CATEGORY`` + ``severity_for_category``; no manual entry exists.

``ENDPOINT_SELF_LOOP_INVALID`` (ODD task T4) reports
``(:Entity)-[r:RELATED]->(:Entity)`` loops whose two endpoints are the same
node. Blocking + ``endpoints`` derive from the ``ENDPOINT_`` prefix; the design
draft's ``ENTITY_SELF_LOOP_INVALID`` would have needed a bespoke
``RULE_CATEGORY`` mapping. Production has zero self-loops (the merge path
stopped creating them on 2026-10-02), so this rule is the regression guard for
extraction noise.

Scoped audits use a dedicated R5a branch: the generic scope predicate would
inject before the grouping and leave every group single-namespace (reporting
zero), so the scoped variant keeps the cross-namespace condition and then keeps
the groups with at least one member inside ``$scope_prefix``.

These are testcontainers integration tests and never touch production.
"""

from __future__ import annotations

from typing import Any

import pytest

from book_graph_rag.application.audit_graph_use_case import build_audit_target
from book_graph_rag.config import Settings
from book_graph_rag.domain.audit_models import (
    RULE_CATALOG,
    RULE_CATEGORY,
    AuditScope,
    AuditSnapshot,
    Severity,
    severity_for_category,
)
from book_graph_rag.infrastructure.neo4j_audit_adapter import (
    QUERY_PLAN,
    Neo4jAuditAdapter,
    _scope_predicate,
    _scoped_query,
)

_NS1 = "knowledge:alpha"
_NS2 = "knowledge:beta"
_NS3 = "knowledge:delta"  # unrelated namespace: must not see the pair

_CROSS_RULE = "DUPLICATE_ENTITY_CROSS_NAMESPACE"
_SELF_LOOP_RULE = "ENDPOINT_SELF_LOOP_INVALID"

_CROSS_NAME = "Pattern Agent"
_GHOST_NAME = "Ghost"

# Seeded graph expectations (global, whole-graph snapshot).
_EXPECTED_CROSS_GROUPS = 1  # one group: the alpha/beta pair (ghost pair is duplicates_entity's)
_EXPECTED_DUP_GROUPS = 1  # ghost pair: duplicates_entity's job, groups convention
_EXPECTED_UNMENTIONED = 5  # 4 pair/ghost entities + the self-loop node
_EXPECTED_ISOLATED = 4  # only the self-loop node has a RELATED edge


def _finding(snapshot: AuditSnapshot, rule_id: str) -> Any:
    return next(f for f in snapshot.findings if f.rule_id == rule_id)


def _finding_total(snapshot: AuditSnapshot, rule_id: str) -> int:
    return int(_finding(snapshot, rule_id).total)


def _subject_ids(snapshot: AuditSnapshot, rule_id: str) -> set[str]:
    finding = _finding(snapshot, rule_id)
    return {sid for sample in finding.samples for sid in sample.subject_ids}


async def _seed_cross_namespace_graph(driver: Any) -> None:
    """Seed one cross-namespace pair plus a same-namespace-only duplicate.

    The pair shares normalized name + type across alpha/beta; the ghost pair
    exists only in alpha, so ``duplicates_entity`` must keep reporting it while
    ``DUPLICATE_ENTITY_CROSS_NAMESPACE`` must ignore it.
    """
    async with driver.session() as session:
        await session.run(
            """
            MERGE (a:Entity {id: $a_id})
            SET a.name = 'Pattern Agent', a.type = 'concept', a.source_page = 1
            MERGE (b:Entity {id: $b_id})
            SET b.name = 'Pattern Agent', b.type = 'concept', b.source_page = 2
            MERGE (g1:Entity {id: $g1_id})
            SET g1.name = 'Ghost', g1.type = 'concept', g1.source_page = 3
            MERGE (g2:Entity {id: $g2_id})
            SET g2.name = 'Ghost', g2.type = 'concept', g2.source_page = 4
            """,
            a_id=f"{_NS1}:pattern-agent",
            b_id=f"{_NS2}:pattern-agent-alias",
            g1_id=f"{_NS1}:ghost",
            g2_id=f"{_NS1}:ghost-duplicate",
        )


async def _seed_self_loop(driver: Any) -> None:
    """Seed a single self-referencing RELATED edge with full provenance."""
    async with driver.session() as session:
        await session.run(
            """
            MERGE (l:Entity {id: $id})
            SET l.name = 'Loop', l.type = 'concept', l.source_page = 1
            MERGE (l)-[r:RELATED]->(l)
                SET r.type = 'related', r.source_page = 1, r.chunk_index = 0
            """,
            id=f"{_NS1}:loop-node",
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


def test_new_rule_ids_are_prefix_derived_catalog_entries() -> None:
    """The catalog stays sorted at 23 and derives category/severity by prefix."""
    assert tuple(sorted(RULE_CATALOG)) == RULE_CATALOG
    assert len(RULE_CATALOG) == 23, "catalog must carry the two new rules"
    assert _CROSS_RULE in RULE_CATALOG
    assert _SELF_LOOP_RULE in RULE_CATALOG
    # No manual RULE_CATEGORY entries: both come from the prefix comprehensions.
    assert RULE_CATEGORY[_CROSS_RULE] == "duplicates"
    assert severity_for_category(RULE_CATEGORY[_CROSS_RULE]) == Severity.WARNING
    assert RULE_CATEGORY[_SELF_LOOP_RULE] == "endpoints"
    assert severity_for_category(RULE_CATEGORY[_SELF_LOOP_RULE]) == Severity.BLOCKING


def test_new_rules_are_wired_into_query_plan_and_scope_branches() -> None:
    """Both rules ship a bounded static query with their correct scoping."""
    queries = dict(QUERY_PLAN)
    cross = queries["duplicates_entity_cross_namespace"]
    loop = queries["endpoints_self_loop"]
    for name, query in (
        ("duplicates_entity_cross_namespace", cross),
        ("endpoints_self_loop", loop),
    ):
        assert "$sample_limit" in query, name
        assert query.index("ORDER BY") < query.index("collect("), name
    # R5a mirrors duplicates_entity's liveness filter and cross-namespace grouping.
    assert "merged_into IS NULL" in cross
    assert "size(namespaces) > 1" in cross
    # R5c joins the RELATED scope family: both endpoints carry the predicate.
    related = _scope_predicate("endpoints_self_loop")
    assert related is not None
    assert related.count("STARTS WITH") == 2
    # Dedicated R5a scoped branch: the predicate lands AFTER the grouping, so a
    # scoped audit keeps whole cross-namespace groups instead of reporting zero.
    scope = AuditScope(corpus="knowledge", source="alpha")
    scoped = _scoped_query("duplicates_entity_cross_namespace", cross, scope)
    assert "size(namespaces) > 1" in scoped
    assert "STARTS WITH $scope_prefix" in scoped
    assert scoped.index("size(namespaces) > 1") < scoped.index("$scope_prefix")


@pytest.mark.neo4j_integration
async def test_cross_namespace_rule_counts_groups_and_excludes_same_namespace_pair(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """R5a totals count groups, like duplicates_entity; same-namespace pairs stay out."""
    await _seed_cross_namespace_graph(neo4j_driver)

    snapshot = await _collect(neo4j_settings)

    assert snapshot.failure_state is None
    assert _finding_total(snapshot, _CROSS_RULE) == _EXPECTED_CROSS_GROUPS
    cross_ids = _subject_ids(snapshot, _CROSS_RULE)
    assert {f"{_NS1}:pattern-agent", f"{_NS2}:pattern-agent-alias"} <= cross_ids
    ghost_ids = {f"{_NS1}:ghost", f"{_NS1}:ghost-duplicate"}
    assert not ghost_ids & cross_ids, "same-namespace pair is not this rule's job"
    # The existing duplicates_entity rule keeps reporting the ghost pair.
    assert _finding_total(snapshot, "DUPLICATE_ENTITY_LOGICAL") == _EXPECTED_DUP_GROUPS
    dup_ids = _subject_ids(snapshot, "DUPLICATE_ENTITY_LOGICAL")
    assert ghost_ids <= dup_ids
    assert not {f"{_NS1}:pattern-agent", f"{_NS2}:pattern-agent-alias"} & dup_ids
    # No self-loop was seeded here: R5c reports zero.
    assert _finding_total(snapshot, _SELF_LOOP_RULE) == 0


@pytest.mark.neo4j_integration
async def test_cross_namespace_rule_scoped_branch_counts_group_touching_scope(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """A scoped audit counts a group when any member is inside the scope."""
    await _seed_cross_namespace_graph(neo4j_driver)

    touching = await _collect(neo4j_settings, scope=AuditScope(corpus="knowledge", source="alpha"))
    unrelated = await _collect(neo4j_settings, scope=AuditScope(corpus="knowledge", source="delta"))

    assert touching.failure_state is None
    assert unrelated.failure_state is None
    assert _finding_total(touching, _CROSS_RULE) == _EXPECTED_CROSS_GROUPS
    assert _finding_total(unrelated, _CROSS_RULE) == 0


@pytest.mark.neo4j_integration
async def test_cross_namespace_rule_severity_category_and_namespaces_sample(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """R5a is a duplicates WARNING and its sample carries both namespaces."""
    await _seed_cross_namespace_graph(neo4j_driver)

    snapshot = await _collect(neo4j_settings)

    finding = _finding(snapshot, _CROSS_RULE)
    assert finding.category == "duplicates"
    assert finding.severity == Severity.WARNING
    pair_ids = {f"{_NS1}:pattern-agent", f"{_NS2}:pattern-agent-alias"}
    sample = next(s for s in finding.samples if set(s.subject_ids) == pair_ids)
    # The namespace list lives under a key that safe_properties does not redact.
    namespaces = sample.properties.get("namespaces")
    assert namespaces is not None, "namespaces must survive safe_properties"
    assert set(namespaces) == {_NS1, _NS2}
    assert sample.namespace is not None
    assert set(sample.namespace.split("|")) == {_NS1, _NS2}
    assert sample.group_id is not None


@pytest.mark.neo4j_integration
async def test_self_loop_rule_counts_seeded_loop_globally_and_per_scope(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """R5c totals 1 for one seeded self-loop and scopes to its namespace."""
    await _seed_self_loop(neo4j_driver)

    global_snapshot = await _collect(neo4j_settings)
    in_scope = await _collect(neo4j_settings, scope=AuditScope(corpus="knowledge", source="alpha"))
    out_of_scope = await _collect(
        neo4j_settings, scope=AuditScope(corpus="knowledge", source="beta")
    )

    assert global_snapshot.failure_state is None
    finding = _finding(global_snapshot, _SELF_LOOP_RULE)
    assert finding.total == 1
    assert finding.category == "endpoints"
    assert finding.severity == Severity.BLOCKING
    sample = finding.samples[0]
    loop_id = f"{_NS1}:loop-node"
    assert set(sample.subject_ids) == {loop_id}
    assert sample.key == f"{loop_id}|related|{loop_id}"
    assert sample.properties.get("type") == "related"
    assert _finding_total(in_scope, _SELF_LOOP_RULE) == 1
    assert _finding_total(out_of_scope, _SELF_LOOP_RULE) == 0


@pytest.mark.neo4j_integration
async def test_pre_existing_rule_totals_on_seeded_graph(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """Seeding moves only the totals the seed explains; catalog reaches 23."""
    await _seed_cross_namespace_graph(neo4j_driver)
    await _seed_self_loop(neo4j_driver)

    snapshot = await _collect(neo4j_settings)

    assert snapshot.failure_state is None
    assert len(snapshot.findings) == 23, "one finding per catalog rule"
    # Totals explained by the seeded data.
    assert _finding_total(snapshot, _CROSS_RULE) == _EXPECTED_CROSS_GROUPS
    assert _finding_total(snapshot, _SELF_LOOP_RULE) == 1
    assert _finding_total(snapshot, "DUPLICATE_ENTITY_LOGICAL") == _EXPECTED_DUP_GROUPS
    assert _finding_total(snapshot, "ENTITY_UNMENTIONED") == _EXPECTED_UNMENTIONED
    assert _finding_total(snapshot, "ENTITY_ISOLATED_RELATED") == _EXPECTED_ISOLATED
    # The self-loop node is a valid endpoint and its edge carries provenance.
    assert _finding_total(snapshot, "ENDPOINT_RELATED_INVALID") == 0
    assert _finding_total(snapshot, "ENDPOINT_RELATED_MERGED_INVALID") == 0
    assert _finding_total(snapshot, "PROVENANCE_RELATIONSHIP_MISSING") == 0
    # Entities carry source pages, so no provenance-entity finding either.
    assert _finding_total(snapshot, "PROVENANCE_ENTITY_MISSING") == 0
    assert tuple(sorted(RULE_CATALOG)) == RULE_CATALOG
    assert {_CROSS_RULE, _SELF_LOOP_RULE} <= set(RULE_CATALOG)
