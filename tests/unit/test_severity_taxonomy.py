"""Coverage taxonomy and category-driven severity tests."""

from __future__ import annotations

import pytest

from book_graph_rag.domain.audit_models import (
    RULE_CATALOG,
    RULE_CATEGORY,
    AuditFinding,
    AuditReport,
    AuditTarget,
    OverallState,
    Severity,
    exit_code,
    severity_for_category,
)


def _target() -> AuditTarget:
    return AuditTarget(selector="bookgraph-neo4j", database="neo4j", scheme="bolt", host="db", uri="bolt://db")


def test_entity_unmentioned_is_coverage_warning() -> None:
    finding = AuditFinding(
        rule_id="ENTITY_UNMENTIONED",
        category=RULE_CATEGORY["ENTITY_UNMENTIONED"],
        severity=severity_for_category(RULE_CATEGORY["ENTITY_UNMENTIONED"]),
        total=1,
    )
    assert finding.category == "coverage"
    assert finding.severity == Severity.WARNING


def test_entity_isolated_related_is_coverage_warning() -> None:
    finding = AuditFinding(
        rule_id="ENTITY_ISOLATED_RELATED",
        category=RULE_CATEGORY["ENTITY_ISOLATED_RELATED"],
        severity=severity_for_category(RULE_CATEGORY["ENTITY_ISOLATED_RELATED"]),
        total=1,
    )
    assert finding.category == "coverage"
    assert finding.severity == Severity.WARNING


def test_provenance_rules_remain_provenance_incomplete() -> None:
    for rule_id in RULE_CATALOG:
        if rule_id.startswith("PROVENANCE_"):
            category = RULE_CATEGORY[rule_id]
            assert category == "provenance", rule_id
            assert severity_for_category(category) == Severity.INCOMPLETE, rule_id


def test_severity_for_category_mapping_all_categories() -> None:
    assert severity_for_category("coverage") == Severity.WARNING
    assert severity_for_category("duplicates") == Severity.WARNING
    assert severity_for_category("provenance") == Severity.INCOMPLETE
    assert severity_for_category("hierarchy") == Severity.BLOCKING
    assert severity_for_category("endpoints") == Severity.BLOCKING
    assert severity_for_category("pages") == Severity.BLOCKING
    with pytest.raises(KeyError):
        severity_for_category("unknown")  # type: ignore[arg-type]


def test_orphan_only_graph_classifies_violations_exit_10() -> None:
    findings = (
        AuditFinding(
            rule_id="ENTITY_UNMENTIONED", category="coverage",
            severity=Severity.WARNING, total=1,
        ),
        AuditFinding(
            rule_id="ENTITY_ISOLATED_RELATED", category="coverage",
            severity=Severity.WARNING, total=1,
        ),
    )
    report = AuditReport(target=_target(), state=OverallState.VIOLATIONS, findings=findings)
    assert report.state == OverallState.VIOLATIONS
    assert exit_code(report.state) == 10


def test_provenance_only_graph_remains_incomplete_exit_11() -> None:
    findings = (
        AuditFinding(
            rule_id="PROVENANCE_ENTITY_MISSING", category="provenance",
            severity=Severity.INCOMPLETE, total=1,
        ),
    )
    report = AuditReport(target=_target(), state=OverallState.INCOMPLETE, findings=findings)
    assert report.state == OverallState.INCOMPLETE
    assert exit_code(report.state) == 11
