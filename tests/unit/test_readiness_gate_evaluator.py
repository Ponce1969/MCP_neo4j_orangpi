"""Tests for ReadinessGateEvaluatorUseCase (Slice C, T-C.1)."""

# mypy: disable-error-code="arg-type"

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest

from book_graph_rag.application.evaluate_generation_layer_use_case import (
    EvaluateGenerationLayerUseCase,
)
from book_graph_rag.config import Settings
from book_graph_rag.domain.audit_models import (
    AuditExecution,
    AuditReport,
    AuditTarget,
    OverallState,
)
from book_graph_rag.domain.evaluation_models import (
    EvaluationBaselineReport,
    EvaluationDataset,
    EvaluationLayerResult,
    LayerMetricValue,
    LayerRunMetadata,
    LayerStatus,
    RAGASSecondaryMetrics,
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
        self.calls: list[dict[str, object]] = []

    async def execute(self, **kwargs: object) -> EvaluationLayerResult:
        self.calls.append(kwargs)
        return self._result


class _ProviderFailureError(Exception):
    """Marker-carrying failure thrown through the real generation producer."""


class _MarkerGenerationDataset:
    def load(self, dataset_id: str) -> EvaluationDataset:
        return EvaluationDataset(
            dataset_id=dataset_id,
            records=(
                {
                    "question_id": "question-secret-17",
                    "question": "marker question",
                    "qtype": "global",
                    "reference_answer": "answer",
                    "contexts_hint": [],
                },
            ),
        )


class _MarkerGenerationRetrieval:
    async def answer_global(self, *, question: str, detail_level: int) -> Any:
        _ = question, detail_level
        raise _ProviderFailureError(_GATE_ERROR_MARKER)


class _MarkerGenerationBaseline:
    def load(self, layer: str) -> EvaluationBaselineReport:
        assert layer == "generation"
        return EvaluationBaselineReport(
            layer="generation",
            dataset_id="generation_dataset",
            dataset_sha256="0" * 64,
            thresholds_finalized=True,
            faithfulness_min=0.5,
            metrics={"faithfulness": 0.8},
        )


class _UnavailableRagas:
    async def run(
        self,
        *,
        dataset_id: str,
        generation_results: tuple[tuple[str, str, tuple[str, ...]], ...],
        previous_metrics: RAGASSecondaryMetrics | None = None,
    ) -> RAGASSecondaryMetrics:
        _ = dataset_id, generation_results, previous_metrics
        return RAGASSecondaryMetrics(available=False)


_GATE_ERROR_MARKER = "provider said 401 for api-key=secret-marker-xyz"


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
    warnings: tuple[str, ...] = (),
) -> EvaluationLayerResult:
    return EvaluationLayerResult(
        layer=layer,
        status=status,
        project_owned_metrics=metrics,
        rationale=rationale,
        warnings=warnings,
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
                required_layers=[RequiredLayer(layer=layer, blocking=True) for layer in required],
                optional_layers=[RequiredLayer(layer=layer, blocking=False) for layer in optional],
                audit_gate_ref="expose-mcp",
            ),
        ],
    )


def _make_use_case(
    policy: GatePolicy,
    audit_result: GateResult,
    layer_results: dict[str, EvaluationLayerResult],
    *,
    code_commit: str = "",
    model_ids: tuple[str, ...] = (),
) -> Any:
    from book_graph_rag.application.readiness_gate_evaluator_use_case import (
        ReadinessGateEvaluatorUseCase,
    )

    resolution = layer_results.get("resolution") or _layer_result("resolution", LayerStatus.PASSED)
    generation = layer_results.get("generation") or _layer_result("generation", LayerStatus.PASSED)
    retrieval = layer_results.get("retrieval") or _layer_result("retrieval", LayerStatus.PASSED)
    extraction = layer_results.get("extraction") or _layer_result("extraction", LayerStatus.PENDING)
    return ReadinessGateEvaluatorUseCase(
        gate_policy=policy,
        audit_evaluator=FakeAuditEvaluator(audit_result),
        resolution_layer=FakeLayerUseCase(resolution),
        generation_layer=FakeLayerUseCase(generation),
        retrieval_layer=FakeLayerUseCase(retrieval),
        extraction_layer=FakeLayerUseCase(extraction),
        code_commit=code_commit,
        model_ids=model_ids,
    )


