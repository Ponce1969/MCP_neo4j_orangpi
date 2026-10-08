"""Application use case: evaluate the retrieval layer (Slice B, U3).

Why this layer has no absolute precision threshold
--------------------------------------------------
``precision@k`` is ``matched / len(contexts)`` per question and the retrieval
port returns at most ``k`` contexts, so the metric's ceiling is fixed by the
dataset and not by retrieval quality: with the committed dataset (12 ``current``
questions carrying 1-3 reference ids each, ``k=10``) a *perfect* retrieval scores
``mean(min(len(refs), k) / k) = 0.1917``. Any absolute threshold above that can
never be met — a hardcoded ``0.5`` warned on every run, whatever the quality.

So the only alert mechanism is the committed baseline (``precision_at_k_min``),
which is also what ``docs/spec/06-evaluation-and-readiness.md`` names as the
retrieval criterion (">= baseline"), and the measured value stays visible in the
rationale for a human to read. If the dataset changes, its ceiling changes with
it: recompute it with the formula above before trusting any absolute number.
"""

from __future__ import annotations

import uuid

from book_graph_rag.domain.evaluation_models import (
    EvaluationBaselineReport,
    EvaluationDataset,
    EvaluationLayerResult,
    LayerMetricValue,
    LayerRunMetadata,
    LayerStatus,
    RAGASSecondaryMetrics,
)
from book_graph_rag.ports.evaluation_baseline_port import EvaluationBaselinePort
from book_graph_rag.ports.evaluation_dataset_port import EvaluationDatasetPort
from book_graph_rag.ports.graph_retrieval_port import GraphRetrievalPort
from book_graph_rag.ports.ragas_runner_port import RAGASRunnerPort


class EvaluateRetrievalLayerUseCase:
    """Run layer 4: informative retrieval metrics; never blocks (R6.2).

    Informative means the status is never FAILED; the committed baseline still
    participates through ``precision_at_k_min`` (mechanism-first: missing or
    unfinalized baseline yields INCOMPLETE until a Phase 5 delta finalizes it).
    """

    _BASELINE_REPORT_PATH = "data/evaluation/retrieval_baseline.json"

    def __init__(
        self,
        dataset_port: EvaluationDatasetPort,
        retrieval_port: GraphRetrievalPort,
        ragas_port: RAGASRunnerPort,
        baseline_port: EvaluationBaselinePort,
        *,
        run_id: str | None = None,
        code_commit: str = "",
    ) -> None:
        self._dataset_port = dataset_port
        self._retrieval_port = retrieval_port
        self._ragas_port = ragas_port
        self._baseline_port = baseline_port
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
        baseline = self._baseline_port.load("retrieval")
        if baseline is None:
            return self._result(
                status=LayerStatus.INCOMPLETE,
                rationale=(
                    "retrieval baseline missing; mechanism-first INCOMPLETE until "
                    "a committed baseline is finalized"
                ),
                precision=0.0,
                ragas=None,
                baseline=None,
                warnings=(),
            )
        if not baseline.thresholds_finalized:
            return self._result(
                status=LayerStatus.INCOMPLETE,
                rationale="retrieval thresholds not finalized",
                precision=0.0,
                ragas=None,
                baseline=baseline,
                warnings=(),
            )
        try:
            dataset = self._dataset_port.load(dataset_id)
        except Exception as exc:  # noqa: BLE001
            return self._result(
                status=LayerStatus.UNREACHABLE,
                rationale=f"dataset load failed: {exc}",
                precision=0.0,
                ragas=None,
                baseline=baseline,
            )

        precisions: list[float] = []
        skipped_future: int = 0
        for record in dataset.records:
            if record.get("corpus") == "future":
                skipped_future += 1
                continue
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
                1 for ctx in contexts if ctx.chunk_id is not None and ctx.chunk_id in reference_ids
            )
            precisions.append(matched / len(contexts))

        precision = sum(precisions) / len(precisions) if precisions else 0.0

        ragas = await self._run_ragas(dataset, run_ragas=run_ragas)
        warnings: list[str] = []
        threshold = baseline.precision_at_k_min
        if threshold is not None and precision < threshold:
            warnings.append(
                f"retrieval precision below committed baseline threshold {threshold:.4f}"
            )
        if ragas is not None and not ragas.available:
            if ragas.notes:
                warnings.append(f"RAGAS unavailable: {ragas.notes}")
            else:
                warnings.append("RAGAS unavailable")
        # No drop branch here on purpose: this layer calls the RAGAS runner with
        # ``previous_metrics=None``, so ``drop_warning`` is always False and the branch was
        # unreachable — while its message named context_precision although the flag is computed on
        # faithfulness. The generation layer does pass a previous snapshot and keeps its own,
        # correctly worded warning.

        if not precisions:
            rationale = (
                f"no active (current) records (skipped {skipped_future} future-corpus records)"
            )
        else:
            rationale = f"retrieval layer informative (precision@k={precision:.4f})"
            if skipped_future:
                rationale += f" (skipped {skipped_future} future-corpus records)"

        return self._result(
            status=LayerStatus.PASSED,
            rationale=rationale,
            precision=precision,
            ragas=ragas,
            baseline=baseline,
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
        baseline: EvaluationBaselineReport | None,
        warnings: tuple[str, ...] = (),
    ) -> EvaluationLayerResult:
        threshold = baseline.precision_at_k_min if baseline is not None else None
        model_ids: tuple[str, ...] = ()
        if baseline is not None:
            model_ids = baseline.model_ids
        return EvaluationLayerResult(
            layer="retrieval",
            status=status,
            project_owned_metrics=(
                LayerMetricValue(
                    name="precision_at_k",
                    value=precision,
                    threshold=threshold,
                    comparator=">=",
                ),
            ),
            ragas_secondary=ragas,
            warnings=warnings,
            rationale=rationale,
            source_dataset_id="retrieval_dataset",
            baseline_report_path=self._BASELINE_REPORT_PATH if baseline is not None else None,
            run_metadata=LayerRunMetadata(
                run_id=self._run_id,
                code_commit=self._code_commit or (baseline.code_commit if baseline else ""),
                model_ids=model_ids,
            ),
        )
