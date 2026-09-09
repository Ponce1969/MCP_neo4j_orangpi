"""Unit tests for the readiness-gate evaluator use case."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from book_graph_rag.application.evaluate_gate_use_case import GateEvaluatorUseCase
from book_graph_rag.domain.audit_models import (
    AuditExecution,
    AuditFinding,
    AuditReport,
    AuditSummary,
    AuditTarget,
    OverallState,
    QueryState,
    Severity,
)
from book_graph_rag.domain.gate_models import GatePolicy, GateResult, UnknownGateError


def _target() -> AuditTarget:
    return AuditTarget(
        selector="bookgraph-neo4j",
        database="neo4j",
        scheme="bolt",
        host="localhost",
        port=7687,
        uri="bolt://localhost:7687",
    )


def _finding(
    rule_id: str,
    category: Any,
    severity: Severity,
    total: int = 1,
    query_state: QueryState = QueryState.EVALUATED,
) -> AuditFinding:
    return AuditFinding(
        rule_id=rule_id,
        category=category,
        severity=severity,
        total=total,
        query_state=query_state,
    )


def _report(
    state: OverallState,
    findings: tuple[AuditFinding, ...] = (),
    scope: str | None = None,
) -> AuditReport:
    summary = AuditSummary(
        blocking_total=sum(f.total for f in findings if f.severity == Severity.BLOCKING),
        warning_total=sum(f.total for f in findings if f.severity == Severity.WARNING),
        incomplete_total=sum(f.total for f in findings if f.severity == Severity.INCOMPLETE),
        evaluated_rule_count=sum(f.query_state == QueryState.EVALUATED for f in findings),
    )
    return AuditReport(
        target=_target(),
        state=state,
        scope=scope,
        findings=findings,
        summary=summary,
        execution=AuditExecution(state=state, exit_code={
            OverallState.PASSED: 0,
            OverallState.VIOLATIONS: 10,
            OverallState.INCOMPLETE: 11,
            OverallState.UNREACHABLE: 12,
            OverallState.FAILED: 13,
        }[state]),
    )


def _policy() -> GatePolicy:
    return GatePolicy.model_validate(
        {
            "version": "1.0.0",
            "gates": [
                {
                    "name": "expose-mcp",
                    "version": "1.0.0",
                    "required_dimensions": {
                        "hierarchy": "pass",
                        "endpoints": "pass",
                        "uniqueness": "pass",
                        "coverage": "pass",
                    },
                    "max_severity": "blocking",
                }
            ],
        }
    )


def test_gate_passes_clean_report() -> None:
    """A report with no findings satisfies every required dimension."""
    policy = _policy()
    report = _report(OverallState.PASSED)
    use_case = GateEvaluatorUseCase(policy)

    result = use_case.evaluate("expose-mcp", report)

    assert isinstance(result, GateResult)
    assert result.passed is True
    assert result.exit_code == 0
    assert result.overall_state == OverallState.PASSED
    assert all(status.satisfied for status in result.dimension_breakdown)


def test_gate_fails_on_blocking_finding() -> None:
    """Any BLOCKING finding fails the gate regardless of required dimensions."""
    policy = _policy()
    report = _report(
        OverallState.VIOLATIONS,
        findings=(
            _finding(
                "HIERARCHY_CHUNK_PARENT_REQUIRED", "hierarchy", Severity.BLOCKING
            ),
        ),
    )
    use_case = GateEvaluatorUseCase(policy)

    result = use_case.evaluate("expose-mcp", report)

    assert result.passed is False
    assert result.exit_code == 10
    assert result.overall_state == OverallState.VIOLATIONS
    assert "HIERARCHY_CHUNK_PARENT_REQUIRED" in result.blocking_findings


def test_gate_fails_when_required_dimension_has_findings() -> None:
    """A required dimension with nonzero findings fails the gate."""
    policy = _policy()
    report = _report(
        OverallState.VIOLATIONS,
        findings=(
            _finding("DUPLICATE_ENTITY_LOGICAL", "duplicates", Severity.WARNING),
        ),
    )
    use_case = GateEvaluatorUseCase(policy)

    result = use_case.evaluate("expose-mcp", report)

    assert result.passed is False
    assert result.exit_code == 10
    uniqueness_status = next(
        s for s in result.dimension_breakdown if s.dimension == "uniqueness"
    )
    assert uniqueness_status.finding_total == 1
    assert uniqueness_status.satisfied is False


def test_coverage_warning_fails_expose_mcp() -> None:
    """A coverage/WARNING finding fails the expose-mcp gate."""
    policy = _policy()
    report = _report(
        OverallState.VIOLATIONS,
        findings=(_finding("ENTITY_UNMENTIONED", "coverage", Severity.WARNING),),
    )
    use_case = GateEvaluatorUseCase(policy)

    result = use_case.evaluate("expose-mcp", report)

    assert result.passed is False
    assert result.exit_code == 10
    coverage_status = next(
        s for s in result.dimension_breakdown if s.dimension == "coverage"
    )
    assert coverage_status.satisfied is False


def test_provenance_omitted_passes_when_only_provenance_incomplete() -> None:
    """A gate that omits provenance passes despite INCOMPLETE provenance findings."""
    policy = GatePolicy.model_validate(
        {
            "version": "1.0.0",
            "gates": [
                {
                    "name": "no-provenance-gate",
                    "version": "1.0.0",
                    "required_dimensions": {"hierarchy": "pass"},
                    "max_severity": "blocking",
                }
            ],
        }
    )
    report = _report(
        OverallState.INCOMPLETE,
        findings=(_finding("PROVENANCE_ENTITY_MISSING", "provenance", Severity.INCOMPLETE),),
    )
    use_case = GateEvaluatorUseCase(policy)

    result = use_case.evaluate("no-provenance-gate", report)

    assert result.passed is True
    assert result.exit_code == 0


def test_incomplete_on_listed_dimension_returns_exit_11() -> None:
    """An INCOMPLETE audit with a listed required-dimension finding returns 11."""
    policy = GatePolicy.model_validate(
        {
            "version": "1.0.0",
            "gates": [
                {
                    "name": "needs-provenance",
                    "version": "1.0.0",
                    "required_dimensions": {"provenance": "pass"},
                    "max_severity": "blocking",
                }
            ],
        }
    )
    report = _report(
        OverallState.INCOMPLETE,
        findings=(_finding("PROVENANCE_ENTITY_MISSING", "provenance", Severity.INCOMPLETE),),
    )
    use_case = GateEvaluatorUseCase(policy)

    result = use_case.evaluate("needs-provenance", report)

    assert result.passed is False
    assert result.exit_code == 11
    assert result.overall_state == OverallState.INCOMPLETE


def test_unreachable_audit_never_satisfies_gate() -> None:
    """An UNREACHABLE audit fails the gate with exit code 12."""
    policy = _policy()
    report = _report(OverallState.UNREACHABLE)
    use_case = GateEvaluatorUseCase(policy)

    result = use_case.evaluate("expose-mcp", report)

    assert result.passed is False
    assert result.exit_code == 12
    assert result.overall_state == OverallState.UNREACHABLE


def test_failed_audit_never_satisfies_gate() -> None:
    """A FAILED audit fails the gate with exit code 13."""
    policy = _policy()
    report = _report(OverallState.FAILED)
    use_case = GateEvaluatorUseCase(policy)

    result = use_case.evaluate("expose-mcp", report)

    assert result.passed is False
    assert result.exit_code == 13
    assert result.overall_state == OverallState.FAILED


def test_unknown_gate_name_raises() -> None:
    """Requesting a gate not in the policy raises UnknownGateError."""
    policy = _policy()
    report = _report(OverallState.PASSED)
    use_case = GateEvaluatorUseCase(policy)

    with pytest.raises(UnknownGateError, match="not-a-gate"):
        use_case.evaluate("not-a-gate", report)


def test_scope_is_preserved_in_result() -> None:
    """The gate result carries the audit scope for traceability."""
    policy = _policy()
    report = _report(OverallState.PASSED, scope="knowledge:agentic-architectural-patterns")
    use_case = GateEvaluatorUseCase(policy)

    result = use_case.evaluate("expose-mcp", report)

    assert result.scope == "knowledge:agentic-architectural-patterns"
    assert result.passed is True


def test_evaluated_at_is_set() -> None:
    """Every gate result records its evaluation timestamp."""
    policy = _policy()
    report = _report(OverallState.PASSED)
    use_case = GateEvaluatorUseCase(policy)

    before = datetime.now(UTC)
    result = use_case.evaluate("expose-mcp", report)
    after = datetime.now(UTC)

    assert before <= result.evaluated_at <= after
