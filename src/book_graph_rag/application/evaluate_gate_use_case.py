"""Evaluate a readiness gate against a completed audit report."""

from __future__ import annotations

from book_graph_rag.domain.audit_models import (
    RULE_CATEGORY,
    AuditReport,
    OverallState,
    QueryState,
    Severity,
    exit_code,
)
from book_graph_rag.domain.gate_models import (
    GateDimension,
    GateDimensionStatus,
    GatePolicy,
    GateResult,
    ReadinessGate,
    UnknownGateError,
)

_DIMENSIONS: tuple[GateDimension, ...] = (
    "hierarchy",
    "endpoints",
    "provenance",
    "uniqueness",
    "coverage",
)

_DIMENSION_MAP: dict[str, GateDimension] = {
    "hierarchy": "hierarchy",
    "endpoints": "endpoints",
    "provenance": "provenance",
    "duplicates": "uniqueness",
    "coverage": "coverage",
}


class GateEvaluatorUseCase:
    """Consume an ``AuditReport`` and produce a deterministic ``GateResult``."""

    def __init__(self, policy: GatePolicy) -> None:
        self._policy = policy

    def evaluate(self, gate_name: str, report: AuditReport) -> GateResult:
        """Return the gate result for ``gate_name`` over ``report``.

        Raises ``UnknownGateError`` when the gate name is absent from the policy.
        """
        gate = next((g for g in self._policy.gates if g.name == gate_name), None)
        if gate is None:
            raise UnknownGateError(gate_name)

        if report.state in (OverallState.UNREACHABLE, OverallState.FAILED):
            return self._terminal_result(gate, report)

        totals_by_dim: dict[GateDimension, int] = dict.fromkeys(_DIMENSIONS, 0)
        for finding in report.findings:
            is_blocking = finding.severity == Severity.BLOCKING
            is_evaluated = finding.query_state == QueryState.EVALUATED
            if not is_blocking and not is_evaluated:
                continue
            category = RULE_CATEGORY.get(finding.rule_id)
            if category is None:
                continue
            dim = _DIMENSION_MAP.get(category)
            if dim is not None:
                totals_by_dim[dim] += finding.total

        blocking = tuple(
            finding.rule_id
            for finding in report.findings
            if finding.severity == Severity.BLOCKING and finding.total
        )

        breakdown: list[GateDimensionStatus] = []
        all_required_pass = True
        for dim, req in gate.required_dimensions.items():
            total = totals_by_dim[dim]
            satisfied = req == "pass" and total == 0
            if not satisfied:
                all_required_pass = False
            breakdown.append(
                GateDimensionStatus(dimension=dim, finding_total=total, satisfied=satisfied)
            )

        if report.state == OverallState.INCOMPLETE and any(
            totals_by_dim[dim] for dim in gate.required_dimensions
        ):
            state = OverallState.INCOMPLETE
        elif blocking or not all_required_pass:
            state = OverallState.VIOLATIONS
        else:
            state = OverallState.PASSED

        return GateResult(
            gate_name=gate.name,
            gate_version=gate.version,
            scope=report.scope,
            passed=state == OverallState.PASSED,
            overall_state=state,
            exit_code=exit_code(state),
            rationale=self._rationale(gate, state, breakdown, blocking),
            dimension_breakdown=tuple(breakdown),
            blocking_findings=blocking,
        )

    @staticmethod
    def _terminal_result(gate: ReadinessGate, report: AuditReport) -> GateResult:
        state = report.state
        return GateResult(
            gate_name=gate.name,
            gate_version=gate.version,
            scope=report.scope,
            passed=False,
            overall_state=state,
            exit_code=exit_code(state),
            rationale=f"Audit is {state.value}; gate cannot be satisfied",
            dimension_breakdown=(),
            blocking_findings=(),
        )

    @staticmethod
    def _rationale(
        gate: ReadinessGate,
        state: OverallState,
        breakdown: list[GateDimensionStatus],
        blocking: tuple[str, ...],
    ) -> str:
        if state == OverallState.PASSED:
            dims = ", ".join(sorted(gate.required_dimensions))
            return f"Gate '{gate.name}' passed; required dimensions satisfied: {dims}"
        failed_dims = [b.dimension for b in breakdown if not b.satisfied]
        parts = [f"Gate '{gate.name}' {state.value}"]
        if blocking:
            parts.append(f"blocking findings: {', '.join(blocking)}")
        if failed_dims:
            parts.append(f"failed dimensions: {', '.join(failed_dims)}")
        return "; ".join(parts)
