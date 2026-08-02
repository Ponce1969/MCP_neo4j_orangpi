"""Tests for GlobalQueryUseCase (REQ-GR.1 PR3)."""

from __future__ import annotations

from typing import Any

import pytest

from book_graph_rag.domain.models import CommunitySummary
from book_graph_rag.ports.community_read_port import CommunityReadPort
from book_graph_rag.ports.llm_summary_port import LLMSummaryPort


class _FakeLLMSummaryPort(LLMSummaryPort):
    """In-memory LLMSummaryPort with configurable scores and answers."""

    def __init__(
        self,
        scores: dict[str, int] | None = None,
        answer: str = "fake answer",
    ) -> None:
        self.scores = scores or {}
        self.answer = answer
        self.score_calls: list[tuple[str, CommunitySummary]] = []
        self.compose_calls: list[tuple[str, list[tuple[CommunitySummary, int]]]] = []

    async def generate_community_summary(
        self,
        entities: list[Any],
        relationships: list[Any],
        level: int,
    ) -> str:
        return f"level {level} summary"

    async def score_community(self, question: str, summary: CommunitySummary) -> int:
        self.score_calls.append((question, summary))
        return self.scores.get(summary.id, 50)

    async def compose_answer(
        self, question: str, ranked: list[tuple[CommunitySummary, int]]
    ) -> str:
        self.compose_calls.append((question, ranked))
        return self.answer


class _FakeCommunityReadPort(CommunityReadPort):
    """In-memory CommunityReadPort with configurable summaries."""

    def __init__(self, summaries: dict[int, list[CommunitySummary]] | None = None) -> None:
        self.summaries = summaries or {}
        self.level_calls: list[int] = []

    async def load_entity_graph(self) -> tuple[list[Any], list[Any]]:
        return [], []

    async def get_summaries_by_level(self, level: int) -> list[CommunitySummary]:
        self.level_calls.append(level)
        return list(self.summaries.get(level, []))

    async def count_summaries(self) -> int:
        return sum(len(s) for s in self.summaries.values())


@pytest.fixture
def llm_port() -> _FakeLLMSummaryPort:
    return _FakeLLMSummaryPort()


@pytest.fixture
def read_port() -> _FakeCommunityReadPort:
    return _FakeCommunityReadPort()


@pytest.fixture
def use_case(
    read_port: _FakeCommunityReadPort, llm_port: _FakeLLMSummaryPort
) -> Any:
    # Imported lazily so the test can reference code that does not exist yet.
    from book_graph_rag.application.global_query_use_case import GlobalQueryUseCase

    return GlobalQueryUseCase(read_port, llm_port, max_concurrency=2, top_n=2)


# ── level validation ───────────────────────────────────────────────────────────


async def test_ask_rejects_negative_detail_level(use_case: Any) -> None:
    """detail_level < 0 raises ValueError before any port call."""
    with pytest.raises(ValueError, match="detail_level"):
        await use_case.ask("question", -1)


async def test_ask_rejects_detail_level_above_three(use_case: Any) -> None:
    """detail_level > 3 raises ValueError before any port call."""
    with pytest.raises(ValueError, match="detail_level"):
        await use_case.ask("question", 5)


async def test_ask_rejects_detail_level_validation_before_port_call(
    read_port: _FakeCommunityReadPort, use_case: Any
) -> None:
    """Validation happens before the read port is touched."""
    with pytest.raises(ValueError, match="detail_level"):
        await use_case.ask("question", 5)

    assert read_port.level_calls == []


# ── missing summaries ──────────────────────────────────────────────────────────


async def test_ask_returns_error_when_no_summaries_at_level(
    read_port: _FakeCommunityReadPort, use_case: Any
) -> None:
    """Empty summaries at the requested level return a clear instruction."""
    result = await use_case.ask("question", 1)

    assert result["answer"] == "Run scripts/run_communities.py first"
    assert result["citations"] == []


# ── MAP / REDUCE ordering ──────────────────────────────────────────────────────


