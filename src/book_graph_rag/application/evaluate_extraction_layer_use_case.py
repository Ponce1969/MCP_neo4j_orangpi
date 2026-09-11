"""Application use case: evaluate the extraction layer (Slice B, U4)."""

from __future__ import annotations

import uuid

from book_graph_rag.domain.evaluation_models import (
    EvaluationLayerResult,
    LayerMetricValue,
    LayerRunMetadata,
    LayerStatus,
)


class EvaluateExtractionLayerUseCase:
    """Layer 2 stub: reported as PENDING/NOT_EVALUATED, never blocks (R6.1)."""

    def __init__(self, *, run_id: str | None = None, code_commit: str = "") -> None:
        self._run_id = run_id or uuid.uuid4().hex
        self._code_commit = code_commit

    async def execute(self) -> EvaluationLayerResult:
        """Return a deterministic PENDING result."""
        return EvaluationLayerResult(
            layer="extraction",
            status=LayerStatus.PENDING,
            project_owned_metrics=(
                LayerMetricValue(
                    name="coverage",
                    value=0.0,
                    threshold=None,
                    comparator=">=",
                ),
            ),
            rationale="deferred per R6.1",
            source_dataset_id="",
            baseline_report_path=None,
            run_metadata=LayerRunMetadata(
                run_id=self._run_id,
                code_commit=self._code_commit,
            ),
        )
