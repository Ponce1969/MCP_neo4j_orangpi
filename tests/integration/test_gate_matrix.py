"""Integration matrix for readiness gates against a real testcontainers Neo4j."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from book_graph_rag.application.audit_graph_use_case import (
    AuditGraphUseCase,
    build_audit_target,
)
from book_graph_rag.application.evaluate_gate_use_case import GateEvaluatorUseCase
from book_graph_rag.config import Settings
from book_graph_rag.domain.audit_models import AuditScope, OverallState
from book_graph_rag.infrastructure.gate_policy_loader import GatePolicyLoader
from book_graph_rag.infrastructure.neo4j_audit_adapter import Neo4jAuditAdapter

logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)

_NS1 = "knowledge:agentic-architectural-patterns"
_NS2 = "knowledge:other-source"


async def _seed_clean_graph(driver: Any) -> None:
    """Seed a minimal namespace graph with no blocking/warning findings."""
    async with driver.session() as session:
        await session.run(
            """
            MERGE (b:Book {id: $book_id})
            SET b.page_count = 10
            MERGE (c:Chapter {id: $chapter_id})
            SET c.number = 1, c.title = 'Intro', c.page_start = 1
            MERGE (k:Chunk {id: $chunk_id})
            SET k.book_id = $book_id, k.chunk_index = 0,
                k.page_start = 1, k.page_end = 5
            MERGE (e1:Entity {id: $e1})
            SET e1.name = 'Agent', e1.type = 'concept', e1.source_page = 1
            MERGE (e2:Entity {id: $e2})
            SET e2.name = 'AI', e2.type = 'concept', e2.source_page = 1
            MERGE (b)-[:CONTAINS]->(c)
            MERGE (c)-[:HAS_CHUNK]->(k)
            MERGE (k)-[m1:MENTIONS]->(e1)
            SET m1.source_page = 1, m1.chunk_index = 0
            MERGE (k)-[m2:MENTIONS]->(e2)
            SET m2.source_page = 1, m2.chunk_index = 0
            MERGE (e1)-[r:RELATED]->(e2)
            SET r.type = 'related', r.source_page = 1, r.chunk_index = 0
            """,
            book_id=_NS1,
            chapter_id=f"{_NS1}:chapter-1",
            chunk_id=f"{_NS1}:chunk-0",
            e1=f"{_NS1}:agent-concept",
            e2=f"{_NS1}:ai-concept",
        )


async def _seed_duplicate_graph(driver: Any) -> None:
    """Seed ns1 with a same-namespace duplicate pair."""
    await _seed_clean_graph(driver)
    async with driver.session() as session:
        await session.run(
            """
            MERGE (e:Entity {id: $dup_id})
            SET e.name = 'Agent', e.type = 'concept', e.source_page = 2
            """,
            dup_id=f"{_NS1}:agent-dup-concept",
        )


async def _seed_orphan_graph(driver: Any) -> None:
    """Seed ns1 with an unmentioned entity."""
    await _seed_clean_graph(driver)
    async with driver.session() as session:
        await session.run(
            """
            MERGE (e:Entity {id: $orphan_id})
            SET e.name = 'Orphan', e.type = 'concept', e.source_page = 2
            """,
            orphan_id=f"{_NS1}:orphan-concept",
        )


async def _seed_two_namespace_graph(driver: Any) -> None:
    """Seed ns1 clean and ns2 with a blocking hierarchy violation."""
    await _seed_clean_graph(driver)
    async with driver.session() as session:
        await session.run(
            """
            MERGE (b:Book {id: $book_id})
            MERGE (c:Chapter {id: $chapter_id})
            SET c.number = 1, c.title = 'Bad', c.page_start = 1
            // Chapter has no Book parent -> hierarchy violation in ns2.
            """,
            book_id=_NS2,
            chapter_id=f"{_NS2}:chapter-1",
        )


async def _run_scoped_audit(
    neo4j_settings: Settings,
    scope: AuditScope | None,
) -> Any:
    adapter = Neo4jAuditAdapter(neo4j_settings)
    try:
        target = build_audit_target(
            "bookgraph-neo4j", neo4j_settings.neo4j_uri, "neo4j"
        )
        return await AuditGraphUseCase(adapter).execute(target, sample_limit=10, scope=scope)
    finally:
        await adapter.close()


@pytest.mark.neo4j_integration
async def test_gate_passes_clean_scoped_graph(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """expose-mcp passes over a clean scoped graph."""
    await _seed_clean_graph(neo4j_driver)
    report = await _run_scoped_audit(
        neo4j_settings,
        AuditScope(corpus="knowledge", source="agentic-architectural-patterns"),
    )
    policy = GatePolicyLoader(Path("gates.yaml")).load()
    result = GateEvaluatorUseCase(policy).evaluate("expose-mcp", report)

    assert report.state == OverallState.PASSED
    assert result.passed is True
    assert result.exit_code == 0
    assert result.scope == _NS1


@pytest.mark.neo4j_integration
async def test_gate_fails_same_namespace_duplicate(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """A duplicate in the scoped namespace fails the uniqueness dimension."""
    await _seed_duplicate_graph(neo4j_driver)
    report = await _run_scoped_audit(
        neo4j_settings,
        AuditScope(corpus="knowledge", source="agentic-architectural-patterns"),
    )
    policy = GatePolicyLoader(Path("gates.yaml")).load()
    result = GateEvaluatorUseCase(policy).evaluate("expose-mcp", report)

    assert result.passed is False
    assert result.exit_code == 10
    uniqueness = next(s for s in result.dimension_breakdown if s.dimension == "uniqueness")
    assert uniqueness.satisfied is False
    assert uniqueness.finding_total >= 1


@pytest.mark.neo4j_integration
async def test_gate_fails_coverage_warning(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """An orphan entity in scope fails the coverage dimension."""
    await _seed_orphan_graph(neo4j_driver)
    report = await _run_scoped_audit(
        neo4j_settings,
        AuditScope(corpus="knowledge", source="agentic-architectural-patterns"),
    )
    policy = GatePolicyLoader(Path("gates.yaml")).load()
    result = GateEvaluatorUseCase(policy).evaluate("expose-mcp", report)

    assert result.passed is False
    assert result.exit_code == 10
    coverage = next(s for s in result.dimension_breakdown if s.dimension == "coverage")
    assert coverage.satisfied is False
    assert coverage.finding_total >= 1


@pytest.mark.neo4j_integration
async def test_scoped_gate_ignores_other_namespace_violations(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """A clean ns1 gate passes even when ns2 contains a blocking violation."""
    await _seed_two_namespace_graph(neo4j_driver)
    report = await _run_scoped_audit(
        neo4j_settings,
        AuditScope(corpus="knowledge", source="agentic-architectural-patterns"),
    )
    policy = GatePolicyLoader(Path("gates.yaml")).load()
    result = GateEvaluatorUseCase(policy).evaluate("expose-mcp", report)

    assert report.scope == _NS1
    assert result.passed is True
    assert result.exit_code == 0