async def test_ask_scores_all_summaries_at_level(
    read_port: _FakeCommunityReadPort,
    llm_port: _FakeLLMSummaryPort,
    use_case: Any,
) -> None:
    """MAP calls score_community for every summary at the requested level."""
    s1 = CommunitySummary(level=1, summary="A", entity_ids=["e1"], parent_id="p1")
    s2 = CommunitySummary(level=1, summary="B", entity_ids=["e2"], parent_id="p1")
    read_port.summaries[1] = [s1, s2]
    llm_port.scores = {s1.id: 80, s2.id: 60}

    await use_case.ask("question", 1)

    assert {summary.id for _, summary in llm_port.score_calls} == {s1.id, s2.id}


async def test_ask_composes_answer_with_top_n_summaries(
    read_port: _FakeCommunityReadPort,
    llm_port: _FakeLLMSummaryPort,
    use_case: Any,
) -> None:
    """REDUCE keeps only top-N summaries and passes them to compose_answer."""
    s1 = CommunitySummary(level=1, summary="A", entity_ids=["e1"], parent_id="p1")
    s2 = CommunitySummary(level=1, summary="B", entity_ids=["e2"], parent_id="p1")
    s3 = CommunitySummary(level=1, summary="C", entity_ids=["e3"], parent_id="p1")
    read_port.summaries[1] = [s1, s2, s3]
    llm_port.scores = {s1.id: 80, s2.id: 90, s3.id: 50}

    await use_case.ask("question", 1)

    assert llm_port.compose_calls
    question, ranked = llm_port.compose_calls[0]
    assert question == "question"
    assert len(ranked) == 2
    assert ranked[0][1] == 90
    assert ranked[1][1] == 80
    assert ranked[0][0].id == s2.id
    assert ranked[1][0].id == s1.id


async def test_ask_returns_answer_with_citations(
    read_port: _FakeCommunityReadPort,
    llm_port: _FakeLLMSummaryPort,
    use_case: Any,
) -> None:
    """The final response includes the composed answer and citation ids."""
    s1 = CommunitySummary(level=1, summary="A", entity_ids=["e1"], parent_id="p1")
    s2 = CommunitySummary(level=1, summary="B", entity_ids=["e2"], parent_id="p1")
    read_port.summaries[1] = [s1, s2]
    llm_port.scores = {s1.id: 80, s2.id: 60}
    llm_port.answer = "Answer with [Data: CommunitySummary(s1-id)]"

    result = await use_case.ask("question", 1)

    assert result["answer"] == "Answer with [Data: CommunitySummary(s1-id)]"
    assert result["citations"] == [s1.id, s2.id]


async def test_ask_honours_top_n_when_more_summaries_exist(
    read_port: _FakeCommunityReadPort,
    llm_port: _FakeLLMSummaryPort,
    use_case: Any,
) -> None:
    """REDUCE limits the number of summaries passed to compose_answer."""
    summaries = [
        CommunitySummary(level=2, summary=str(i), entity_ids=[f"e{i}"], parent_id="p1")
        for i in range(5)
    ]
    read_port.summaries[2] = summaries
    llm_port.scores = {summary.id: 100 - i for i, summary in enumerate(summaries)}

    await use_case.ask("question", 2)

    assert len(llm_port.compose_calls[0][1]) == 2
    assert llm_port.compose_calls[0][1][0][1] == 100


async def test_ask_concurrency_is_limited_by_semaphore(
    read_port: _FakeCommunityReadPort,
    llm_port: _FakeLLMSummaryPort,
) -> None:
    """Concurrent scoring respects the configured max_concurrency."""
    from book_graph_rag.application.global_query_use_case import GlobalQueryUseCase

    active = 0
    max_active = 0

    class _RecordingFakeLLMSummaryPort(_FakeLLMSummaryPort):
        async def score_community(self, question: str, summary: CommunitySummary) -> int:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await _tiny_sleep()
            active -= 1
            return 50

    recording_llm = _RecordingFakeLLMSummaryPort()
    summaries = [
        CommunitySummary(level=1, summary=str(i), entity_ids=[f"e{i}"], parent_id="p1")
        for i in range(10)
    ]
    read_port.summaries[1] = summaries
    use_case = GlobalQueryUseCase(read_port, recording_llm, max_concurrency=2, top_n=10)

    await use_case.ask("question", 1)

    assert max_active <= 2


async def _tiny_sleep() -> None:
    """Yield control so other tasks can enter the semaphore."""
    import asyncio

    await asyncio.sleep(0)
