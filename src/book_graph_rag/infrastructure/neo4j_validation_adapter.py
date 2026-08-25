"""Read-only Neo4j validation adapter."""
from __future__ import annotations

from typing import Any

from neo4j import AsyncGraphDatabase

from book_graph_rag.application.audit_graph_use_case import build_audit_target
from book_graph_rag.config import Settings
from book_graph_rag.domain.audit_models import AuditFinding
from book_graph_rag.domain.validation_models import (
    AuditRuleCategory,
    AuditRuleOutcome,
    CoverageEvidence,
    RuleEvidence,
)
from book_graph_rag.infrastructure.neo4j_audit_adapter import Neo4jAuditAdapter
from book_graph_rag.ports.graph_audit_port import GraphIntegrityAuditPort
from book_graph_rag.ports.validation_read_port import GraphValidationReadPort

_CATEGORY_MAP: dict[str, AuditRuleCategory] = {
    "hierarchy": AuditRuleCategory.HIERARCHY,
    "endpoints": AuditRuleCategory.ENDPOINT,
    "pages": AuditRuleCategory.PAGE,
    "provenance": AuditRuleCategory.PROVENANCE,
    "duplicates": AuditRuleCategory.RELATIONSHIP,
    "coverage": AuditRuleCategory.PROVENANCE,
}

_OUTCOME_MAP: dict[str, AuditRuleOutcome] = {
    "blocking": AuditRuleOutcome.FAIL,
    "warning": AuditRuleOutcome.WARNING,
    "incomplete": AuditRuleOutcome.INCOMPLETE,
}

_COVERAGE_QUERIES = {
    "chunks": (
        "MATCH (c:Chunk) WHERE c.book_id = $book_id "
        "WITH count(c) AS total, "
        "count(CASE WHEN c.chunk_index IS NOT NULL THEN 1 END) AS valid "
        "RETURN valid, total"
    ),
    "mentions": (
        "MATCH (c:Chunk)-[r:MENTIONS]->(:Entity) WHERE c.book_id = $book_id "
        "WITH count(r) AS total, "
        "count(CASE WHEN r.source_page IS NOT NULL AND r.chunk_index IS NOT NULL "
        "THEN 1 END) AS valid RETURN valid, total"
    ),
    "related_occurrences": (
        "MATCH (:Entity)-[r:RELATED]->(:Entity) "
        "WITH count(r) AS total, "
        "count(CASE WHEN r.source_page IS NOT NULL AND r.chunk_index IS NOT NULL "
        "THEN 1 END) AS valid RETURN valid, total"
    ),
}


def _map_finding(finding: AuditFinding) -> RuleEvidence:
    severity = finding.severity.value
    return RuleEvidence(
        rule_id=finding.rule_id,
        category=_CATEGORY_MAP[finding.category],
        mandatory=severity == "blocking",
        outcome=_OUTCOME_MAP[severity],
        expected={"total": 0},
        observed={
            "total": finding.total,
            "sample_keys": [sample.key for sample in finding.samples],
        },
        violation_count=finding.total,
        sample_limit=finding.sample_limit,
        sample_rows=tuple(dict(sample.properties) for sample in finding.samples),
        evidence_ref="audit://" + finding.rule_id,
    )


class Neo4jValidationAdapter(GraphValidationReadPort):
    """Read-only adapter composing the existing audit plus provenance coverage reads."""

    def __init__(
        self,
        settings: Settings,
        audit_port: GraphIntegrityAuditPort | None = None,
    ) -> None:
        self._settings = settings
        self._audit = audit_port or Neo4jAuditAdapter(settings)
        self._driver: Any = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
        )

    async def close(self) -> None:
        await self._driver.close()

    async def collect_rule_evidence(
        self, book_id: str, sample_limit: int
    ) -> tuple[RuleEvidence, ...]:
        if sample_limit < 0:
            raise ValueError("sample_limit must be non-negative")
        target = build_audit_target(
            "bookgraph-neo4j", self._settings.neo4j_uri, self._settings.neo4j_database
        )
        snapshot = await self._audit.collect_snapshot(target, sample_limit)
        return tuple(_map_finding(finding) for finding in snapshot.findings)

    async def collect_coverage(self, book_id: str) -> tuple[CoverageEvidence, ...]:
        evidence: list[CoverageEvidence] = []
        async with self._driver.session(
            database=self._settings.neo4j_database
        ) as session:
            for scope, query in _COVERAGE_QUERIES.items():
                rows = await session.execute_read(
                    self._read, query, {"book_id": book_id}
                )
                row = rows[0] if rows else {"valid": 0, "total": 0}
                valid = int(row.get("valid", 0) or 0)
                total = int(row.get("total", 0) or 0)
                evidence.append(self._coverage(scope, valid, total))
        return tuple(evidence)

    @staticmethod
    def _coverage(scope: str, valid: int, total: int) -> CoverageEvidence:
        if total == 0:
            return CoverageEvidence(
                scope=scope,
                valid=0,
                total=0,
                percentage=100.0,
                applicable=False,
                reason="no applicable records",
            )
        return CoverageEvidence(
            scope=scope, valid=valid, total=total, percentage=valid / total * 100
        )

    @staticmethod
    async def _read(tx: Any, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        result = await tx.run(query, params)
        rows: list[dict[str, Any]] = []
        async for row in result:
            rows.append(row.data() if hasattr(row, "data") else dict(row))
        return rows
