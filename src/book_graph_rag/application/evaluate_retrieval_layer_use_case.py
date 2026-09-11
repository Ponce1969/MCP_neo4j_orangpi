"""Application use case: evaluate the retrieval layer (Slice B, U3)."""

from __future__ import annotations

import uuid

from book_graph_rag.domain.evaluation_models import (
    EvaluationDataset,
    EvaluationLayerResult,
    LayerMetricValue,
    LayerRunMetadata,
    LayerStatus,
    RAGASSecondaryMetrics,
)
from book_graph_rag.ports.evaluation_dataset_port import EvaluationDatasetPort
from book_graph_rag.ports.graph_retrieval_port import GraphRetrievalPort
from book_graph_rag.ports.ragas_runner_port import RAGASRunnerPort


class EvaluateRetrievalLayerUseCase:
    """Run layer 4: informative retrieval metrics only; never blocks (R6.2)."""

    _PRECISION_WARNING_THRESHOLD: float = 0.5

    def __init__(
        self,
        dataset_port: EvaluationDatasetPort,
        retrieval_port: GraphRetrievalPort,
        ragas_port: RAGASRunnerPort,
        *,
        run_id: str | None = None,
        code_commit: str = "",
    ) -> None:
        self._dataset_port = dataset_port
        self._retrieval_port = retrieval_port
        self._ragas_port = ragas_port
        self._run_id = run_id or uuid.uuid4().hex
        self._code_commit = code_commit

    async def execute(
        self,
        *,
        dataset_id: str = "retrieval_dataset",
        detail_level: int = 1,
        run_ragas: bool = True,
    ) -> EvaluationLayerResult:
        """Evaluate layer 4 and return a WARNING-capable PASSED result."""
        try:
            dataset = self._dataset_port.load(dataset_id)
        except Exception as exc:  # noqa: BLE001
            return self._result(
                status=LayerStatus.UNREACHABLE,
                rationale=f"dataset load failed: {exc}",
                precision=0.0,
                ragas=None,
            )

        precisions: list[float] = []
        for record in dataset.records:
            question = str(record.get("question", ""))
            qtype = str(record.get("qtype", "local"))
            reference_ids = set(record.get("reference_context_ids", []) or [])
            contexts = await self._retrieval_port.fetch_contexts(
                question=question,
                qtype=qtype,  # type: ignore[arg-type]
                detail_level=detail_level,
            )
            if not contexts:
                precisions.append(0.0)
                continue
            matched = sum(
                1
                for ctx in contexts
                if any(ref_id in ctx for ref_id in reference_ids)
            )
            precisions.append(matched / len(contexts))

        precision = sum(precisions) / len(precisions) if precisions else 0.0

        ragas = await self._run_ragas(dataset, run_ragas=run_ragas)
        warnings: list[str] = []
        if precision < self._PRECISION_WARNING_THRESHOLD:
            warnings.append(f"low precision@k: {precision:.4f}")
        if ragas is not None:
            if not ragas.available:
                if ragas.notes:
                    warnings.append(f"RAGAS unavailable: {ragas.notes}")
                else:
                    warnings.append("RAGAS unavailable")
            elif ragas.drop_warning:
                warnings.append("RAGAS context_precision drop detected")

        return self._result(
            status=LayerStatus.PASSED,
            rationale=f"retrieval layer informative (precision@k={precision:.4f})",
            precision=precision,
            ragas=ragas,
            warnings=tuple(warnings),
        )

    async def _run_ragas(
        self,
        dataset: EvaluationDataset,
        *,
        run_ragas: bool,
    ) -> RAGASSecondaryMetrics | None:
        if not run_ragas:
            return None
        generation_results: tuple[tuple[str, str, tuple[str, ...]], ...] = ()
        return await self._ragas_port.run(
            dataset_id=dataset.dataset_id,
            generation_results=generation_results,
            previous_metrics=None,
        )

    def _result(
        self,
        *,
        status: LayerStatus,
        rationale: str,
        precision: float,
        ragas: RAGASSecondaryMetrics | None,
        warnings: tuple[str, ...] = (),
    ) -> EvaluationLayerResult:
        return EvaluationLayerResult(
            layer="retrieval",
            status=status,
            project_owned_metrics=(
                LayerMetricValue(
                    name="precision_at_k",
                    value=precision,
                    threshold=None,
                    comparator=">=",
                ),
            ),
            ragas_secondary=ragas,
            warnings=warnings,
            rationale=rationale,
            source_dataset_id="retrieval_dataset",
            baseline_report_path=None,
            run_metadata=LayerRunMetadata(
                run_id=self._run_id,
                code_commit=self._code_commit,
            ),
        )
