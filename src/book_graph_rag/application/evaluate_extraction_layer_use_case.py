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
    """Layer 2 stub: reported as PENDING/NOT_EVALUATED, never blocks (spec 06 §11).

    Deferred because a fidelity number needs a labeled extraction dataset this repository does not
    have; the rationale is written in ``docs/spec/06-evaluation-and-readiness.md``. Connectivity —
    REQ-PROV-03's ``:MENTIONS`` coverage — is measurable without labels and is a separate
    instrument.
    """

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
            rationale=(
                "deferred: layer 2 is optional/informative per spec 06 §11 (no labeled extraction "
                "dataset exists, so fidelity cannot be measured; never blocks)"
            ),
            source_dataset_id="",
            baseline_report_path=None,
            run_metadata=LayerRunMetadata(
                run_id=self._run_id,
                code_commit=self._code_commit,
            ),
        )
