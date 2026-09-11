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
    LayerStatus,
    RAGASSecondaryMetrics,
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
    def __init__(self, contexts_by_question: Mapping[str, tuple[str, ...]]) -> None:
        self._contexts = contexts_by_question

    async def fetch_contexts(
        self, *, question: str, qtype: str, detail_level: int,
    ) -> tuple[str, ...]:
        return self._contexts.get(question, ())

    async def compose_answer(
        self, *, question: str, contexts: tuple[str, ...],
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
    contexts: Mapping[str, tuple[str, ...]],
    ragas_metrics: RAGASSecondaryMetrics | None = None,
) -> EvaluateRetrievalLayerUseCase:
    return EvaluateRetrievalLayerUseCase(
        dataset_port=_FakeDatasetPort(records),
        retrieval_port=_FakeRetrievalPort(contexts),
        ragas_port=_FakeRagasPort(ragas_metrics or RAGASSecondaryMetrics(available=False)),
    )


def test_warning_only_status_for_low_precision() -> None:
    """Low precision@k returns PASSED with a WARNING (never FAILED)."""
    records = (
        {
            "question_id": "ret-001",
            "question": "ReAct pattern definition",
            "qtype": "local",
            "reference_context_ids": ["react-summary", "react-example"],
        },
    )
    contexts = {
        "ReAct pattern definition": ("unrelated-context",),
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
            "reference_context_ids": ["react-summary"],
        },
    )
    contexts = {
        "ReAct pattern definition": ("react-summary contains the definition.",),
    }
    uc = _make_use_case(
        records=records,
        contexts=contexts,
        ragas_metrics=RAGASSecondaryMetrics(available=True),
    )
    result = asyncio.run(uc.execute())
    assert result.status == LayerStatus.PASSED
    assert result.warnings == ()
    precision_metric = next(m for m in result.project_owned_metrics if m.name == "precision_at_k")
    assert precision_metric.value == 1.0


def test_ragas_context_precision_drop_folded_as_warning() -> None:
    """RAGAS context_precision drop is folded as a warning, never FAILED."""
    records = (
        {
            "question_id": "ret-001",
            "question": "ReAct pattern definition",
            "qtype": "local",
            "reference_context_ids": ["react-summary"],
        },
    )
    contexts = {
        "ReAct pattern definition": ("react-summary contains the definition.",),
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
