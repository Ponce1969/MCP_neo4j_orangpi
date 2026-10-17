"""Application use case: evaluate the generation layer (Slice B, U2)."""

from __future__ import annotations

import uuid
from typing import Any

from book_graph_rag.config import Settings
from book_graph_rag.domain.answer_grounding import (
    AnswerGrounding,
    GroundingContext,
    grounding,
)
from book_graph_rag.domain.checkpoint_models import format_redacted_error
from book_graph_rag.domain.evaluation_models import (
    AnswerGroundingSummary,
    AtomicClaim,
    EvaluationBaselineReport,
    EvaluationDataset,
    EvaluationLayerResult,
    GroundingQuestionScore,
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

    #: A faithfulness drop larger than this margin vs the committed baseline is worth an
    #: operator's attention even while the gate still passes — the same 0.05 the drop logic
    #: used when it compared RAGAS run-to-run.
    _FAITHFULNESS_DROP_THRESHOLD: float = 0.05

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
        hmac_key_id: str = "",
        hmac_secret: str = "",
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
        self._hmac_key_id = hmac_key_id
        self._hmac_secret = hmac_secret

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
            redacted = format_redacted_error(
                exc,
                key_id=self._hmac_key_id,
                secret=self._hmac_secret,
            )
            return self._result(
                status=LayerStatus.UNREACHABLE,
                rationale=f"dataset load failed: {redacted}",
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

        per_question_results, failures = await self._evaluate_questions(
            dataset, detail_level=detail_level
        )
        self._last_per_question = per_question_results

        total_claims = sum(len(r.claims) for r in per_question_results)
        supported_claims = sum(
            1 for r in per_question_results for c in r.claims if c.verdict == "support"
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
        warnings: list[str] = [
            f"question {question_id} could not be evaluated: {message}"
            for question_id, message in failures
        ]
        if not ragas.available:
            warnings.append(f"RAGAS unavailable: {ragas.notes}")
        # The drop signal is the layer's OWN claim-verified faithfulness vs the committed
        # baseline's faithfulness — the same instrument on both sides. It must not be
        # routed through the RAGAS secondary: the runner scores a dataset this layer fills
        # with empty answers keyed by question id (the script re-answers the ids itself),
        # and the committed baseline carries no RAGAS number at all. Comparing the two
        # under one name produced the 2026-10-16 gate warning
        # "RAGAS drop in faithfulness vs baseline: 0.8988 -> 0.7143" whose two numbers
        # came from two different instruments while the same report called RAGAS
        # unavailable.
        baseline_faithfulness = baseline.metrics.get("faithfulness")
        if (
            baseline_faithfulness is not None
            and baseline_faithfulness - faithfulness > self._FAITHFULNESS_DROP_THRESHOLD
        ):
            warnings.append(
                "faithfulness drop vs baseline (project-owned metric): "
                f"{baseline_faithfulness:.4f} -> {faithfulness:.4f}"
            )

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

        if failures:
            # Never PASSED over a partial sample: name the questions that were lost and say what
            # the metrics cover. One failing question used to abort the whole layer instead.
            failed_ids = ", ".join(question_id for question_id, _ in failures)
            return self._result(
                status=LayerStatus.UNREACHABLE,
                rationale=(
                    f"{len(failures)} of {len(dataset.records)} questions could not be "
                    f"evaluated ({failed_ids}); the metrics cover the rest"
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
    ) -> tuple[tuple[_PerQuestionResult, ...], tuple[tuple[str, str], ...]]:
        """Evaluate every record, keeping the failures instead of aborting the run.

        Each question is independent, and a transient provider omission used to abort the whole
        layer: an hour of work, one exit code and no report. The failures come back with their
        question id so ``execute`` can name them and refuse to call a partial sample PASSED.
        """
        results: list[_PerQuestionResult] = []
        failures: list[tuple[str, str]] = []
        for record in dataset.records:
            question_id = str(record.get("question_id", ""))
            question = str(record.get("question", ""))
            qtype = str(record.get("qtype", "global"))
            reference_answer = str(record.get("reference_answer", ""))

            try:
                result = await self._evaluate_one(
                    question_id=question_id,
                    question=question,
                    qtype=qtype,
                    reference_answer=reference_answer,
                    detail_level=detail_level,
                )
            except Exception as exc:  # noqa: BLE001 - one question must not cost the run
                failures.append(
                    (
                        question_id,
                        format_redacted_error(
                            exc,
                            key_id=self._hmac_key_id,
                            secret=self._hmac_secret,
                        ),
                    )
                )
                continue
            results.append(result)
        return tuple(results), tuple(failures)

    async def _evaluate_one(
        self,
        *,
        question_id: str,
        question: str,
        qtype: str,
        reference_answer: str,
        detail_level: int,
    ) -> _PerQuestionResult:
        """Run one question end to end: evidence, answer, claims, verdict and grounding."""
        if qtype == "global":
            # Global questions go through the production path, which selects and composes in
            # ONE pass. Fetching the contexts and composing separately measured a path
            # production never runs — and measured it blind to the question, because that
            # fetch returned every summary at the level in database order.
            global_answer = await self._retrieval_port.answer_global(
                question=question, detail_level=detail_level
            )
            contexts = global_answer.contexts
            answer = global_answer.answer
            evidence_ids: tuple[str | None, ...] = global_answer.citation_ids
        else:
            contexts = await self._retrieval_port.fetch_contexts(
                question=question,
                qtype=qtype,  # type: ignore[arg-type]
                detail_level=detail_level,
            )
            answer = await self._retrieval_port.compose_answer(question=question, contexts=contexts)
            evidence_ids = tuple(context.chunk_id for context in contexts)
        # Deterministic evidence usage: the answer's own citations and terms measured
        # against the contexts it was given. No model call, and no id is invented for a
        # context that carries none. Chunk hits cite their chunk id; the global path cites
        # the summary ids production hands its composer, so the share stops being undefined.
        evidence_usage = grounding(
            answer,
            [
                GroundingContext(id=evidence_id, text=context.text)
                for evidence_id, context in zip(evidence_ids, contexts, strict=True)
            ],
        )
        # Claim/pairwise ports still consume plain text evidence; pass the
        # text payloads while preserving the structured contexts upstream.
        context_texts = tuple(ctx.text for ctx in contexts)
        extracted = await self._claim_port.extract_claims(
            question_id=question_id,
            answer=answer,
            contexts=context_texts,
            extractor_model_id=self._settings.query_llm_model_name or "extractor",
        )
        verified = await self._claim_port.verify_claims(
            question_id=question_id,
            claims=extracted,
            contexts=context_texts,
            verifier_model_id=self._settings.query_llm_model_name or "verifier",
        )
        pairwise = await self._pairwise_port.compare(
            question_id=question_id,
            question=question,
            graph_answer=answer,
            baseline_answer=reference_answer,
            contexts=context_texts,
            judge_model_id=self._settings.query_llm_model_name or "judge",
        )
        return _PerQuestionResult(
            question_id=question_id,
            claims=verified,
            pairwise=pairwise,
            grounding=evidence_usage,
        )

    async def _run_ragas(
        self,
        baseline: EvaluationBaselineReport,
        per_question_results: tuple[_PerQuestionResult, ...],
        *,
        run_ragas: bool,
    ) -> RAGASSecondaryMetrics:
        if not run_ragas:
            return RAGASSecondaryMetrics(available=False, notes="skipped")
        generation_results = tuple((r.question_id, "", ()) for r in per_question_results)
        # No ``previous_metrics`` snapshot: the committed baseline's faithfulness is the
        # claim-verified primary number, not a RAGAS measurement, so handing it to the
        # runner as a "previous RAGAS value" made the runner compute a cross-instrument
        # comparison that the layer then printed under RAGAS's name. The secondary result
        # now carries only what RAGAS itself measured.
        return await self._ragas_port.run(
            dataset_id=baseline.dataset_id,
            generation_results=generation_results,
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

        per_question: tuple[_PerQuestionResult, ...] = getattr(self, "_last_per_question", ())
        claim_count = sum(len(r.claims) for r in per_question)
        return EvaluationLayerResult(
            layer="generation",
            status=status,
            project_owned_metrics=metrics,
            ragas_secondary=ragas,
            answer_grounding=_grounding_summary(per_question),
            warnings=warnings,
            rationale=(
                f"{rationale} (claims={claim_count}, pairwise={pairwise_wins}/{pairwise_total})"
            ),
            source_dataset_id=baseline.dataset_id if baseline else "generation_dataset",
            baseline_report_path="data/evaluation/generation_baseline.json" if baseline else None,
            run_metadata=LayerRunMetadata(
                run_id=self._run_id,
                code_commit=self._code_commit or (baseline.code_commit if baseline else ""),
                model_ids=model_ids,
            ),
        )


def _grounding_summary(results: tuple[_PerQuestionResult, ...]) -> AnswerGroundingSummary | None:
    """Aggregate the per-question evidence usage, leaving an undefined mean undefined.

    The citation share is only averaged over the questions that could produce it: on paths
    whose contexts carry no citable id, including that question would drag the mean toward a
    zero that means "no ids", not "no grounding".
    """
    scored: list[tuple[str, AnswerGrounding]] = [
        (result.question_id, result.grounding) for result in results if result.grounding is not None
    ]
    if not scored:
        return None
    shares = [
        usage.cited_context_share for _, usage in scored if usage.cited_context_share is not None
    ]
    return AnswerGroundingSummary(
        questions=tuple(
            GroundingQuestionScore(
                question_id=question_id,
                cited_context_share=usage.cited_context_share,
                term_grounding=usage.term_grounding,
                cited_contexts=usage.cited_contexts,
                contexts_provided=usage.contexts_provided,
                contexts_with_ids=usage.contexts_with_ids,
            )
            for question_id, usage in scored
        ),
        mean_cited_context_share=sum(shares) / len(shares) if shares else None,
        mean_term_grounding=sum(usage.term_grounding for _, usage in scored) / len(scored),
    )


class _PerQuestionResult:
    def __init__(
        self,
        *,
        question_id: str,
        claims: tuple[AtomicClaim, ...],
        pairwise: Any,
        grounding: AnswerGrounding | None = None,
    ) -> None:
        self.question_id = question_id
        self.claims = claims
        self.pairwise = pairwise
        self.grounding = grounding
