"""Unit tests for scoped audit scope parsing and model behavior."""

from __future__ import annotations

import re

import pytest

from book_graph_rag.application.resolve_audit_scope import resolve_audit_scope
from book_graph_rag.domain.audit_models import (
    AuditScope,
    AuditSnapshot,
    AuditTarget,
    OverallState,
)
from book_graph_rag.domain.namespaces import Catalog, Corpus, Source
from book_graph_rag.infrastructure.neo4j_audit_adapter import QUERY_PLAN, _scoped_query
from book_graph_rag.ports.graph_audit_port import GraphIntegrityAuditPort


def _catalog() -> Catalog:
    return Catalog(
        version=1,
        corpora={
            "knowledge": Corpus(
                label="Knowledge Library",
                sources={
                    "agentic-architectural-patterns": Source(
                        label="Agentic Architectural Patterns",
                        file="data/libro.pdf",
                        status="active",
                    ),
                },
            ),
        },
    )


def test_resolve_scope_accepts_corpus_only() -> None:
    scope = resolve_audit_scope("knowledge", _catalog())
    assert scope == AuditScope(corpus="knowledge")
    assert scope.display == "knowledge"
    assert scope.entity_prefix == "knowledge:"
    assert scope.source_id is None


def test_resolve_scope_accepts_corpus_and_source() -> None:
    scope = resolve_audit_scope("knowledge:agentic-architectural-patterns", _catalog())
    assert scope == AuditScope(corpus="knowledge", source="agentic-architectural-patterns")
    assert scope.display == "knowledge:agentic-architectural-patterns"
    assert scope.entity_prefix == "knowledge:agentic-architectural-patterns:"
    assert scope.source_id == "knowledge:agentic-architectural-patterns"


def test_resolve_scope_rejects_malformed_separator() -> None:
    with pytest.raises(ValueError, match="Malformed scope"):
        resolve_audit_scope("knowledge/source", _catalog())
    with pytest.raises(ValueError, match="Malformed scope"):
        resolve_audit_scope("knowledge:source:extra", _catalog())


def test_resolve_scope_rejects_empty_corpus() -> None:
    with pytest.raises(ValueError, match="Malformed scope"):
        resolve_audit_scope(":source", _catalog())
    with pytest.raises(ValueError, match="scope cannot be empty"):
        resolve_audit_scope("", _catalog())


def test_resolve_scope_rejects_unknown_corpus() -> None:
    with pytest.raises(ValueError, match="Unknown corpus"):
        resolve_audit_scope("nope", _catalog())


def test_resolve_scope_rejects_unknown_source() -> None:
    with pytest.raises(ValueError, match="Unknown source"):
        resolve_audit_scope("knowledge:nope", _catalog())


class _ScopeCapturingPort(GraphIntegrityAuditPort):
    def __init__(self) -> None:
        self.calls: list[tuple[object, int, AuditScope | None]] = []

    async def collect_snapshot(
        self, target: object, sample_limit: int, scope: AuditScope | None = None
    ) -> AuditSnapshot:
        self.calls.append((target, sample_limit, scope))
        return AuditSnapshot()


def test_collect_snapshot_default_scope_is_none() -> None:
    import asyncio

    port = _ScopeCapturingPort()
    asyncio.run(port.collect_snapshot("target", 10, scope=None))
    assert port.calls == [("target", 10, None)]


async def test_collect_snapshot_accepts_scoped_value() -> None:
    port = _ScopeCapturingPort()
    scope = AuditScope(corpus="knowledge", source="agentic-architectural-patterns")
    await port.collect_snapshot("target", 10, scope=scope)
    assert port.calls == [("target", 10, scope)]


def _target() -> AuditTarget:
    return AuditTarget(selector="bookgraph-neo4j", database="neo4j", scheme="bolt", host="db", uri="bolt://db")


def test_scoped_report_scope_field_is_set() -> None:
    from book_graph_rag.domain.audit_models import AuditReport
    report = AuditReport(
        target=_target(), state=OverallState.PASSED,
        scope="knowledge:agentic-architectural-patterns",
    )
    assert report.scope == "knowledge:agentic-architectural-patterns"


def test_whole_graph_report_scope_is_none() -> None:
    from book_graph_rag.domain.audit_models import AuditReport
    report = AuditReport(target=_target(), state=OverallState.PASSED)
    assert report.scope is None


def test_canonical_json_includes_scope_field_when_scoped() -> None:
    from book_graph_rag.domain.audit_models import AuditReport
    report = AuditReport(target=_target(), state=OverallState.PASSED, scope="knowledge")
    payload = report.canonical_json()
    assert '"scope":"knowledge"' in payload


_ENTITY_QUERY_NAMES = {
    "entity_unmentioned",
    "entity_isolated_related",
    "provenance_entity",
    "duplicates_entity",
}
_CHUNK_QUERY_NAMES = {
    "provenance_chunk",
    "pages_chunk",
    "hierarchy_chunk_parent_required",
    "hierarchy_chunk_multiple_parent",
}
_READ_ONLY_RE = re.compile(r"\b(MERGE|CREATE|DELETE|DROP|SET)\b")


def test_scoped_entity_queries_combine_prefix_and_merged_filter() -> None:
    """Entity-scoped queries AND scope prefix with merged_into in one WHERE."""
    scope = AuditScope(corpus="knowledge", source="agentic-architectural-patterns")
    for name, base_query in QUERY_PLAN:
        if name not in _ENTITY_QUERY_NAMES:
            continue
        scoped = _scoped_query(name, base_query, scope)
        assert "STARTS WITH $scope_prefix" in scoped, name
        assert "merged_into IS NULL OR n.merged_into = ''" in scoped, name


def test_scoped_chunk_queries_filter_by_book_id() -> None:
    """Chunk-scoped queries reference book_id for source equality."""
    scope = AuditScope(corpus="knowledge", source="agentic-architectural-patterns")
    for name, base_query in QUERY_PLAN:
        if name not in _CHUNK_QUERY_NAMES:
            continue
        scoped = _scoped_query(name, base_query, scope)
        assert "book_id" in scoped, name


def test_query_plan_contains_no_write_operations() -> None:
    """Every static audit query stays within the read-only contract."""
    for name, query in QUERY_PLAN:
        assert not _READ_ONLY_RE.search(query), f"{name} contains a write operation"
