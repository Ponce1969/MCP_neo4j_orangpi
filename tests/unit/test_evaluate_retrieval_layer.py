"""Tests for EvaluateRetrievalLayerUseCase (Slice B, T-B.12)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from book_graph_rag.application.evaluate_retrieval_layer_use_case import (
    EvaluateRetrievalLayerUseCase,
)
from book_graph_rag.domain.evaluation_models import (
    EvaluationDataset,
    EvaluationLayerResult,
    LayerStatus,
    RAGASSecondaryMetrics,
    RetrievalContext,
)
from book_graph_rag.ports.evaluation_dataset_port import EvaluationDatasetPort
from book_graph_rag.ports.graph_retrieval_port import GraphRetrievalPort
from book_graph_rag.ports.ragas_runner_port import RAGASRunnerPort


class _FakeDatasetPort(EvaluationDatasetPort):
    def __init__(self, records: tuple[dict[str, Any], ...]) -> None:
        self._records = records

    def load(self, dataset_id: str) -> EvaluationDataset:
        return EvaluationDataset(dataset_id=dataset_id, records=self._records)

    def list_datasets(self) -> tuple[str, ...]:
        return ("retrieval_dataset",)


class _FakeRetrievalPort(GraphRetrievalPort):
    def __init__(
        self, contexts_by_question: Mapping[str, tuple[RetrievalContext, ...]],
    ) -> None:
        self._contexts = contexts_by_question

    async def fetch_contexts(
        self, *, question: str, qtype: str, detail_level: int,
    ) -> tuple[RetrievalContext, ...]:
        return self._contexts.get(question, ())

    async def compose_answer(
        self, *, question: str, contexts: tuple[RetrievalContext, ...],
    ) -> str:
        return ""


class _FakeRagasPort(RAGASRunnerPort):
    def __init__(self, metrics: RAGASSecondaryMetrics) -> None:
        self._metrics = metrics

    async def run(
        self, *, dataset_id: str,
        generation_results: tuple[tuple[str, str, tuple[str, ...]], ...],
        previous_metrics: RAGASSecondaryMetrics | None = None,
    ) -> RAGASSecondaryMetrics:
        return self._metrics


def _make_use_case(
    *,
    records: tuple[dict[str, Any], ...],
    contexts: Mapping[str, tuple[RetrievalContext, ...]],
    ragas_metrics: RAGASSecondaryMetrics | None = None,
) -> EvaluateRetrievalLayerUseCase:
    return EvaluateRetrievalLayerUseCase(
        dataset_port=_FakeDatasetPort(records),
        retrieval_port=_FakeRetrievalPort(contexts),
        ragas_port=_FakeRagasPort(ragas_metrics or RAGASSecondaryMetrics(available=False)),
    )


def _precision(result: EvaluationLayerResult) -> float:
    metric = next(
        m for m in result.project_owned_metrics if m.name == "precision_at_k"
    )
    return metric.value


def test_warning_only_status_for_low_precision() -> None:
    """Low precision@k returns PASSED with a WARNING (never FAILED)."""
    records = (
        {
            "question_id": "ret-001",
            "question": "ReAct pattern definition",
            "qtype": "local",
            "reference_context_ids": ["a:book:1", "a:book:2"],
        },
    )
    contexts = {
        "ReAct pattern definition": (
            RetrievalContext(chunk_id="a:book:99", text="unrelated-context"),
        ),
    }
    uc = _make_use_case(records=records, contexts=contexts)
    result = asyncio.run(uc.execute())
    assert result.status == LayerStatus.PASSED
    assert any("low precision" in w.lower() for w in result.warnings)


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
        "ReAct pattern definition": (
            RetrievalContext(chunk_id="a:book:5", text="..."),
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
    assert any("low precision" in w.lower() for w in result.warnings)


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
