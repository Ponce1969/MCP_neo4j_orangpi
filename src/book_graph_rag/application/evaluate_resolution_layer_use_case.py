"""Application use case: evaluate the entity-resolution layer (Slice A)."""

from __future__ import annotations

import uuid
from typing import Any

from book_graph_rag.domain.evaluation_models import (
    EvaluationBaselineReport,
    EvaluationLayerResult,
    LayerMetricValue,
    LayerRunMetadata,
    LayerStatus,
)
from book_graph_rag.ports.evaluation_baseline_port import EvaluationBaselinePort
from book_graph_rag.ports.evaluation_dataset_port import EvaluationDatasetPort


class EvaluateResolutionLayerUseCase:
    """Run layer 3 deterministically and report F1 + hard over-merge (R4)."""

    def __init__(
        self,
        dataset_port: EvaluationDatasetPort,
        baseline_port: EvaluationBaselinePort,
        harness: Any,
        *,
        run_id: str | None = None,
        code_commit: str = "",
    ) -> None:
        self._dataset_port = dataset_port
        self._baseline_port = baseline_port
        self._harness = harness
        self._run_id = run_id or uuid.uuid4().hex
        self._code_commit = code_commit

    async def execute(
        self, *, dataset_id: str = "resolution_dataset"
    ) -> EvaluationLayerResult:
        """Evaluate layer 3 against the committed baseline."""
        try:
            self._dataset_port.load(dataset_id)
        except Exception as exc:  # noqa: BLE001
            return self._result(
                status=LayerStatus.UNREACHABLE,
                rationale=f"dataset load failed: {exc}",
                metrics=(),
                baseline=None,
            )

        baseline = self._baseline_port.load("resolution")
        if baseline is None:
            return self._result(
                status=LayerStatus.INCOMPLETE,
                rationale="resolution baseline missing",
                metrics=(),
                baseline=None,
            )

        if not baseline.thresholds_finalized:
            metrics = await self._run_harness(baseline)
            return self._result(
                status=LayerStatus.INCOMPLETE,
                rationale="resolution thresholds not finalized",
                metrics=metrics,
                baseline=baseline,
            )

        metrics = await self._run_harness(baseline)
        f1_metric = next(m for m in metrics if m.name == "f1")
        over_metric = next(m for m in metrics if m.name == "hard_over_merge")

        if over_metric.threshold is None or over_metric.value > over_metric.threshold:
            return self._result(
                status=LayerStatus.FAILED,
                rationale="hard over-merge > threshold",
                metrics=metrics,
                baseline=baseline,
            )
        if f1_metric.threshold is None or f1_metric.value < f1_metric.threshold:
            return self._result(
                status=LayerStatus.FAILED,
                rationale=f"f1 {f1_metric.value:.4f} < baseline {f1_metric.threshold:.4f}",
                metrics=metrics,
                baseline=baseline,
            )
        return self._result(
            status=LayerStatus.PASSED,
            rationale="resolution layer passed baseline checks",
            metrics=metrics,
            baseline=baseline,
        )

    async def _run_harness(
        self, baseline: EvaluationBaselineReport
    ) -> tuple[LayerMetricValue, ...]:
        model_id = baseline.model_ids[0] if baseline.model_ids else ""
        metrics = await self._harness.evaluate(model_id=model_id, input_variant="A")
        f1_threshold = baseline.f1_min if baseline.thresholds_finalized else None
        over_threshold = baseline.hard_over_merge_max if baseline.thresholds_finalized else None
        return (
            LayerMetricValue(
                name="f1",
                value=float(metrics.retrieval_f1),
                threshold=f1_threshold,
                comparator=">=",
            ),
            LayerMetricValue(
                name="hard_over_merge",
                value=float(metrics.hard_over_merge_rate),
                threshold=over_threshold,
                comparator="<=",
            ),
        )

    def _result(
        self,
        *,
        status: LayerStatus,
        rationale: str,
        metrics: tuple[LayerMetricValue, ...],
        baseline: EvaluationBaselineReport | None,
    ) -> EvaluationLayerResult:
        return EvaluationLayerResult(
            layer="resolution",
            status=status,
            project_owned_metrics=metrics,
            source_dataset_id="resolution_dataset",
            baseline_report_path="data/evaluation/resolution_baseline.json"
            if baseline
            else None,
            rationale=rationale,
            run_metadata=LayerRunMetadata(
                run_id=self._run_id,
                code_commit=self._code_commit or (baseline.code_commit if baseline else ""),
                model_ids=baseline.model_ids if baseline else (),
            ),
        )
