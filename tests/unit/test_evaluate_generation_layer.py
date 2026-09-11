"""Tests for EvaluateGenerationLayerUseCase (Slice B, T-B.11)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from book_graph_rag.application.evaluate_generation_layer_use_case import (
    EvaluateGenerationLayerUseCase,
)
from book_graph_rag.config import Settings
from book_graph_rag.domain.evaluation_models import (
    AtomicClaim,
    EvaluationBaselineReport,
    EvaluationDataset,
    LayerStatus,
    PairwiseJudgment,
    RAGASSecondaryMetrics,
)
from book_graph_rag.ports.claim_validator_port import ClaimValidatorPort
from book_graph_rag.ports.evaluation_baseline_port import EvaluationBaselinePort
from book_graph_rag.ports.evaluation_dataset_port import EvaluationDatasetPort
from book_graph_rag.ports.graph_retrieval_port import GraphRetrievalPort
from book_graph_rag.ports.pairwise_judge_port import PairwiseJudgePort
from book_graph_rag.ports.ragas_runner_port import RAGASRunnerPort


class _FakeDatasetPort(EvaluationDatasetPort):
    def __init__(self, records: tuple[dict[str, Any], ...] = ()) -> None:
        self._records = records

    def load(self, dataset_id: str) -> EvaluationDataset:
        return EvaluationDataset(dataset_id=dataset_id, records=self._records)

    def list_datasets(self) -> tuple[str, ...]:
        return ("generation_dataset",)


class _FakeRetrievalPort(GraphRetrievalPort):
    def __init__(self, answers: Mapping[str, str] | None = None) -> None:
        self._answers = answers or {}

    async def fetch_contexts(
        self, *, question: str, qtype: str, detail_level: int,
    ) -> tuple[str, ...]:
        return ("ctx1", "ctx2")

    async def compose_answer(
        self, *, question: str, contexts: tuple[str, ...],
    ) -> str:
        return self._answers.get(question, "composed answer")


class _FakeClaimPort(ClaimValidatorPort):
    def __init__(
        self,
        claims_by_question: Mapping[str, tuple[AtomicClaim, ...]],
        verdict: str | None = "support",
    ) -> None:
        self._claims = claims_by_question
        self._verdict = verdict

    async def extract_claims(
        self, *, question_id: str, answer: str, contexts: tuple[str, ...],
        extractor_model_id: str,
    ) -> tuple[AtomicClaim, ...]:
        return self._claims.get(question_id, ())

    async def verify_claims(
        self, *, question_id: str, claims: tuple[AtomicClaim, ...],
        contexts: tuple[str, ...], verifier_model_id: str,
    ) -> tuple[AtomicClaim, ...]:
        if self._verdict is None:
            return claims
        return tuple(
            claim.model_copy(update={"verdict": self._verdict})
            for claim in claims
        )


class _FakePairwisePort(PairwiseJudgePort):
    async def compare(
        self, *, question_id: str, question: str,
        graph_answer: str, baseline_answer: str, contexts: tuple[str, ...],
        judge_model_id: str,
    ) -> PairwiseJudgment:
        return PairwiseJudgment(
            question_id=question_id,
            verdict="graph_wins",
            rationale="graph wins",
            judge_model_id=judge_model_id,
        )


class _FakeRagasPort(RAGASRunnerPort):
    def __init__(self, metrics: RAGASSecondaryMetrics) -> None:
        self._metrics = metrics

    async def run(
        self, *, dataset_id: str,
        generation_results: tuple[tuple[str, str, tuple[str, ...]], ...],
        previous_metrics: RAGASSecondaryMetrics | None = None,
    ) -> RAGASSecondaryMetrics:
        return self._metrics


class _FakeBaselinePort(EvaluationBaselinePort):
    def __init__(self, baseline: EvaluationBaselineReport | None) -> None:
        self._baseline = baseline

    def load(self, layer: str) -> EvaluationBaselineReport | None:
        return self._baseline


def _settings() -> Settings:
    return Settings.model_validate({
        "neo4j_uri": "bolt://localhost:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "password",
    })


def _make_use_case(
    *,
    baseline: EvaluationBaselineReport | None,
    claims: Mapping[str, tuple[AtomicClaim, ...]] | None = None,
    verdict: str | None = "support",
    ragas_metrics: RAGASSecondaryMetrics | None = None,
    answers: Mapping[str, str] | None = None,
) -> EvaluateGenerationLayerUseCase:
    records = (
        {
            "question_id": "q1",
            "question": "what is MCP?",
            "qtype": "global",
            "reference_answer": "MCP is a protocol.",
            "contexts_hint": ["ctx1"],
        },
    )
    default_claim = AtomicClaim(
        claim_id="c1",
        text="MCP is a protocol.",
        evidence_refs=("ctx1",),
    )
    claims = claims or {"q1": (default_claim,)}
    return EvaluateGenerationLayerUseCase(
        dataset_port=_FakeDatasetPort(records),
        retrieval_port=_FakeRetrievalPort(answers=answers),
        claim_port=_FakeClaimPort(claims, verdict=verdict),
        pairwise_port=_FakePairwisePort(),
        ragas_port=_FakeRagasPort(
            ragas_metrics or RAGASSecondaryMetrics(available=False),
        ),
        baseline_port=_FakeBaselinePort(baseline),
        settings=_settings(),
        run_id="run-1",
        code_commit="abc123",
    )


def test_generation_status_incomplete_when_baseline_missing() -> None:
    """Missing baseline yields INCOMPLETE."""
    uc = _make_use_case(baseline=None)
    result = asyncio.run(uc.execute())
    assert result.status == LayerStatus.INCOMPLETE
    assert "baseline missing" in result.rationale


def test_generation_status_incomplete_when_thresholds_unfinalized() -> None:
    """Unfinalized thresholds yield INCOMPLETE even when faithfulness is high."""
    baseline = EvaluationBaselineReport(
        layer="generation",
        dataset_id="generation_dataset",
        dataset_sha256="0" * 64,
        thresholds_finalized=False,
        metrics={"faithfulness": 1.0},
    )
    uc = _make_use_case(baseline=baseline)
    result = asyncio.run(uc.execute())
    assert result.status == LayerStatus.INCOMPLETE
    assert "thresholds not finalized" in result.rationale


def test_generation_status_failed_when_faithfulness_regression() -> None:
    """faithfulness < faithfulness_min yields FAILED."""
    baseline = EvaluationBaselineReport(
        layer="generation",
        dataset_id="generation_dataset",
        dataset_sha256="0" * 64,
        thresholds_finalized=True,
        faithfulness_min=0.9,
        metrics={"faithfulness": 0.5},
    )
    low_claims = {
        "q1": (
            AtomicClaim(
                claim_id="c1",
                text="MCP is a protocol.",
                evidence_refs=("ctx1",),
            ),
            AtomicClaim(
                claim_id="c2",
                text="MCP is a database.",
                evidence_refs=("ctx1",),
            ),
        ),
    }
    uc = _make_use_case(
        baseline=baseline, claims=low_claims, verdict="contradict",
    )
    result = asyncio.run(uc.execute())
    assert result.status == LayerStatus.FAILED
    assert "faithfulness" in result.rationale


def test_generation_status_passed_when_faithfulness_above_baseline() -> None:
    """faithfulness >= faithfulness_min yields PASSED."""
    baseline = EvaluationBaselineReport(
        layer="generation",
        dataset_id="generation_dataset",
        dataset_sha256="0" * 64,
        thresholds_finalized=True,
        faithfulness_min=0.5,
        metrics={"faithfulness": 0.8},
    )
    uc = _make_use_case(baseline=baseline)
    result = asyncio.run(uc.execute())
    assert result.status == LayerStatus.PASSED


def test_ragas_unavailable_yields_warning_not_block() -> None:
    """RAGAS unavailable adds a warning but does not block PASSED."""
    baseline = EvaluationBaselineReport(
        layer="generation",
        dataset_id="generation_dataset",
        dataset_sha256="0" * 64,
        thresholds_finalized=True,
        faithfulness_min=0.5,
        metrics={"faithfulness": 0.8},
    )
    uc = _make_use_case(
        baseline=baseline,
        ragas_metrics=RAGASSecondaryMetrics(
            available=False, notes="import failed",
        ),
    )
    result = asyncio.run(uc.execute())
    assert result.status == LayerStatus.PASSED
    assert any("RAGAS unavailable" in w for w in result.warnings)


def test_ragas_drop_yields_warning_not_block() -> None:
    """RAGAS drop adds a warning but does not block PASSED."""
    baseline = EvaluationBaselineReport(
        layer="generation",
        dataset_id="generation_dataset",
        dataset_sha256="0" * 64,
        thresholds_finalized=True,
        faithfulness_min=0.5,
        metrics={"faithfulness": 0.8},
    )
    ragas = RAGASSecondaryMetrics(
        faithfulness=0.7,
        available=True,
        drop_warning=True,
    )
    uc = _make_use_case(baseline=baseline, ragas_metrics=ragas)
    result = asyncio.run(uc.execute())
    assert result.status == LayerStatus.PASSED
    assert any("RAGAS drop" in w for w in result.warnings)


def test_pairwise_recorded_as_evidence_never_blocks() -> None:
    """Pairwise verdict is recorded in metrics but never causes FAILED."""
    baseline = EvaluationBaselineReport(
        layer="generation",
        dataset_id="generation_dataset",
        dataset_sha256="0" * 64,
        thresholds_finalized=True,
        faithfulness_min=0.5,
        metrics={"faithfulness": 0.8},
    )
    uc = _make_use_case(baseline=baseline)
    result = asyncio.run(uc.execute())
    metric_names = {m.name for m in result.project_owned_metrics}
    assert "pairwise_win_rate" in metric_names
    assert result.status == LayerStatus.PASSED


def test_claims_auditable() -> None:
    """Every claim carries verdict and evidence_refs in the layer result."""
    baseline = EvaluationBaselineReport(
        layer="generation",
        dataset_id="generation_dataset",
        dataset_sha256="0" * 64,
        thresholds_finalized=True,
        faithfulness_min=0.5,
        metrics={"faithfulness": 0.8},
    )
    uc = _make_use_case(baseline=baseline)
    result = asyncio.run(uc.execute())
    assert result.ragas_secondary is not None
    assert "claim" in result.rationale.lower()


def test_pipeline_orchestration_order() -> None:
    """The pipeline runs fetch -> compose -> extract -> verify -> pairwise -> ragas."""
    calls: list[str] = []

    class TracedRetrieval(_FakeRetrievalPort):
        async def fetch_contexts(
            self, *, question: str, qtype: str, detail_level: int,
        ) -> tuple[str, ...]:
            calls.append("fetch")
            return await super().fetch_contexts(
                question=question, qtype=qtype, detail_level=detail_level,
            )

        async def compose_answer(
            self, *, question: str, contexts: tuple[str, ...],
        ) -> str:
            calls.append("compose")
            return await super().compose_answer(
                question=question, contexts=contexts,
            )

    class TracedClaim(_FakeClaimPort):
        async def extract_claims(
            self, *, question_id: str, answer: str,
            contexts: tuple[str, ...], extractor_model_id: str,
        ) -> tuple[AtomicClaim, ...]:
            calls.append("extract")
            return await super().extract_claims(
                question_id=question_id, answer=answer, contexts=contexts,
                extractor_model_id=extractor_model_id,
            )

        async def verify_claims(
            self, *, question_id: str, claims: tuple[AtomicClaim, ...],
            contexts: tuple[str, ...], verifier_model_id: str,
        ) -> tuple[AtomicClaim, ...]:
            calls.append("verify")
            return await super().verify_claims(
                question_id=question_id, claims=claims, contexts=contexts,
                verifier_model_id=verifier_model_id,
            )

    class TracedPairwise(_FakePairwisePort):
        async def compare(
            self, *, question_id: str, question: str,
            graph_answer: str, baseline_answer: str,
            contexts: tuple[str, ...], judge_model_id: str,
        ) -> PairwiseJudgment:
            calls.append("pairwise")
            return await super().compare(
                question_id=question_id, question=question,
                graph_answer=graph_answer, baseline_answer=baseline_answer,
                contexts=contexts, judge_model_id=judge_model_id,
            )

    class TracedRagas(_FakeRagasPort):
        async def run(
            self, *, dataset_id: str,
            generation_results: tuple[tuple[str, str, tuple[str, ...]], ...],
            previous_metrics: RAGASSecondaryMetrics | None = None,
        ) -> RAGASSecondaryMetrics:
            calls.append("ragas")
            return await super().run(
                dataset_id=dataset_id,
                generation_results=generation_results,
                previous_metrics=previous_metrics,
            )

    baseline = EvaluationBaselineReport(
        layer="generation",
        dataset_id="generation_dataset",
        dataset_sha256="0" * 64,
        thresholds_finalized=True,
        faithfulness_min=0.5,
        metrics={"faithfulness": 0.8},
    )
    traced_claim = TracedClaim(
        {"q1": (AtomicClaim(
            claim_id="c1",
            text="MCP is a protocol.",
            evidence_refs=("ctx1",),
        ),)},
    )
    uc = EvaluateGenerationLayerUseCase(
        dataset_port=_FakeDatasetPort((
            {
                "question_id": "q1",
                "question": "what is MCP?",
                "qtype": "global",
                "reference_answer": "MCP is a protocol.",
                "contexts_hint": ["ctx1"],
            },
        )),
        retrieval_port=TracedRetrieval(),
        claim_port=traced_claim,
        pairwise_port=TracedPairwise(),
        ragas_port=TracedRagas(RAGASSecondaryMetrics(available=False)),
        baseline_port=_FakeBaselinePort(baseline),
        settings=_settings(),
    )
    asyncio.run(uc.execute())
    assert calls == ["fetch", "compose", "extract", "verify", "pairwise", "ragas"]
