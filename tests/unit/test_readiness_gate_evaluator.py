"""Tests for ReadinessGateEvaluatorUseCase (Slice C, T-C.1)."""

from __future__ import annotations

import pytest

from book_graph_rag.domain.audit_models import (
    AuditExecution,
    AuditReport,
    AuditTarget,
    OverallState,
)
from book_graph_rag.domain.evaluation_models import (
    EvaluationLayerResult,
    LayerMetricValue,
    LayerRunMetadata,
    LayerStatus,
    ReadinessGatePolicy,
    ReadinessLayerStatus,
    RequiredLayer,
)
from book_graph_rag.domain.gate_models import GatePolicy, GateResult, ReadinessGate
from book_graph_rag.domain.gate_models import UnknownGateError as AuditUnknownGateError


class FakeAuditEvaluator:
    """Returns a configured GateResult without touching Phase 4 code."""

    def __init__(self, result: GateResult) -> None:
        self._result = result
        self.calls: list[tuple[str, AuditReport]] = []

    def evaluate(self, gate_name: str, report: AuditReport) -> GateResult:
        self.calls.append((gate_name, report))
        return self._result


class FakeLayerUseCase:
    def __init__(self, result: EvaluationLayerResult) -> None:
        self._result = result
        self.calls: list[tuple[dict[str, object], ...]] = []

    async def execute(self, **kwargs: object) -> EvaluationLayerResult:
        self.calls.append(kwargs)
        return self._result


def _audit_report(state: OverallState = OverallState.PASSED) -> AuditReport:
    return AuditReport(
        target=AuditTarget(
            selector="bookgraph-neo4j",
            database="neo4j",
            scheme="bolt",
            host="db",
            port=7687,
            uri="bolt://db:7687",
        ),
        state=state,
        findings=(),
        execution=AuditExecution(
            state=state,
            exit_code={
                OverallState.PASSED: 0,
                OverallState.VIOLATIONS: 10,
                OverallState.INCOMPLETE: 11,
                OverallState.UNREACHABLE: 12,
                OverallState.FAILED: 13,
            }[state],
        ),
    )


def _gate_result(
    state: OverallState = OverallState.PASSED,
    exit_code: int | None = None,
) -> GateResult:
    code = (
        exit_code
        if exit_code is not None
        else {
            OverallState.PASSED: 0,
            OverallState.VIOLATIONS: 10,
            OverallState.INCOMPLETE: 11,
            OverallState.UNREACHABLE: 12,
            OverallState.FAILED: 13,
        }[state]
    )
    return GateResult(
        gate_name="expose-mcp",
        gate_version="1.0.0",
        scope=None,
        passed=state == OverallState.PASSED,
        overall_state=state,
        exit_code=code,
        rationale="audit result",
        dimension_breakdown=(),
        blocking_findings=(),
    )


def _layer_result(
    layer: str,
    status: LayerStatus,
    rationale: str = "",
    *,
    baseline_path: str | None = None,
    metrics: tuple[LayerMetricValue, ...] = (),
) -> EvaluationLayerResult:
    return EvaluationLayerResult(
        layer=layer,  # type: ignore[arg-type]
        status=status,
        project_owned_metrics=metrics,
        rationale=rationale,
        source_dataset_id=f"{layer}_dataset",
        baseline_report_path=baseline_path,
        run_metadata=LayerRunMetadata(run_id="run-1", code_commit="abc123"),
    )


def _policy(
    *,
    required: tuple[str, ...] = ("structure", "resolution", "generation"),
    optional: tuple[str, ...] = ("extraction", "retrieval"),
) -> GatePolicy:
    return GatePolicy(
        version="1.0.0",
        gates=[
            ReadinessGate(
                name="expose-mcp",
                version="1.0.0",
                required_dimensions={"hierarchy": "pass"},
                max_severity="blocking",
            ),
        ],
        readiness_gates=[
            ReadinessGatePolicy(
                name="expose-mcp-readiness",
                version="1.0.0",
                required_layers=[
                    RequiredLayer(layer=layer, blocking=True)  # type: ignore[arg-type]
                    for layer in required
                ],
                optional_layers=[
                    RequiredLayer(layer=layer, blocking=False)  # type: ignore[arg-type]
                    for layer in optional
                ],
                audit_gate_ref="expose-mcp",
            ),
        ],
    )