async def test_the_report_names_the_code_and_the_models_that_produced_it() -> None:
    """A gate report whose commit is empty cannot be tied to the code that ran.

    The generation layer used to fall back to the *baseline's* commit — which names code that did
    not run — and the report's own metadata carried nothing at all, so the verdict was
    unattributable. Provenance either travels with the report or the audit is guesswork.
    """
    policy = _policy()
    use_case = _make_use_case(
        policy,
        _gate_result(OverallState.PASSED),
        {
            "resolution": _layer_result("resolution", LayerStatus.PASSED),
            "generation": _layer_result("generation", LayerStatus.PASSED),
        },
        code_commit="deadbee",
        model_ids=("deepseek-v4-flash", "paraphrase-multilingual-MiniLM-L12-v2"),
    )

    result = await use_case.execute("expose-mcp-readiness", _audit_report(OverallState.PASSED))

    assert result.run_metadata.code_commit == "deadbee"
    assert result.run_metadata.model_ids == (
        "deepseek-v4-flash",
        "paraphrase-multilingual-MiniLM-L12-v2",
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
    """Optional-layer WARNINGs appear in result.warnings, attributed to their layer, without
    failing the gate."""
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
    assert "retrieval: low precision" in result.warnings
    breakdown = {b.layer: b for b in result.layer_breakdown}
    assert breakdown["retrieval"].status == LayerStatus.WARNING


@pytest.mark.asyncio
async def test_warnings_are_attributed_to_the_layer_that_emitted_them() -> None:
    """A warning that does not name its layer turns two layers into one contradicting run.

    The 2026-10-16 readiness report showed `RAGAS drop in faithfulness vs baseline: …`
    next to `RAGAS unavailable: subprocess ragas failed: …`. The two came from two
    different layers' two different subprocess invocations — generation's drop and
    retrieval's deterministic empty-input failure — but the flattened list hid who spoke,
    so the pair read as one self-contradicting runner. Attribution is the same provenance
    rule the numbers follow: a warning must name its source."""
    policy = _policy()
    generation = _layer_result(
        "generation",
        LayerStatus.PASSED,
        warnings=("faithfulness drop vs baseline (project-owned metric): 0.8988 -> 0.7143",),
    )
    retrieval = _layer_result(
        "retrieval",
        LayerStatus.WARNING,
        warnings=("RAGAS unavailable: subprocess ragas failed: CalledProcessError(...)",),
    )
    use_case = _make_use_case(
        policy,
        _gate_result(OverallState.PASSED),
        {"generation": generation, "retrieval": retrieval},
    )

    result = await use_case.execute("expose-mcp-readiness", _audit_report())

    # Each warning carries its layer, and the pair is distinguishable in the report.
    assert result.warnings == (
        "generation: faithfulness drop vs baseline (project-owned metric): 0.8988 -> 0.7143",
        "retrieval: RAGAS unavailable: subprocess ragas failed: CalledProcessError(...)",
    )
    # Attribution changes neither the verdict nor the exit code.
    assert result.exit_code == 0
    assert result.passed is True


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
async def test_gate_reemits_redacted_warning_without_raw_exception_text() -> None:
    """A producer exception stays redacted when the real generation path reaches the gate."""
    key_id = "evaluation-test-v1"
    secret = "independent-test-key-that-is-long-enough"
    canonical = json.dumps(
        {"error_message": _GATE_ERROR_MARKER},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    fingerprint = hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
    generation = EvaluateGenerationLayerUseCase(
        dataset_port=_MarkerGenerationDataset(),
        retrieval_port=_MarkerGenerationRetrieval(),
        claim_port=object(),
        pairwise_port=object(),
        ragas_port=_UnavailableRagas(),
        baseline_port=_MarkerGenerationBaseline(),
        settings=Settings.model_validate(
            {
                "neo4j_uri": "bolt://localhost:7687",
                "neo4j_user": "neo4j",
                "neo4j_password": "password",
            }
        ),
        run_id="run-1",
        code_commit="abc123",
        hmac_key_id=key_id,
        hmac_secret=secret,
    )

    from book_graph_rag.application.readiness_gate_evaluator_use_case import (
        ReadinessGateEvaluatorUseCase,
    )

    use_case = ReadinessGateEvaluatorUseCase(
        gate_policy=_policy(),
        audit_evaluator=FakeAuditEvaluator(_gate_result(OverallState.PASSED)),
        resolution_layer=FakeLayerUseCase(_layer_result("resolution", LayerStatus.PASSED)),
        generation_layer=generation,
        retrieval_layer=FakeLayerUseCase(_layer_result("retrieval", LayerStatus.PASSED)),
        extraction_layer=FakeLayerUseCase(_layer_result("extraction", LayerStatus.PENDING)),
        run_id="run-1",
        code_commit="abc123",
    )

    result = await use_case.execute("expose-mcp-readiness", _audit_report())
    gate_json = result.model_dump_json()

    assert _GATE_ERROR_MARKER not in gate_json
    assert "_ProviderFailureError" in gate_json
    assert f"message_length={len(_GATE_ERROR_MARKER)}" in gate_json
    assert "question-secret-17" in gate_json
    assert fingerprint in gate_json


async def test_layer_breakdown_includes_required_and_optional() -> None:
    """The per-layer breakdown lists every required and optional layer."""
    policy = _policy()
    use_case = _make_use_case(policy, _gate_result(), {})
    result = await use_case.execute("expose-mcp-readiness", _audit_report())
    layers = {b.layer for b in result.layer_breakdown}
    assert layers == {"structure", "resolution", "generation", "extraction", "retrieval"}
    assert all(isinstance(b, ReadinessLayerStatus) for b in result.layer_breakdown)
