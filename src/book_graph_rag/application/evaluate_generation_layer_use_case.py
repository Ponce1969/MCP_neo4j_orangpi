"""Application use case: evaluate the generation layer (Slice B, U2)."""

from __future__ import annotations

import uuid
from typing import Any

from book_graph_rag.config import Settings
from book_graph_rag.domain.evaluation_models import (
    AtomicClaim,
    EvaluationBaselineReport,
    EvaluationDataset,
    EvaluationLayerResult,
    LayerMetricValue,
    LayerRunMetadata,
    LayerStatus,
    RAGASSecondaryMetrics,
)
from book_graph_rag.ports.claim_validator_port import ClaimValidatorPort
from book_graph_rag.ports.evaluation_baseline_port import EvaluationBaselinePort
from book_graph_rag.ports.evaluation_dataset_port import EvaluationDatasetPort
from book_graph_rag.ports.graph_retrieval_port import GraphRetrievalPort
from book_graph_rag.ports.pairwise_judge_port import PairwiseJudgePort
from book_graph_rag.ports.ragas_runner_port import RAGASRunnerPort


class EvaluateGenerationLayerUseCase:
    """Run layer 5: claim validation, pairwise judge, RAGAS secondary (R5)."""

    def __init__(
        self,
        dataset_port: EvaluationDatasetPort,
        retrieval_port: GraphRetrievalPort,
        claim_port: ClaimValidatorPort,
        pairwise_port: PairwiseJudgePort,
        ragas_port: RAGASRunnerPort,
        baseline_port: EvaluationBaselinePort,
        settings: Settings,
        *,
        run_id: str | None = None,
        code_commit: str = "",
    ) -> None:
        self._dataset_port = dataset_port
        self._retrieval_port = retrieval_port
        self._claim_port = claim_port
        self._pairwise_port = pairwise_port
        self._ragas_port = ragas_port
        self._baseline_port = baseline_port
        self._settings = settings
        self._run_id = run_id or uuid.uuid4().hex
        self._code_commit = code_commit

    async def execute(
        self,
        *,
        dataset_id: str = "generation_dataset",
        detail_level: int = 1,
        run_ragas: bool = True,
    ) -> EvaluationLayerResult:
        """Evaluate layer 5 against the committed baseline."""
        try:
            dataset = self._dataset_port.load(dataset_id)
        except Exception as exc:  # noqa: BLE001
            return self._result(
                status=LayerStatus.UNREACHABLE,
                rationale=f"dataset load failed: {exc}",
                metrics=(),
                baseline=None,
                ragas=None,
                pairwise_wins=0,
                pairwise_total=0,
            )

        baseline = self._baseline_port.load("generation")
        if baseline is None:
            return self._result(
                status=LayerStatus.INCOMPLETE,
                rationale="generation baseline missing",
                metrics=(),
                baseline=None,
                ragas=None,
                pairwise_wins=0,
                pairwise_total=0,
            )

        self._last_per_question = per_question_results = await self._evaluate_questions(
            dataset, detail_level=detail_level
        )

        total_claims = sum(len(r.claims) for r in per_question_results)
        supported_claims = sum(
            1
            for r in per_question_results
            for c in r.claims
            if c.verdict == "support"
        )
        faithfulness = supported_claims / total_claims if total_claims > 0 else 0.0

        pairwise_wins = sum(1 for r in per_question_results if r.pairwise.verdict == "graph_wins")
        pairwise_total = len(per_question_results)
        pairwise_win_rate = pairwise_wins / pairwise_total if pairwise_total > 0 else 0.0

        faithfulness_min = baseline.faithfulness_min if baseline.thresholds_finalized else None
        metrics = (
            LayerMetricValue(
                name="faithfulness",
                value=faithfulness,
                threshold=faithfulness_min,
                comparator=">=",
            ),
            LayerMetricValue(
                name="pairwise_win_rate",
                value=pairwise_win_rate,
                threshold=None,
                comparator=">=",
            ),
        )

        ragas = await self._run_ragas(baseline, per_question_results, run_ragas=run_ragas)
        warnings: list[str] = []
        if not ragas.available:
            warnings.append(f"RAGAS unavailable: {ragas.notes}")
        elif ragas.drop_warning:
            warnings.append("RAGAS drop detected vs baseline")

        if not baseline.thresholds_finalized:
            return self._result(
                status=LayerStatus.INCOMPLETE,
                rationale="generation thresholds not finalized",
                metrics=metrics,
                baseline=baseline,
                ragas=ragas,
                pairwise_wins=pairwise_wins,
                pairwise_total=pairwise_total,
                warnings=tuple(warnings),
            )

        faithfulness_metric = metrics[0]
        if (
            faithfulness_metric.threshold is not None
            and faithfulness_metric.value < faithfulness_metric.threshold
        ):
            return self._result(
                status=LayerStatus.FAILED,
                rationale=(
                    f"faithfulness {faithfulness_metric.value:.4f} < "
                    f"baseline {faithfulness_metric.threshold:.4f}"
                ),
                metrics=metrics,
                baseline=baseline,
                ragas=ragas,
                pairwise_wins=pairwise_wins,
                pairwise_total=pairwise_total,
                warnings=tuple(warnings),
            )

        return self._result(
            status=LayerStatus.PASSED,
            rationale="generation layer passed baseline checks",
            metrics=metrics,
            baseline=baseline,
            ragas=ragas,
            pairwise_wins=pairwise_wins,
            pairwise_total=pairwise_total,
            warnings=tuple(warnings),
        )

    async def _evaluate_questions(
        self, dataset: EvaluationDataset, *, detail_level: int
    ) -> tuple[_PerQuestionResult, ...]:
        results: list[_PerQuestionResult] = []
        for record in dataset.records:
            question_id = str(record.get("question_id", ""))
            question = str(record.get("question", ""))
            qtype = str(record.get("qtype", "global"))
            reference_answer = str(record.get("reference_answer", ""))

            contexts = await self._retrieval_port.fetch_contexts(
                question=question,
                qtype=qtype,  # type: ignore[arg-type]
                detail_level=detail_level,
            )
            answer = await self._retrieval_port.compose_answer(
                question=question, contexts=contexts
            )
            extracted = await self._claim_port.extract_claims(
                question_id=question_id,
                answer=answer,
                contexts=contexts,
                extractor_model_id=self._settings.query_llm_model_name or "extractor",
            )
            verified = await self._claim_port.verify_claims(
                question_id=question_id,
                claims=extracted,
                contexts=contexts,
                verifier_model_id=self._settings.query_llm_model_name or "verifier",
            )
            pairwise = await self._pairwise_port.compare(
                question_id=question_id,
                question=question,
                graph_answer=answer,
                baseline_answer=reference_answer,
                contexts=contexts,
                judge_model_id=self._settings.query_llm_model_name or "judge",
            )
            results.append(
                _PerQuestionResult(
                    question_id=question_id,
                    claims=verified,
                    pairwise=pairwise,
                )
            )
        return tuple(results)

    async def _run_ragas(
        self,
        baseline: EvaluationBaselineReport,
        per_question_results: tuple[_PerQuestionResult, ...],
        *,
        run_ragas: bool,
    ) -> RAGASSecondaryMetrics:
        if not run_ragas:
            return RAGASSecondaryMetrics(available=False, notes="skipped")
        generation_results = tuple(
            (r.question_id, "", ()) for r in per_question_results
        )
        previous = RAGASSecondaryMetrics(
            faithfulness=baseline.metrics.get("faithfulness"),
            answer_relevancy=baseline.metrics.get("answer_relevancy"),
            context_precision=baseline.metrics.get("context_precision"),
            available=True,
        )
        return await self._ragas_port.run(
            dataset_id=baseline.dataset_id,
            generation_results=generation_results,
            previous_metrics=previous,
        )

    def _result(
        self,
        *,
        status: LayerStatus,
        rationale: str,
        metrics: tuple[LayerMetricValue, ...],
        baseline: EvaluationBaselineReport | None,
        ragas: RAGASSecondaryMetrics | None,
        pairwise_wins: int,
        pairwise_total: int,
        warnings: tuple[str, ...] = (),
    ) -> EvaluationLayerResult:
        model_ids: tuple[str, ...] = ()
        if baseline is not None:
            model_ids = baseline.model_ids
        elif self._settings.query_llm_model_name:
            model_ids = (self._settings.query_llm_model_name,)

        claim_count = sum(len(r.claims) for r in getattr(self, "_last_per_question", ()))
        return EvaluationLayerResult(
            layer="generation",
            status=status,
            project_owned_metrics=metrics,
            ragas_secondary=ragas,
            warnings=warnings,
            rationale=(
                f"{rationale} "
                f"(claims={claim_count}, pairwise={pairwise_wins}/{pairwise_total})"
            ),
            source_dataset_id=baseline.dataset_id if baseline else "generation_dataset",
            baseline_report_path="data/evaluation/generation_baseline.json"
            if baseline
            else None,
            run_metadata=LayerRunMetadata(
                run_id=self._run_id,
                code_commit=self._code_commit or (baseline.code_commit if baseline else ""),
                model_ids=model_ids,
            ),
        )


class _PerQuestionResult:
    def __init__(
        self,
        *,
        question_id: str,
        claims: tuple[AtomicClaim, ...],
        pairwise: Any,
    ) -> None:
        self.question_id = question_id
        self.claims = claims
        self.pairwise = pairwise
