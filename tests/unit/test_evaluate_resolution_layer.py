"""Tests for EvaluateResolutionLayerUseCase (Slice A)."""

from __future__ import annotations

from typing import Any

import pytest

from book_graph_rag.application.evaluate_resolution_layer_use_case import (
    EvaluateResolutionLayerUseCase,
)
from book_graph_rag.domain.evaluation_models import (
    EvaluationBaselineReport,
    EvaluationDataset,
    LayerStatus,
)
from book_graph_rag.ports.evaluation_baseline_port import EvaluationBaselinePort
from book_graph_rag.ports.evaluation_dataset_port import EvaluationDatasetPort


class _FakeDatasetPort(EvaluationDatasetPort):
    def __init__(self, records: tuple[dict[str, Any], ...] = ()) -> None:
        self._records = records

    def load(self, dataset_id: str) -> EvaluationDataset:
        return EvaluationDataset(dataset_id=dataset_id, records=self._records)

    def list_datasets(self) -> tuple[str, ...]:
        return ("resolution_dataset",)


class _FailingDatasetPort(EvaluationDatasetPort):
    def load(self, dataset_id: str) -> EvaluationDataset:
        raise RuntimeError("dataset unreachable")

    def list_datasets(self) -> tuple[str, ...]:
        return ()


class _FakeBaselinePort(EvaluationBaselinePort):
    def __init__(self, baseline: EvaluationBaselineReport | None) -> None:
        self._baseline = baseline

    def load(self, layer: str) -> EvaluationBaselineReport | None:
        return self._baseline


class _FakeHarness:
    def __init__(self, f1: float, hard_over_merge: float) -> None:
        self._f1 = f1
        self._hard_over_merge = hard_over_merge

    async def evaluate(self, *, model_id: str, input_variant: str) -> Any:
        class _Metrics:
            retrieval_f1 = self._f1
            hard_over_merge_rate = self._hard_over_merge
            model = model_id
            variant = input_variant

        return _Metrics()


def _baseline(
    *,
    finalized: bool = False,
    f1_min: float | None = None,
    hard_over_merge_max: float | None = None,
    metrics: dict[str, float] | None = None,
) -> EvaluationBaselineReport:
    return EvaluationBaselineReport(
        layer="resolution",
        dataset_id="resolution_dataset",
        dataset_sha256="0" * 64,
        thresholds_finalized=finalized,
        f1_min=f1_min,
        hard_over_merge_max=hard_over_merge_max,
        metrics=metrics or {"f1": 0.63, "hard_over_merge": 0.0},
        code_commit="abc123",
    )


@pytest.mark.asyncio
async def test_missing_baseline_is_incomplete() -> None:
    """Missing baseline -> INCOMPLETE."""
    use_case = EvaluateResolutionLayerUseCase(
        dataset_port=_FakeDatasetPort(),
        baseline_port=_FakeBaselinePort(None),
        harness=_FakeHarness(f1=0.8, hard_over_merge=0.0),
    )
    result = await use_case.execute()
    assert result.status == LayerStatus.INCOMPLETE
    assert "baseline" in result.rationale.lower()


@pytest.mark.asyncio
async def test_unfinalized_baseline_is_incomplete() -> None:
    """thresholds_finalized=False -> INCOMPLETE even with passing metrics."""
    use_case = EvaluateResolutionLayerUseCase(
        dataset_port=_FakeDatasetPort(),
        baseline_port=_FakeBaselinePort(_baseline(finalized=False)),
        harness=_FakeHarness(f1=1.0, hard_over_merge=0.0),
    )
    result = await use_case.execute()
    assert result.status == LayerStatus.INCOMPLETE
    assert result.project_owned_metrics[0].threshold is None


@pytest.mark.asyncio
async def test_hard_over_merge_fails_layer() -> None:
    """hard_over_merge > 0 -> FAILED."""
    use_case = EvaluateResolutionLayerUseCase(
        dataset_port=_FakeDatasetPort(),
        baseline_port=_FakeBaselinePort(
            _baseline(finalized=True, f1_min=0.5, hard_over_merge_max=0.0)
        ),
        harness=_FakeHarness(f1=0.8, hard_over_merge=0.1),
    )
    result = await use_case.execute()
    assert result.status == LayerStatus.FAILED
    assert any(m.name == "hard_over_merge" for m in result.project_owned_metrics)


@pytest.mark.asyncio
async def test_f1_regression_fails_layer() -> None:
    """f1 < f1_min -> FAILED."""
    use_case = EvaluateResolutionLayerUseCase(
        dataset_port=_FakeDatasetPort(),
        baseline_port=_FakeBaselinePort(
            _baseline(finalized=True, f1_min=0.9, hard_over_merge_max=0.0)
        ),
        harness=_FakeHarness(f1=0.8, hard_over_merge=0.0),
    )
    result = await use_case.execute()
    assert result.status == LayerStatus.FAILED
    metric = next(m for m in result.project_owned_metrics if m.name == "f1")
    assert metric.threshold == 0.9


@pytest.mark.asyncio
async def test_passing_metrics_returns_passed() -> None:
    """f1 >= f1_min and hard_over_merge == 0 -> PASSED."""
    use_case = EvaluateResolutionLayerUseCase(
        dataset_port=_FakeDatasetPort(),
        baseline_port=_FakeBaselinePort(
            _baseline(finalized=True, f1_min=0.6, hard_over_merge_max=0.0)
        ),
        harness=_FakeHarness(f1=0.8, hard_over_merge=0.0),
    )
    result = await use_case.execute()
    assert result.status == LayerStatus.PASSED
    assert result.source_dataset_id == "resolution_dataset"
    assert result.run_metadata.code_commit != ""


@pytest.mark.asyncio
async def test_dataset_load_failure_yields_unreachable() -> None:
    """A dataset load failure maps to UNREACHABLE."""
    use_case = EvaluateResolutionLayerUseCase(
        dataset_port=_FailingDatasetPort(),
        baseline_port=_FakeBaselinePort(_baseline()),
        harness=_FakeHarness(f1=0.8, hard_over_merge=0.0),
    )
    result = await use_case.execute()
    assert result.status == LayerStatus.UNREACHABLE
