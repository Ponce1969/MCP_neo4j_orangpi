"""Tests for EvaluateRetrievalLayerUseCase (Slice B, T-B.12)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from book_graph_rag.application.evaluate_retrieval_layer_use_case import (
    EvaluateRetrievalLayerUseCase,
)
from book_graph_rag.domain.evaluation_models import (
    EvaluationBaselineReport,
    EvaluationDataset,
    EvaluationLayerResult,
    LayerStatus,
    RAGASSecondaryMetrics,
    RetrievalContext,
)
from book_graph_rag.ports.evaluation_baseline_port import EvaluationBaselinePort
from book_graph_rag.ports.evaluation_dataset_port import EvaluationDatasetPort
from book_graph_rag.ports.graph_retrieval_port import GraphRetrievalPort
from book_graph_rag.ports.ragas_runner_port import RAGASRunnerPort


class _FakeBaselinePort(EvaluationBaselinePort):
    """Fake baseline store; returns the configured report or None."""

    def __init__(self, baseline: EvaluationBaselineReport | None) -> None:
        self._baseline = baseline

    def load(self, layer: str) -> EvaluationBaselineReport | None:
        return self._baseline if layer == "retrieval" else None


class _FakeDatasetPort(EvaluationDatasetPort):
    def __init__(self, records: tuple[dict[str, Any], ...]) -> None:
        self._records = records

    def load(self, dataset_id: str) -> EvaluationDataset:
        return EvaluationDataset(dataset_id=dataset_id, records=self._records)

    def list_datasets(self) -> tuple[str, ...]:
        return ("retrieval_dataset",)


class _FakeRetrievalPort(GraphRetrievalPort):
    def __init__(
        self,
        contexts_by_question: Mapping[str, tuple[RetrievalContext, ...]],
    ) -> None:
        self._contexts = contexts_by_question

    async def fetch_contexts(
        self,
        *,
        question: str,
        qtype: str,
        detail_level: int,
    ) -> tuple[RetrievalContext, ...]:
        return self._contexts.get(question, ())

    async def compose_answer(
        self,
        *,
        question: str,
        contexts: tuple[RetrievalContext, ...],
    ) -> str:
        return ""


class _FakeRagasPort(RAGASRunnerPort):
    def __init__(self, metrics: RAGASSecondaryMetrics) -> None:
        self._metrics = metrics

    async def run(
        self,
        *,
        dataset_id: str,
        generation_results: tuple[tuple[str, str, tuple[str, ...]], ...],
        previous_metrics: RAGASSecondaryMetrics | None = None,
    ) -> RAGASSecondaryMetrics:
        return self._metrics


def _finalized_baseline(*, precision_at_k_min: float = 0.0) -> EvaluationBaselineReport:
    return EvaluationBaselineReport(
        layer="retrieval",
        dataset_id="retrieval_dataset",
        dataset_sha256="0" * 64,
        thresholds_finalized=True,
        precision_at_k_min=precision_at_k_min,
    )


def _make_use_case(
    *,
    records: tuple[dict[str, Any], ...],
    contexts: Mapping[str, tuple[RetrievalContext, ...]],
    ragas_metrics: RAGASSecondaryMetrics | None = None,
    baseline: EvaluationBaselineReport | None = None,
) -> EvaluateRetrievalLayerUseCase:
    return EvaluateRetrievalLayerUseCase(
        dataset_port=_FakeDatasetPort(records),
        retrieval_port=_FakeRetrievalPort(contexts),
        ragas_port=_FakeRagasPort(ragas_metrics or RAGASSecondaryMetrics(available=False)),
        baseline_port=_FakeBaselinePort(
            baseline if baseline is not None else _finalized_baseline()
        ),
    )


def _precision(result: EvaluationLayerResult) -> float:
    metric = next(m for m in result.project_owned_metrics if m.name == "precision_at_k")
    return metric.value


def test_precision_above_committed_baseline_emits_no_warning() -> None:
    """Precision above the committed baseline is silent: no arbitrary threshold.

    This is the production shape: 0.0478 against a committed 0.045 used to warn,
    because a hardcoded 0.5 was unreachable for a ``matched / len(contexts)``
    metric with k=10 (the dataset's ceiling is ~0.19). The committed baseline is
    the only alert mechanism; the number itself stays visible in the rationale.
    """
    records = (
        {
            "question_id": "ret-001",
            "question": "ReAct pattern definition",
            "qtype": "local",
            "reference_context_ids": ["a:book:1", "a:book:77"],
        },
    )
    contexts = {
        "ReAct pattern definition": tuple(
            RetrievalContext(chunk_id=f"a:book:{n}", text="ctx") for n in range(10)
        ),
    }
    uc = _make_use_case(
        records=records,
        contexts=contexts,
        ragas_metrics=RAGASSecondaryMetrics(available=True),
        baseline=_finalized_baseline(precision_at_k_min=0.045),
    )
    result = asyncio.run(uc.execute())
    assert result.status == LayerStatus.PASSED
    assert _precision(result) == 0.1
    assert result.warnings == ()


def test_high_precision_returns_passed_no_warning() -> None:
    """High precision@k returns PASSED without a warning."""
    records = (
        {
            "question_id": "ret-001",
            "question": "ReAct pattern definition",
            "qtype": "local",
            "reference_context_ids": ["a:book:5"],
        },
    )
    contexts = {
        "ReAct pattern definition": (
            RetrievalContext(chunk_id="a:book:5", text="the ReAct definition."),
        ),
    }
    uc = _make_use_case(
        records=records,
        contexts=contexts,
        ragas_metrics=RAGASSecondaryMetrics(available=True),
    )
    result = asyncio.run(uc.execute())
    assert result.status == LayerStatus.PASSED
    assert result.warnings == ()
    assert _precision(result) == 1.0


def test_exact_chunk_id_match_returns_high_precision() -> None:
    """Exact chunk_id match yields precision 1.0 and no warning."""
    records = (
        {
            "question_id": "ret-001",
            "question": "ReAct pattern definition",
            "qtype": "local",
            "reference_context_ids": ["a:book:5"],
        },
    )
    contexts = {
        "ReAct pattern definition": (RetrievalContext(chunk_id="a:book:5", text="..."),),
    }
    uc = _make_use_case(
        records=records,
        contexts=contexts,
        ragas_metrics=RAGASSecondaryMetrics(available=True),
    )
    result = asyncio.run(uc.execute())
    assert result.status == LayerStatus.PASSED
    assert result.warnings == ()
    assert _precision(result) == 1.0


def test_chunk_id_none_contributes_zero_never_false_positive() -> None:
    """A None chunk_id never matches by substring in the context text."""
    records = (
        {
            "question_id": "ret-001",
            "question": "ReAct pattern definition",
            "qtype": "local",
            "reference_context_ids": ["a:book:5"],
        },
    )
    contexts = {
        "ReAct pattern definition": (
            RetrievalContext(chunk_id=None, text="a:book:5 appears only in text"),
        ),
    }
    uc = _make_use_case(records=records, contexts=contexts)
    result = asyncio.run(uc.execute())
    assert _precision(result) == 0.0
    # No arbitrary threshold warns about the zero; only the committed baseline
    # does, and here it is 0.0.
    assert not any("precision" in w.lower() for w in result.warnings)


def test_ragas_context_precision_drop_folded_as_warning() -> None:
    """RAGAS context_precision drop is folded as a warning, never FAILED."""
    records = (
        {
            "question_id": "ret-001",
            "question": "ReAct pattern definition",
            "qtype": "local",
            "reference_context_ids": ["a:book:7"],
        },
    )
    contexts = {
        "ReAct pattern definition": (
            RetrievalContext(chunk_id="a:book:7", text="the definition."),
        ),
    }
    ragas = RAGASSecondaryMetrics(
        context_precision=0.3,
        available=True,
        drop_warning=True,
    )
    uc = _make_use_case(records=records, contexts=contexts, ragas_metrics=ragas)
    result = asyncio.run(uc.execute())
    assert result.status == LayerStatus.PASSED
    assert any("RAGAS" in w for w in result.warnings)


def test_retrieval_layer_never_failed() -> None:
    """The retrieval layer result status is never FAILED."""
    records = (
        {
            "question_id": "ret-001",
            "question": "ReAct pattern definition",
            "qtype": "local",
            "reference_context_ids": [],
        },
    )
    contexts = {
        "ReAct pattern definition": (),
    }
    uc = _make_use_case(records=records, contexts=contexts)
    result = asyncio.run(uc.execute())
    assert result.status != LayerStatus.FAILED


def test_mixed_dataset_skips_future_records_in_precision() -> None:
    """Precision is computed only over current-corpus records.

    Future-corpus records never contribute to the precision numerator or
    denominator; their skipped count is reported in the rationale.
    """
    records = (
        {
            "question_id": "ret-001",
            "question": "current question A",
            "qtype": "local",
            "corpus": "current",
            "reference_context_ids": ["a:book:1"],
        },
        {
            "question_id": "ret-002",
            "question": "current question B",
            "qtype": "local",
            "corpus": "current",
            "reference_context_ids": ["a:book:2"],
        },
        {
            "question_id": "ret-003",
            "question": "future question C",
            "qtype": "local",
            "corpus": "future",
            "reference_context_ids": [],
        },
        {
            "question_id": "ret-004",
            "question": "future question D",
            "qtype": "local",
            "corpus": "future",
            "reference_context_ids": [],
        },
    )
    contexts = {
        "current question A": (RetrievalContext(chunk_id="a:book:1", text="..."),),
        "current question B": (RetrievalContext(chunk_id="a:book:2", text="..."),),
    }
    uc = _make_use_case(
        records=records,
        contexts=contexts,
        ragas_metrics=RAGASSecondaryMetrics(available=True),
    )
    result = asyncio.run(uc.execute())
    assert result.status == LayerStatus.PASSED
    assert _precision(result) == 1.0
    assert result.warnings == ()
    assert "skipped 2 future-corpus records" in result.rationale


def test_all_future_dataset_returns_passed_precision_zero() -> None:
    """All-future dataset yields PASSED (never FAILED) with precision 0.0."""
    records = (
        {
            "question_id": "ret-001",
            "question": "future question A",
            "qtype": "local",
            "corpus": "future",
            "reference_context_ids": [],
        },
        {
            "question_id": "ret-002",
            "question": "future question B",
            "qtype": "local",
            "corpus": "future",
            "reference_context_ids": [],
        },
    )
    uc = _make_use_case(records=records, contexts={})
    result = asyncio.run(uc.execute())
    assert result.status == LayerStatus.PASSED
    assert _precision(result) == 0.0
    assert "no active (current) records" in result.rationale
    assert "skipped 2 future-corpus records" in result.rationale


# ── baseline mechanism (Phase 5 delta) ───────────────────────────────────────


def test_missing_baseline_returns_incomplete() -> None:
    """No committed retrieval baseline ⇒ INCOMPLETE (mechanism-first)."""
    records = (
        ({},)
        if False
        else (  # placeholder to satisfy type shape
            {
                "question_id": "ret-001",
                "question": "ReAct pattern definition",
                "qtype": "local",
                "reference_context_ids": ["a:book:5"],
            },
        )
    )
    contexts = {"ReAct pattern definition": (RetrievalContext(chunk_id="a:book:5", text="x"),)}
    uc = EvaluateRetrievalLayerUseCase(
        dataset_port=_FakeDatasetPort(records),
        retrieval_port=_FakeRetrievalPort(contexts),
        ragas_port=_FakeRagasPort(RAGASSecondaryMetrics(available=False)),
        baseline_port=_FakeBaselinePort(None),
    )
    result = asyncio.run(uc.execute())
    assert result.status == LayerStatus.INCOMPLETE
    assert result.baseline_report_path is None


def test_unfinalized_baseline_returns_incomplete() -> None:
    """A committed but unfinalized baseline ⇒ INCOMPLETE, path still set."""
    records = (
        {
            "question_id": "ret-001",
            "question": "ReAct pattern definition",
            "qtype": "local",
            "reference_context_ids": ["a:book:5"],
        },
    )
    contexts = {"ReAct pattern definition": (RetrievalContext(chunk_id="a:book:5", text="x"),)}
    unfinalized = EvaluationBaselineReport(
        layer="retrieval",
        dataset_id="retrieval_dataset",
        dataset_sha256="0" * 64,
        thresholds_finalized=False,
    )
    uc = _make_use_case(records=records, contexts=contexts, baseline=unfinalized)
    result = asyncio.run(uc.execute())
    assert result.status == LayerStatus.INCOMPLETE
    assert result.baseline_report_path is not None
    assert "not finalized" in result.rationale


def test_finalized_baseline_sets_threshold_and_path() -> None:
    """A finalized baseline exposes precision_at_k_min as the metric threshold."""
    records = (
        {
            "question_id": "ret-001",
            "question": "ReAct pattern definition",
            "qtype": "local",
            "reference_context_ids": ["a:book:5"],
        },
    )
    contexts = {"ReAct pattern definition": (RetrievalContext(chunk_id="a:book:5", text="x"),)}
    uc = _make_use_case(
        records=records,
        contexts=contexts,
        ragas_metrics=RAGASSecondaryMetrics(available=True),
        baseline=_finalized_baseline(precision_at_k_min=0.45),
    )
    result = asyncio.run(uc.execute())
    assert result.status == LayerStatus.PASSED
    assert result.baseline_report_path == "data/evaluation/retrieval_baseline.json"
    metric = next(m for m in result.project_owned_metrics if m.name == "precision_at_k")
    assert metric.threshold == 0.45
    assert result.warnings == ()


def test_regression_below_baseline_threshold_warns() -> None:
    """Precision below the committed threshold yields a regression warning."""
    records = (
        {
            "question_id": "ret-001",
            "question": "ReAct pattern definition",
            "qtype": "local",
            "reference_context_ids": ["a:book:5"],
        },
    )
    contexts = {
        "ReAct pattern definition": (RetrievalContext(chunk_id="a:book:99", text="off-target"),),
    }
    uc = _make_use_case(
        records=records,
        contexts=contexts,
        baseline=_finalized_baseline(precision_at_k_min=0.5),
    )
    result = asyncio.run(uc.execute())
    assert result.status == LayerStatus.PASSED
    assert any("baseline" in w.lower() for w in result.warnings)
