"""Tests for PairwiseJudgePort (Slice B, T-B.2)."""

from __future__ import annotations

import asyncio

import pytest

from book_graph_rag.domain.evaluation_models import PairwiseJudgment
from book_graph_rag.ports.pairwise_judge_port import PairwiseJudgePort


def test_pairwise_judge_port_is_abstract() -> None:
    """PairwiseJudgePort cannot be instantiated directly."""
    with pytest.raises(TypeError, match="abstract"):
        PairwiseJudgePort()  # type: ignore[abstract]


def test_pairwise_judge_port_compare_returns_judgment() -> None:
    """A fake adapter implements compare and returns a PairwiseJudgment."""

    class FakeJudge(PairwiseJudgePort):
        async def compare(
            self, *, question_id: str, question: str,
            graph_answer: str, baseline_answer: str, contexts: tuple[str, ...],
            judge_model_id: str,
        ) -> PairwiseJudgment:
            return PairwiseJudgment(
                question_id=question_id,
                verdict="graph_wins",
                rationale="graph is more faithful",
                judge_model_id=judge_model_id,
            )

    port = FakeJudge()
    judgment = asyncio.run(port.compare(
        question_id="q1",
        question="what is MCP?",
        graph_answer="MCP is a protocol.",
        baseline_answer="MCP.",
        contexts=("ctx1",),
        judge_model_id="judge-x",
    ))
    assert judgment.verdict == "graph_wins"
    assert judgment.judge_model_id == "judge-x"
