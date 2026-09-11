"""Tests for EvaluateCommandUseCase (Slice C, T-C.2)."""

from __future__ import annotations

from typing import Any

import pytest

from book_graph_rag.domain.evaluation_models import (
    EvaluationLayerResult,
    EvaluationReport,
    LayerMetricValue,
    LayerRunMetadata,
    LayerStatus,
)


class FakeLayerUseCase:
    def __init__(
        self,
        result: EvaluationLayerResult,
        *,
        expected_kwargs: dict[str, object] | None = None,
    ) -> None:
        self._result = result
        self._expected_kwargs = expected_kwargs or {}
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute(self, **kwargs: object) -> EvaluationLayerResult:
        self.calls.append(("execute", kwargs))
        return self._result


def _layer_result(layer: str, status: LayerStatus) -> EvaluationLayerResult:
    return EvaluationLayerResult(
        layer=layer,  # type: ignore[arg-type]
        status=status,
        project_owned_metrics=(
            LayerMetricValue(name="metric", value=0.5, threshold=None, comparator=">="),
        ),
        rationale="ok",
        source_dataset_id=f"{layer}_dataset",
        run_metadata=LayerRunMetadata(run_id="run-1", code_commit="abc123"),
    )


def _make_use_case(
    *,
    resolution: LayerStatus = LayerStatus.PASSED,
    generation: LayerStatus = LayerStatus.PASSED,
    retrieval: LayerStatus = LayerStatus.PASSED,
    extraction: LayerStatus = LayerStatus.PENDING,
) -> Any:
    from book_graph_rag.application.evaluate_command_use_case import (
        EvaluateCommandUseCase,
    )

    return EvaluateCommandUseCase(
        resolution_layer=FakeLayerUseCase(_layer_result("resolution", resolution)),  # type: ignore[arg-type]
        generation_layer=FakeLayerUseCase(_layer_result("generation", generation)),  # type: ignore[arg-type]
        retrieval_layer=FakeLayerUseCase(_layer_result("retrieval", retrieval)),  # type: ignore[arg-type]
        extraction_layer=FakeLayerUseCase(_layer_result("extraction", extraction)),  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("layer", "expected_calls"),
    [
        ("resolution", {"resolution"}),
        ("retrieval", {"retrieval"}),
        ("generation", {"generation"}),
    ],
)
@pytest.mark.asyncio
async def test_dispatches_by_layer(layer: str, expected_calls: set[str]) -> None:
    """Each layer name routes to the matching use case."""
    use_case = _make_use_case()
    report, exit_code = await use_case.execute(layer=layer)
    assert isinstance(report, EvaluationReport)
    assert exit_code == 0
    # Inspect the correct fake
    fakes = {
        "resolution": use_case._resolution_layer,
        "generation": use_case._generation_layer,
        "retrieval": use_case._retrieval_layer,
        "extraction": use_case._extraction_layer,
    }
    for name, fake in fakes.items():
        if name in expected_calls:
            assert len(fake.calls) == 1, name
        else:
            assert fake.calls == [], name


@pytest.mark.asyncio
async def test_layer_all_dispatches_per_layer() -> None:
    """--layer all runs every evaluation layer."""
    use_case = _make_use_case()
    report, exit_code = await use_case.execute(layer="all")
    assert exit_code == 0
    assert len(use_case._resolution_layer.calls) == 1
    assert len(use_case._generation_layer.calls) == 1
    assert len(use_case._retrieval_layer.calls) == 1
    assert len(use_case._extraction_layer.calls) == 1
    assert len(report.layer_results) == 4


@pytest.mark.parametrize(
    ("status", "expected_exit"),
    [
        (LayerStatus.PASSED, 0),
        (LayerStatus.FAILED, 10),
        (LayerStatus.INCOMPLETE, 11),
        (LayerStatus.UNREACHABLE, 12),
    ],
)
@pytest.mark.asyncio
async def test_returns_layer_exit_code(status: LayerStatus, expected_exit: int) -> None:
    """Exit code follows the Phase 4 convention mapped from layer status."""
    use_case = _make_use_case(resolution=status)
    _, exit_code = await use_case.execute(layer="resolution")
    assert exit_code == expected_exit


@pytest.mark.asyncio
async def test_all_uses_worst_exit_code() -> None:
    """--layer all returns the highest-priority exit code across layers."""
    use_case = _make_use_case(
        resolution=LayerStatus.PASSED,
        generation=LayerStatus.INCOMPLETE,
        retrieval=LayerStatus.FAILED,
    )
    _, exit_code = await use_case.execute(layer="all")
    assert exit_code == 10


@pytest.mark.asyncio
async def test_unknown_layer_raises_value_error() -> None:
    """Unknown layer names fail fast."""
    use_case = _make_use_case()
    with pytest.raises(ValueError, match="unknown layer"):
        await use_case.execute(layer="nope")


@pytest.mark.asyncio
async def test_report_records_run_metadata() -> None:
    """The emitted report carries run id, code commit, and scope."""
    use_case = _make_use_case()
    report, _ = await use_case.execute(layer="resolution")
    assert report.run_id
    assert report.code_commit == ""
    assert report.overall_status == LayerStatus.PASSED
