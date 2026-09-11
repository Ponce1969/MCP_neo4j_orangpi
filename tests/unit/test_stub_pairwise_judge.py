"""Tests for StubPairwiseJudge (Slice B, T-B.6)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from book_graph_rag.infrastructure.stub_pairwise_judge import StubPairwiseJudge


@pytest.fixture
def fixture_path() -> Path:
    return Path("tests/fixtures/evaluation/pairwise.json")


@pytest.fixture
def judge(fixture_path: Path) -> StubPairwiseJudge:
    return StubPairwiseJudge(fixture_path)


def test_stub_pairwise_judge_returns_fixture_verdict(
    judge: StubPairwiseJudge,
) -> None:
    """compare returns the verdict stored in the fixture keyed by question_id."""
    result = asyncio.run(judge.compare(
        question_id="q1",
        question="what is MCP?",
        graph_answer="MCP is a protocol for context exchange.",
        baseline_answer="MCP.",
        contexts=("ctx1",),
        judge_model_id="stub-judge",
    ))
    assert result.verdict == "graph_wins"
    assert result.judge_model_id == "stub-judge"
    assert "more detail" in result.rationale


def test_stub_pairwise_judge_unknown_question_defaults_tie(
    judge: StubPairwiseJudge,
) -> None:
    """An unknown question_id yields a tie with a clear rationale."""
    result = asyncio.run(judge.compare(
        question_id="unknown",
        question="?",
        graph_answer="a",
        baseline_answer="b",
        contexts=(),
        judge_model_id="stub-judge",
    ))
    assert result.verdict == "tie"
    assert "no fixture entry" in result.rationale


def test_stub_pairwise_judge_no_real_llm_imports(
    judge: StubPairwiseJudge,
) -> None:
    """The stub adapter does not import LLM client libraries."""
    import book_graph_rag.infrastructure.stub_pairwise_judge as stub_module

    source = Path(stub_module.__file__).read_text(encoding="utf-8")
    assert "openai" not in source
    assert "anthropic" not in source
    assert "instructor" not in source
