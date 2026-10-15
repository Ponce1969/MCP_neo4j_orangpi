"""Global retrieval must be question-aware: the same ranking production uses.

Neo4j is never contacted and no LLM is called for real: the community read port and the
LLM summary port are injected, so these tests pin the contract only — which summaries the
layer-5 global path selects, in which order, out of how many, and against what question.

The defect these tests describe: ``fetch_contexts(qtype="global")`` used to return every
community summary at the level, in database order, without ever looking at the question, so
three different questions produced the same 164-context blob and the layer-5 metrics measured
self-consistency against a fixed input instead of retrieval quality.
"""

from __future__ import annotations

from typing import Any, cast

from book_graph_rag.config import Settings
from book_graph_rag.domain.models import CommunitySummary
from book_graph_rag.infrastructure.neo4j_retrieval_adapter import Neo4jRetrievalAdapter


class _FakeReadPort:
    """Community read port that records what it was asked for."""

    def __init__(self, summaries: dict[int, list[CommunitySummary]]) -> None:
        self.summaries = summaries
        self.levels_asked: list[int] = []

    async def get_summaries_by_level(self, level: int, *, scope: Any = None) -> list[Any]:
        self.levels_asked.append(level)
        return list(self.summaries.get(level, []))

    async def close(self) -> None:
        return None


class _FakeScoringLLM:
    """Scores summaries per question and records the batches it was handed."""

    def __init__(self, scores_by_question: dict[str, dict[str, int]]) -> None:
        self._scores = scores_by_question
        self.questions: list[str] = []
        self.batches: list[tuple[str, ...]] = []
        self.composed: list[str] = []

    async def score_communities(
        self, question: str, summaries: tuple[CommunitySummary, ...]
    ) -> dict[str, int]:
        self.questions.append(question)
        self.batches.append(tuple(summary.id for summary in summaries))
        per_question = self._scores.get(question, {})
        return {summary.id: per_question.get(summary.id, 0) for summary in summaries}

    async def compose_answer(
        self, question: str, ranked: list[tuple[CommunitySummary, int]]
    ) -> str:
        self.composed.append(question)
        return "fake answer"


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "neo4j_uri": "bolt://localhost:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "not-a-real-password",
    }
    base.update(overrides)
    return Settings.model_validate(base)


def _summaries(count: int) -> list[CommunitySummary]:
    """Summaries with the model's own derived ids (never hand-written)."""
    return [
        CommunitySummary(
            level=1,
            summary=f"summary {i}",
            entity_ids=[f"e{i}"],
            parent_id="p1",
        )
        for i in range(count)
    ]


def _adapter(
    read_port: _FakeReadPort,
    llm: _FakeScoringLLM,
    *,
    settings: Settings,
) -> Neo4jRetrievalAdapter:
    return Neo4jRetrievalAdapter(
        settings,
        llm_adapter=cast(Any, llm),
        query_adapter=cast(Any, object()),
        community_adapter=cast(Any, read_port),
    )


async def test_global_selection_depends_on_the_question() -> None:
    """Two different questions must select different summaries."""
    summaries = _summaries(4)
    read_port = _FakeReadPort({1: summaries})
    # q-a cares about the fourth summary; q-b about the first. Everything else scores 0.
    llm = _FakeScoringLLM({"q-a": {summaries[3].id: 90}, "q-b": {summaries[0].id: 80}})
    adapter = _adapter(read_port, llm, settings=_settings(ask_global_top_n=1))

    first = await adapter.fetch_contexts(question="q-a", qtype="global", detail_level=1)
    second = await adapter.fetch_contexts(question="q-b", qtype="global", detail_level=1)

    assert [context.text for context in first] == ["summary 3"]
    assert [context.text for context in second] == ["summary 0"]
    assert llm.questions == ["q-a", "q-b"]


async def test_global_selection_keeps_top_n_in_score_order() -> None:
    """Only ``ask_global_top_n`` survive, highest score first."""
    summaries = _summaries(5)
    read_port = _FakeReadPort({1: summaries})
    llm = _FakeScoringLLM(
        {
            "q": {
                summaries[0].id: 10,
                summaries[1].id: 50,
                summaries[2].id: 30,
                summaries[3].id: 40,
                summaries[4].id: 20,
            }
        }
    )
    adapter = _adapter(read_port, llm, settings=_settings(ask_global_top_n=3))

    contexts = await adapter.fetch_contexts(question="q", qtype="global", detail_level=1)

    assert [context.text for context in contexts] == ["summary 1", "summary 3", "summary 2"]


async def test_global_selection_batches_scoring_by_the_configured_size() -> None:
    """Scoring travels in batches of ``ask_global_score_batch_size``, not one call per summary."""
    summaries = _summaries(5)
    read_port = _FakeReadPort({1: summaries})
    llm = _FakeScoringLLM({"q": {}})
    adapter = _adapter(
        read_port, llm, settings=_settings(ask_global_top_n=5, ask_global_score_batch_size=2)
    )

    await adapter.fetch_contexts(question="q", qtype="global", detail_level=1)

    assert llm.batches == [
        (summaries[0].id, summaries[1].id),
        (summaries[2].id, summaries[3].id),
        (summaries[4].id,),
    ]


async def test_global_selection_reads_one_level_once() -> None:
    """The summaries of the requested level are read exactly once."""
    read_port = _FakeReadPort({1: _summaries(3)})
    llm = _FakeScoringLLM({"q": {}})
    adapter = _adapter(read_port, llm, settings=_settings())

    await adapter.fetch_contexts(question="q", qtype="global", detail_level=1)

    assert read_port.levels_asked == [1]


async def test_global_contexts_never_claim_a_chunk_id() -> None:
    """Summaries are not chunks: their contexts carry ``chunk_id=None``."""
    read_port = _FakeReadPort({1: _summaries(2)})
    llm = _FakeScoringLLM({"q": {}})
    adapter = _adapter(read_port, llm, settings=_settings(ask_global_top_n=2))

    contexts = await adapter.fetch_contexts(question="q", qtype="global", detail_level=1)

    assert all(context.chunk_id is None for context in contexts)