def _make_use_case(
    policy: GatePolicy,
    audit_result: GateResult,
    layer_results: dict[str, EvaluationLayerResult],
):
    from book_graph_rag.application.readiness_gate_evaluator_use_case import (
        ReadinessGateEvaluatorUseCase,
    )

    return ReadinessGateEvaluatorUseCase(
        gate_policy=policy,
        audit_evaluator=FakeAuditEvaluator(audit_result),
        resolution_layer=FakeLayerUseCase(layer_results.get("resolution") or _layer_result("resolution", LayerStatus.PASSED)),
        generation_layer=FakeLayerUseCase(layer_results.get("generation") or _layer_result("generation", LayerStatus.PASSED)),
        retrieval_layer=FakeLayerUseCase(layer_results.get("retrieval") or _layer_result("retrieval", LayerStatus.PASSED)),
        extraction_layer=FakeLayerUseCase(layer_results.get("extraction") or _layer_result("extraction", LayerStatus.PENDING)),
    )


@pytest.mark.parametrize(
    ("audit_state", "resolution", "generation", "expected_exit", "expected_state"),
    [
        # all green
        (OverallState.PASSED, LayerStatus.PASSED, LayerStatus.PASSED, 0, "passed"),
        # audit violations block regardless of layers
        (OverallState.VIOLATIONS, LayerStatus.PASSED, LayerStatus.PASSED, 10, "violations"),
        # required layer failure
        (OverallState.PASSED, LayerStatus.FAILED, LayerStatus.PASSED, 10, "failed"),
        # baseline unfinalized/missing takes precedence over failed
        (OverallState.PASSED, LayerStatus.INCOMPLETE, LayerStatus.FAILED, 11, "incomplete"),
        # both incomplete
        (OverallState.PASSED, LayerStatus.INCOMPLETE, LayerStatus.INCOMPLETE, 11, "incomplete"),
        # audit unreachable short-circuits
        (OverallState.UNREACHABLE, LayerStatus.PASSED, LayerStatus.PASSED, 12, "unreachable"),
        # audit failed short-circuits
        (OverallState.FAILED, LayerStatus.PASSED, LayerStatus.PASSED, 13, "failed"),
        # not applicable required layer -> incomplete
        (OverallState.PASSED, LayerStatus.NOT_APPLICABLE, LayerStatus.PASSED, 11, "incomplete"),
    ],
)
@pytest.mark.asyncio
async def test_exit_code_matrix(
    audit_state: OverallState,
    resolution: LayerStatus,
    generation: LayerStatus,
    expected_exit: int,
    expected_state: str,
) -> None:
    """Exit-code derivation follows R7.3 precedence table."""
    policy = _policy()
    use_case = _make_use_case(
        policy,
        _gate_result(audit_state),
        {
            "resolution": _layer_result("resolution", resolution),
            "generation": _layer_result("generation", generation),
        },
    )
    result = await use_case.execute("expose-mcp-readiness", _audit_report(audit_state))
    assert result.exit_code == expected_exit
    assert result.overall_state == expected_state
    assert result.passed == (expected_exit == 0)


@pytest.mark.asyncio
async def test_short_circuit_on_unreachable_audit() -> None:
    """Audit UNREACHABLE/FAILED never evaluates layers."""
    policy = _policy()
    audit = FakeAuditEvaluator(_gate_result(OverallState.UNREACHABLE, 12))
    resolution = FakeLayerUseCase(_layer_result("resolution", LayerStatus.PASSED))
    generation = FakeLayerUseCase(_layer_result("generation", LayerStatus.PASSED))

    from book_graph_rag.application.readiness_gate_evaluator_use_case import (
        ReadinessGateEvaluatorUseCase,
    )

    use_case = ReadinessGateEvaluatorUseCase(
        gate_policy=policy,
        audit_evaluator=audit,
        resolution_layer=resolution,
        generation_layer=generation,
        retrieval_layer=FakeLayerUseCase(_layer_result("retrieval", LayerStatus.PASSED)),
        extraction_layer=FakeLayerUseCase(_layer_result("extraction", LayerStatus.PENDING)),
    )
    result = await use_case.execute("expose-mcp-readiness", _audit_report(OverallState.UNREACHABLE))
    assert result.exit_code == 12
    assert resolution.calls == []
    assert generation.calls == []


