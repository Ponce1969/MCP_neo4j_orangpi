"""Port for pairwise comparison between graph answer and baseline answer."""

from __future__ import annotations

import abc

from book_graph_rag.domain.evaluation_models import PairwiseJudgment


class PairwiseJudgePort(abc.ABC):
    """Compare graph answer to vector baseline answer; emit a verdict (R5.2)."""

    @abc.abstractmethod
    async def compare(
        self,
        *,
        question_id: str,
        question: str,
        graph_answer: str,
        baseline_answer: str,
        contexts: tuple[str, ...],
        judge_model_id: str,
    ) -> PairwiseJudgment:
        """Return a verdict: ``graph_wins``, ``tie``, or ``baseline_wins``."""
