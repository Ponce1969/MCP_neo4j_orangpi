"""Application use case: dispatch evaluate --layer to layer evaluators (Slice C)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from book_graph_rag.domain.evaluation_models import (
    EvaluationLayerResult,
    EvaluationReport,
    LayerStatus,
)

if TYPE_CHECKING:
    from book_graph_rag.application.evaluate_extraction_layer_use_case import (
        EvaluateExtractionLayerUseCase,
    )
    from book_graph_rag.application.evaluate_generation_layer_use_case import (
        EvaluateGenerationLayerUseCase,
    )
    from book_graph_rag.application.evaluate_resolution_layer_use_case import (
        EvaluateResolutionLayerUseCase,
    )
    from book_graph_rag.application.evaluate_retrieval_layer_use_case import (
        EvaluateRetrievalLayerUseCase,
    )


_LAYER_EXIT_CODES: dict[LayerStatus, int] = {
    LayerStatus.PASSED: 0,
    LayerStatus.WARNING: 0,
    LayerStatus.PENDING: 0,
    LayerStatus.NOT_EVALUATED: 0,
    LayerStatus.NOT_APPLICABLE: 0,
    LayerStatus.FAILED: 10,
    LayerStatus.INCOMPLETE: 11,
    LayerStatus.UNREACHABLE: 12,
}


class EvaluateCommandUseCase:
    """Thin orchestrator for ``book-graph-rag evaluate --layer`` (R8.1)."""

    def __init__(
        self,
        resolution_layer: EvaluateResolutionLayerUseCase,
        generation_layer: EvaluateGenerationLayerUseCase,
        retrieval_layer: EvaluateRetrievalLayerUseCase,
        extraction_layer: EvaluateExtractionLayerUseCase,
        *,
        run_id: str | None = None,
        code_commit: str = "",
        evaluated_at: datetime | None = None,
    ) -> None:
        self._resolution_layer = resolution_layer
        self._generation_layer = generation_layer
        self._retrieval_layer = retrieval_layer
        self._extraction_layer = extraction_layer
        self._run_id = run_id or uuid.uuid4().hex
        self._code_commit = code_commit
        self._evaluated_at = evaluated_at or datetime.now(UTC)

    def _exit_code(self, status: LayerStatus) -> int:
        return _LAYER_EXIT_CODES.get(status, 13)

    def _worst_exit_code(self, results: tuple[EvaluationLayerResult, ...]) -> int:
        """Return highest-priority exit code across layer results."""
        # Order: harness failure > unreachable > failed > incomplete > passed.
        priority = (13, 12, 10, 11, 0)
        codes = {self._exit_code(r.status) for r in results}
        for code in priority:
            if code in codes:
                return code
        return 0

    async def execute(
        self,
        *,
        layer: str,
        scope: str | None = None,
    ) -> tuple[EvaluationReport, int]:
        """Run the requested layer evaluator(s) and return the report + exit code."""
        if layer == "all":
            layer_results = [
                await self._resolution_layer.execute(),
                await self._retrieval_layer.execute(),
                await self._generation_layer.execute(),
                await self._extraction_layer.execute(),
            ]
            report = EvaluationReport(
                overall_status=EvaluationReport.derive_overall_status(
                    tuple(layer_results),
                    {"resolution", "generation"},
                ),
                layer_results=tuple(layer_results),
                run_id=self._run_id,
                code_commit=self._code_commit,
                evaluated_at=self._evaluated_at,
                scope=scope,
                rationale=f"evaluated layers: resolution, retrieval, generation, extraction",
            )
            return report, self._worst_exit_code(tuple(layer_results))

        if layer == "resolution":
            result = await self._resolution_layer.execute()
        elif layer == "generation":
            result = await self._generation_layer.execute()
        elif layer == "retrieval":
            result = await self._retrieval_layer.execute()
        elif layer == "extraction":
            result = await self._extraction_layer.execute()
        else:
            raise ValueError(f"unknown layer: {layer}")

        report = EvaluationReport(
            overall_status=result.status,
            layer_results=(result,),
            run_id=self._run_id,
            code_commit=self._code_commit,
            evaluated_at=self._evaluated_at,
            scope=scope,
            rationale=f"evaluated layer: {layer}",
        )
        return report, self._exit_code(result.status)
