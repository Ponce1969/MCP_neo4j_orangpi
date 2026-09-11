"""Stub RAGAS runner for deterministic tests (Slice B, D8)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from book_graph_rag.domain.evaluation_models import RAGASSecondaryMetrics
from book_graph_rag.ports.ragas_runner_port import RAGASRunnerPort


class StubRAGASRunner(RAGASRunnerPort):
    """Reads RAGAS metrics from a JSON fixture keyed by ``dataset_id``."""

    _DROP_THRESHOLD: float = 0.05

    def __init__(self, fixture_path: Path) -> None:
        self._fixture_path = fixture_path
        self._data: dict[str, dict[str, Any]] = {}
        if fixture_path.exists():
            self._data = json.loads(fixture_path.read_text(encoding="utf-8"))

    async def run(
        self,
        *,
        dataset_id: str,
        generation_results: tuple[tuple[str, str, tuple[str, ...]], ...],
        previous_metrics: RAGASSecondaryMetrics | None = None,
    ) -> RAGASSecondaryMetrics:
        """Return fixture metrics or an unavailable warning."""
        entry = self._data.get(dataset_id, {})
        if not entry.get("available", False):
            return RAGASSecondaryMetrics(
                available=False,
                notes=entry.get("notes", "RAGAS unavailable"),
            )

        faithfulness = entry.get("faithfulness")
        previous_faithfulness = (
            previous_metrics.faithfulness if previous_metrics else None
        )
        drop_warning = False
        if (
            faithfulness is not None
            and previous_faithfulness is not None
            and (previous_faithfulness - faithfulness) > self._DROP_THRESHOLD
        ):
            drop_warning = True

        return RAGASSecondaryMetrics(
            faithfulness=faithfulness,
            answer_relevancy=entry.get("answer_relevancy"),
            context_precision=entry.get("context_precision"),
            previous_faithfulness=previous_faithfulness,
            drop_warning=drop_warning,
            available=True,
            notes=entry.get("notes", ""),
        )
