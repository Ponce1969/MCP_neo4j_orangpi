"""Tests for RAGASRunnerPort (Slice B, T-B.3)."""

from __future__ import annotations

import asyncio

import pytest

from book_graph_rag.domain.evaluation_models import RAGASSecondaryMetrics
from book_graph_rag.ports.ragas_runner_port import RAGASRunnerPort


def test_ragas_runner_port_is_abstract() -> None:
    """RAGASRunnerPort cannot be instantiated directly."""
    with pytest.raises(TypeError, match="abstract"):
        RAGASRunnerPort()  # type: ignore[abstract]


def test_ragas_runner_port_unavailable_returns_available_false() -> None:
    """A fake adapter can simulate RAGAS failure by returning available=False."""

    class FakeRunner(RAGASRunnerPort):
        async def run(
            self, *, dataset_id: str,
            generation_results: tuple[tuple[str, str, tuple[str, ...]], ...],
            previous_metrics: RAGASSecondaryMetrics | None = None,
        ) -> RAGASSecondaryMetrics:
            return RAGASSecondaryMetrics(available=False, notes="import failed")

    port = FakeRunner()
    metrics = asyncio.run(port.run(
        dataset_id="generation_dataset",
        generation_results=(),
        previous_metrics=None,
    ))
    assert metrics.available is False
    assert "import failed" in metrics.notes


def test_ragas_runner_port_drop_warning_flag() -> None:
    """A fake adapter can surface a drop_warning when faithfulness drops."""

    class FakeRunner(RAGASRunnerPort):
        async def run(
            self, *, dataset_id: str,
            generation_results: tuple[tuple[str, str, tuple[str, ...]], ...],
            previous_metrics: RAGASSecondaryMetrics | None = None,
        ) -> RAGASSecondaryMetrics:
            return RAGASSecondaryMetrics(
                faithfulness=0.5,
                previous_faithfulness=0.7,
                drop_warning=True,
                available=True,
            )

    port = FakeRunner()
    previous = RAGASSecondaryMetrics(faithfulness=0.7, available=True)
    metrics = asyncio.run(port.run(
        dataset_id="generation_dataset",
        generation_results=(),
        previous_metrics=previous,
    ))
    assert metrics.drop_warning is True
    assert metrics.available is True
