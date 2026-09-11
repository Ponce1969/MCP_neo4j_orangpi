"""Application use case: compose audit + evaluation layers into readiness (Slice C)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from book_graph_rag.domain.evaluation_models import (
    EvaluationLayerResult,
    EvaluationReport,
    LayerMetricValue,
    LayerRunMetadata,
    LayerStatus,
    ReadinessGatePolicy,
    ReadinessGateResult,
    ReadinessLayerStatus,
)
from book_graph_rag.domain.gate_models import GatePolicy, GateResult, UnknownGateError

if TYPE_CHECKING:
    from book_graph_rag.application.evaluate_extraction_layer_use_case import (
        EvaluateExtractionLayerUseCase,
    )
    from book_graph_rag.application.evaluate_gate_use_case import GateEvaluatorUseCase
    from book_graph_rag.application.evaluate_generation_layer_use_case import (
        EvaluateGenerationLayerUseCase,
    )
    from book_graph_rag.application.evaluate_resolution_layer_use_case import (
        EvaluateResolutionLayerUseCase,
    )
    from book_graph_rag.application.evaluate_retrieval_layer_use_case import (
        EvaluateRetrievalLayerUseCase,
    )
    from book_graph_rag.domain.audit_models import AuditReport


class ReadinessGateEvaluatorUseCase:
    """Compose audit gate + required/optional evaluation layers (R7)."""

    def __init__(
        self,
        gate_policy: GatePolicy,
        audit_evaluator: GateEvaluatorUseCase,
        resolution_layer: EvaluateResolutionLayerUseCase,
        generation_layer: EvaluateGenerationLayerUseCase,
        retrieval_layer: EvaluateRetrievalLayerUseCase,
        extraction_layer: EvaluateExtractionLayerUseCase,
        *,
        run_id: str | None = None,
        code_commit: str = "",
        evaluated_at: datetime | None = None,
    ) -> None:
        self._gate_policy = gate_policy
        self._audit_evaluator = audit_evaluator
        self._resolution_layer = resolution_layer
        self._generation_layer = generation_layer
        self._retrieval_layer = retrieval_layer
        self._extraction_layer = extraction_layer
        self._run_id = run_id or uuid.uuid4().hex
        self._code_commit = code_commit
        self._evaluated_at = evaluated_at or datetime.now(UTC)

    def _find_gate(self, gate_name: str) -> ReadinessGatePolicy:
        gate = next(
            (g for g in self._gate_policy.readiness_gates if g.name == gate_name),
            None,
        )
        if gate is None:
            raise UnknownGateError(gate_name)
        return gate

    @staticmethod
    def _audit_state_to_layer_status(audit_result: GateResult) -> LayerStatus:
        mapping: dict[str, LayerStatus] = {
            "passed": LayerStatus.PASSED,
            "violations": LayerStatus.FAILED,
            "incomplete": LayerStatus.INCOMPLETE,
            "unreachable": LayerStatus.UNREACHABLE,
            "failed": LayerStatus.FAILED,
        }
        return mapping.get(audit_result.overall_state.value, LayerStatus.FAILED)

    def _structure_layer_result(self, audit_result: GateResult) -> EvaluationLayerResult:
        status = self._audit_state_to_layer_status(audit_result)
        return EvaluationLayerResult(
            layer="structure",
            status=status,
            project_owned_metrics=(),
            rationale=f"audit gate '{audit_result.gate_name}': {audit_result.overall_state.value}",
            source_dataset_id="",
            baseline_report_path=None,
            run_metadata=LayerRunMetadata(
                run_id=self._run_id,
                code_commit=self._code_commit,
                evaluated_at=self._evaluated_at,
            ),
        )

    async def _run_layer(
        self,
        layer_name: str,
        gate: ReadinessGatePolicy,
        *,
        audit_result: GateResult | None = None,
    ) -> EvaluationLayerResult:
        match layer_name:
            case "structure":
                result = audit_result or self._audit_evaluator.evaluate(
                    gate.audit_gate_ref, self._current_audit_report
                )
                return self._structure_layer_result(result)
            case "extraction":
                return await self._extraction_layer.execute()
            case "resolution":
                return await self._resolution_layer.execute()
            case "retrieval":
                return await self._retrieval_layer.execute()
            case "generation":
                return await self._generation_layer.execute()
            case _:
                return EvaluationLayerResult(
                    layer=layer_name,  # type: ignore[arg-type]
                    status=LayerStatus.NOT_APPLICABLE,
                    project_owned_metrics=(),
                    rationale=f"unknown layer: {layer_name}",
                    source_dataset_id="",
                    baseline_report_path=None,
                    run_metadata=LayerRunMetadata(
                        run_id=self._run_id,
                        code_commit=self._code_commit,
                        evaluated_at=self._evaluated_at,
                    ),
                )

    @staticmethod
    def _is_baseline_incomplete(layer_result: EvaluationLayerResult) -> bool:
        """True when an INCOMPLETE status is caused by missing/unfinalized baseline."""
        if layer_result.status != LayerStatus.INCOMPLETE:
            return False
        if layer_result.baseline_report_path is None:
            return True
        return any(m.threshold is None for m in layer_result.project_owned_metrics)

    @staticmethod
    def _to_readiness_layer(layer_result: EvaluationLayerResult) -> ReadinessLayerStatus:
        return ReadinessLayerStatus(
            layer=layer_result.layer,
            status=layer_result.status,
            rationale=layer_result.rationale,
            measured=layer_result.project_owned_metrics,
            thresholds=tuple(
                LayerMetricValue(
                    name=m.name,
                    value=m.threshold if m.threshold is not None else 0.0,
                    threshold=m.threshold,
                    comparator=m.comparator,
                )
                for m in layer_result.project_owned_metrics
                if m.threshold is not None
            ),
        )

    def _derive_state(
        self,
        audit_result: GateResult,
        layer_results: tuple[EvaluationLayerResult, ...],
        required_layers: set[str],
    ) -> tuple[str, int]:
        # ``structure`` is a mirror of the audit gate; its failure is captured
        # by ``audit_result.overall_state`` below, so we exclude it from the
        # layer failure/incomplete checks to preserve audit semantics.
        non_structure_required = [
            r for r in layer_results
            if r.layer in required_layers and r.layer != "structure"
        ]

        baseline_incomplete = any(
            self._is_baseline_incomplete(r) for r in non_structure_required
        )
        any_failed = any(r.status == LayerStatus.FAILED for r in non_structure_required)
        any_incomplete = any(
            r.status
            in {
                LayerStatus.INCOMPLETE,
                LayerStatus.PENDING,
                LayerStatus.NOT_EVALUATED,
                LayerStatus.NOT_APPLICABLE,
                LayerStatus.UNREACHABLE,
            }
            for r in non_structure_required
        )

        if baseline_incomplete:
            return "incomplete", 11
        if any_failed:
            return "failed", 10
        if any_incomplete:
            return "incomplete", 11
        if audit_result.overall_state.value == "violations":
            return "violations", 10
        if audit_result.overall_state.value == "incomplete":
            return "incomplete", 11
        if audit_result.overall_state.value == "unreachable":
            return "unreachable", 12
        if audit_result.overall_state.value == "failed":
            return "failed", 13
        return "passed", 0

    def evaluate_audit_only(
        self,
        gate_name: str,
        audit_report: AuditReport,
        scope: str | None = None,
    ) -> GateResult:
        """Delegate to the existing audit gate evaluator (R3)."""
        _ = scope
        return self._audit_evaluator.evaluate(gate_name, audit_report)

    async def execute(
        self,
        gate_name: str,
        audit_report: AuditReport,
        scope: str | None = None,
    ) -> ReadinessGateResult:
        """Compose readiness result for ``gate_name`` over ``audit_report``."""
        gate = self._find_gate(gate_name)
        self._current_audit_report = audit_report

        audit_result = self._audit_evaluator.evaluate(gate.audit_gate_ref, audit_report)

        if audit_result.overall_state.value in {"unreachable", "failed"}:
            return ReadinessGateResult(
                schema_version="1.0.0",
                gate_name=gate.name,
                gate_version=gate.version,
                scope=scope,
                passed=False,
                overall_state=audit_result.overall_state.value,
                exit_code=audit_result.exit_code,
                rationale=f"audit {audit_result.overall_state.value}: {audit_result.rationale}",
                audit_gate_status=audit_result.overall_state.value,
                evaluation_status="not_run",
                layer_breakdown=(
                    self._to_readiness_layer(self._structure_layer_result(audit_result)),
                ),
                run_metadata=LayerRunMetadata(
                    run_id=self._run_id,
                    code_commit=self._code_commit,
                    evaluated_at=self._evaluated_at,
                ),
            )

        required_names: set[str] = {r.layer for r in gate.required_layers}

        layer_results: list[EvaluationLayerResult] = []
        for req in gate.required_layers:
            layer_results.append(
                await self._run_layer(req.layer, gate, audit_result=audit_result)
            )
        for opt in gate.optional_layers:
            layer_results.append(
                await self._run_layer(opt.layer, gate, audit_result=audit_result)
            )

        overall_state, exit_code = self._derive_state(
            audit_result, tuple(layer_results), required_names
        )

        warnings: list[str] = []
        for result in layer_results:
            warnings.extend(result.warnings)

        evaluation_status = EvaluationReport.derive_overall_status(
            tuple(layer_results), required_names
        ).value

        return ReadinessGateResult(
            schema_version="1.0.0",
            gate_name=gate.name,
            gate_version=gate.version,
            scope=scope,
            passed=overall_state == "passed",
            overall_state=overall_state,  # type: ignore[arg-type]
            exit_code=exit_code,
            rationale=self._rationale(overall_state, tuple(layer_results), required_names),
            audit_gate_status=audit_result.overall_state.value,
            evaluation_status=evaluation_status,
            layer_breakdown=tuple(
                self._to_readiness_layer(r) for r in layer_results
            ),
            run_metadata=LayerRunMetadata(
                run_id=self._run_id,
                code_commit=self._code_commit,
                evaluated_at=self._evaluated_at,
            ),
            warnings=tuple(warnings),
        )

    @staticmethod
    def _rationale(
        overall_state: str,
        layer_results: tuple[EvaluationLayerResult, ...],
        required_names: set[str],
    ) -> str:
        if overall_state == "passed":
            return "readiness gate passed; all required layers satisfied"
        failed = [
            r.layer for r in layer_results
            if r.layer in required_names and r.status == LayerStatus.FAILED
        ]
        incomplete = [
            r.layer for r in layer_results
            if r.layer in required_names
            and r.status
            in {
                LayerStatus.INCOMPLETE,
                LayerStatus.PENDING,
                LayerStatus.NOT_EVALUATED,
                LayerStatus.NOT_APPLICABLE,
                LayerStatus.UNREACHABLE,
            }
        ]
        parts: list[str] = [f"readiness gate {overall_state}"]
        if failed:
            parts.append(f"failed layers: {', '.join(failed)}")
        if incomplete:
            parts.append(f"incomplete layers: {', '.join(incomplete)}")
        return "; ".join(parts)