@pytest.mark.asyncio
async def test_unknown_gate_raises() -> None:
    """An unknown readiness gate name raises UnknownGateError with non-zero intent."""
    policy = _policy()
    use_case = _make_use_case(policy, _gate_result(), {})
    with pytest.raises(AuditUnknownGateError):
        await use_case.execute("not-a-gate", _audit_report())


@pytest.mark.asyncio
async def test_optional_layer_warnings_folded_without_blocking() -> None:
    """Optional-layer WARNINGs appear in result.warnings but do not fail the gate."""
    policy = _policy()
    retrieval = EvaluationLayerResult(
        layer="retrieval",
        status=LayerStatus.WARNING,
        project_owned_metrics=(
            LayerMetricValue(name="precision_at_k", value=0.3, threshold=None, comparator=">="),
        ),
        warnings=("low precision",),
        rationale="retrieval warning",
        source_dataset_id="retrieval_dataset",
        run_metadata=LayerRunMetadata(run_id="run-1", code_commit="abc123"),
    )
    use_case = _make_use_case(
        policy,
        _gate_result(OverallState.PASSED),
        {"retrieval": retrieval},
    )
    result = await use_case.execute("expose-mcp-readiness", _audit_report())
    assert result.exit_code == 0
    assert result.passed is True
    assert "low precision" in result.warnings
    breakdown = {b.layer: b for b in result.layer_breakdown}
    assert breakdown["retrieval"].status == LayerStatus.WARNING


@pytest.mark.asyncio
async def test_determinism() -> None:
    """Identical inputs produce identical ReadinessGateResult (canonical_json)."""
    from datetime import UTC, datetime

    policy = _policy()
    fixed_at = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)

    from book_graph_rag.application.readiness_gate_evaluator_use_case import (
        ReadinessGateEvaluatorUseCase,
    )

    use_case = ReadinessGateEvaluatorUseCase(
        gate_policy=policy,
        audit_evaluator=FakeAuditEvaluator(_gate_result()),
        resolution_layer=FakeLayerUseCase(_layer_result("resolution", LayerStatus.PASSED)),
        generation_layer=FakeLayerUseCase(_layer_result("generation", LayerStatus.PASSED)),
        retrieval_layer=FakeLayerUseCase(_layer_result("retrieval", LayerStatus.PASSED)),
        extraction_layer=FakeLayerUseCase(_layer_result("extraction", LayerStatus.PENDING)),
        run_id="run-1",
        evaluated_at=fixed_at,
    )
    audit_report = _audit_report()
    first = await use_case.execute("expose-mcp-readiness", audit_report)
    second = await use_case.execute("expose-mcp-readiness", audit_report)
    assert first.canonical_json() == second.canonical_json()


@pytest.mark.asyncio
async def test_audit_gate_is_delegated_not_reimplemented() -> None:
    """Readiness composition calls the existing GateEvaluatorUseCase for the audit gate."""
    policy = _policy()
    audit = FakeAuditEvaluator(_gate_result(OverallState.PASSED))

    from book_graph_rag.application.readiness_gate_evaluator_use_case import (
        ReadinessGateEvaluatorUseCase,
    )

    use_case = ReadinessGateEvaluatorUseCase(
        gate_policy=policy,
        audit_evaluator=audit,
        resolution_layer=FakeLayerUseCase(_layer_result("resolution", LayerStatus.PASSED)),
        generation_layer=FakeLayerUseCase(_layer_result("generation", LayerStatus.PASSED)),
        retrieval_layer=FakeLayerUseCase(_layer_result("retrieval", LayerStatus.PASSED)),
        extraction_layer=FakeLayerUseCase(_layer_result("extraction", LayerStatus.PENDING)),
    )
    audit_report = _audit_report()
    await use_case.execute("expose-mcp-readiness", audit_report)
    assert len(audit.calls) == 1
    assert audit.calls[0][0] == "expose-mcp"
    assert audit.calls[0][1] is audit_report


@pytest.mark.asyncio
async def test_layer_breakdown_includes_required_and_optional() -> None:
    """The per-layer breakdown lists every required and optional layer."""
    policy = _policy()
    use_case = _make_use_case(policy, _gate_result(), {})
    result = await use_case.execute("expose-mcp-readiness", _audit_report())
    layers = {b.layer for b in result.layer_breakdown}
    assert layers == {"structure", "resolution", "generation", "extraction", "retrieval"}
    assert all(isinstance(b, ReadinessLayerStatus) for b in result.layer_breakdown)
