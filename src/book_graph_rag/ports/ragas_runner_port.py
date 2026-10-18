"""Port for running RAGAS secondary metrics."""

from __future__ import annotations

import abc
from collections.abc import Sequence

from book_graph_rag.domain.evaluation_models import RagasSample, RAGASSecondaryMetrics


class RAGASRunnerPort(abc.ABC):
    """Compute RAGAS metrics for an evaluation run; secondary signal only (R5.3)."""

    @abc.abstractmethod
    async def run(
        self,
        *,
        dataset_id: str,
        generation_results: Sequence[RagasSample],
        previous_metrics: RAGASSecondaryMetrics | None = None,
    ) -> RAGASSecondaryMetrics:
        """Compute faithfulness/answer_relevancy/context_precision.

        Returns ``RAGASSecondaryMetrics(available=False, ...)`` on import or
        execution failure (R5.4: WARNING, never block).

        ``generation_results`` carries one ``RagasSample`` per evaluated question:
        the real benchmark question, the answer the pipeline produced and the
        contexts that fed its synthesis. The runner is a passive auditor (E3b):
        it scores exactly what it receives and regenerates nothing.
        """
