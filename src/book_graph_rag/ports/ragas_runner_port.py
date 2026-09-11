"""Port for running RAGAS secondary metrics."""

from __future__ import annotations

import abc

from book_graph_rag.domain.evaluation_models import RAGASSecondaryMetrics


class RAGASRunnerPort(abc.ABC):
    """Compute RAGAS metrics for an evaluation run; secondary signal only (R5.3)."""

    @abc.abstractmethod
    async def run(
        self,
        *,
        dataset_id: str,
        generation_results: tuple[tuple[str, str, tuple[str, ...]], ...],
        previous_metrics: RAGASSecondaryMetrics | None = None,
    ) -> RAGASSecondaryMetrics:
        """Compute faithfulness/answer_relevancy/context_precision.

        Returns ``RAGASSecondaryMetrics(available=False, ...)`` on import or
        execution failure (R5.4: WARNING, never block).

        ``generation_results`` is a tuple of ``(question_id, answer, contexts)``.
        """
