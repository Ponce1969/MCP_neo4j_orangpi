"""Tests for Neo4jValidationAdapter."""
from __future__ import annotations

import pytest
from pydantic import SecretStr

from book_graph_rag.config import Settings
from book_graph_rag.domain.audit_models import (
    AuditFinding,
    AuditSnapshot,
    AuditTarget,
    Severity,
)
from book_graph_rag.domain.validation_models import (
    AuditRuleCategory,
    AuditRuleOutcome,
    CoverageEvidence,
)
from book_graph_rag.infrastructure.neo4j_validation_adapter import (
    Neo4jValidationAdapter,
    _map_finding,
)
from book_graph_rag.ports.graph_audit_port import GraphIntegrityAuditPort


class _FakeAuditPort(GraphIntegrityAuditPort):
    def __init__(self, snapshot: AuditSnapshot) -> None:
        self._snapshot = snapshot

    async def collect_snapshot(
        self, target: AuditTarget, sample_limit: int
    ) -> AuditSnapshot:
        return self._snapshot


def _settings() -> Settings:
    return Settings(
        neo4j_uri="bolt://db:7687",
        neo4j_user="neo4j",
        neo4j_password=SecretStr("secret"),
        neo4j_database="neo4j",
    )


def _blocking_finding() -> AuditFinding:
    return AuditFinding(
        rule_id="HIERARCHY_CHUNK_PARENT_REQUIRED",
        category="hierarchy",
        severity=Severity.BLOCKING,
        total=2,
        sample_limit=50,
    )


def test_map_finding_translates_audit_severity_to_rule_outcome() -> None:
    rule = _map_finding(_blocking_finding())
    assert rule.rule_id == "HIERARCHY_CHUNK_PARENT_REQUIRED"
    assert rule.category == AuditRuleCategory.HIERARCHY
    assert rule.mandatory is True
    assert rule.outcome == AuditRuleOutcome.FAIL
    assert rule.violation_count == 2
    assert rule.evidence_ref == "audit://HIERARCHY_CHUNK_PARENT_REQUIRED"


def test_map_finding_warning_is_not_mandatory() -> None:
    finding = _blocking_finding().model_copy(
        update={"severity": Severity.WARNING}
    )
    rule = _map_finding(finding)
    assert rule.mandatory is False
    assert rule.outcome == AuditRuleOutcome.WARNING


@pytest.mark.asyncio
async def test_collect_rule_evidence_maps_snapshot_findings() -> None:
    snapshot = AuditSnapshot(findings=(_blocking_finding(),))
    adapter = Neo4jValidationAdapter(_settings(), audit_port=_FakeAuditPort(snapshot))
    evidence = await adapter.collect_rule_evidence("book-1", 50)
    assert len(evidence) == 1
    assert evidence[0].outcome == AuditRuleOutcome.FAIL


@pytest.mark.asyncio
async def test_collect_rule_evidence_rejects_negative_sample_limit() -> None:
    adapter = Neo4jValidationAdapter(_settings(), audit_port=_FakeAuditPort(AuditSnapshot()))
    with pytest.raises(ValueError, match="sample_limit"):
        await adapter.collect_rule_evidence("book-1", -1)


def test_coverage_zero_total_is_explicitly_inapplicable() -> None:
    coverage = Neo4jValidationAdapter._coverage("chunks", 0, 0)
    assert coverage.applicable is False
    assert coverage.reason == "no applicable records"


def test_coverage_partial_is_below_100() -> None:
    coverage: CoverageEvidence = Neo4jValidationAdapter._coverage("chunks", 9, 10)
    assert coverage.percentage == 90.0
    assert coverage.outcome == AuditRuleOutcome.FAIL
