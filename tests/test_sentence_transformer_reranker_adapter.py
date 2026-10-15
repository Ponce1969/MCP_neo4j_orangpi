"""Unit tests for ``SentenceTransformerRerankerAdapter`` with an injected fake model.

No Hugging Face download happens here: the adapter takes the CrossEncoder
through its constructor, so the tests pin ordering, conservation, determinism
and tie-breaking without touching the network.
"""

from __future__ import annotations

from typing import Any

import pytest

from book_graph_rag.config import Settings
from book_graph_rag.domain.evaluation_models import RetrievalContext
from book_graph_rag.infrastructure.sentence_transformer_reranker_adapter import (
    SentenceTransformerRerankerAdapter,
)


class _FakeCrossEncoder:
    """Deterministic stand-in for a CrossEncoder: candidate text -> fixed logit."""

    def __init__(self, scores_by_text: dict[str, float]) -> None:
        self._scores = scores_by_text
        self.calls: list[list[tuple[str, str]]] = []

    def predict(self, pairs: list[tuple[str, str]], batch_size: int = 32) -> list[float]:
        self.calls.append(pairs)
        return [self._scores[text] for _, text in pairs]


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "neo4j_uri": "bolt://localhost:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "not-a-real-password",
    }
    base.update(overrides)
    return Settings.model_validate(base)


def _ctx(text: str, chunk_id: str | None = None) -> RetrievalContext:
    return RetrievalContext(chunk_id=chunk_id, text=text)


async def test_orders_by_score_descending_and_truncates_to_top_k() -> None:
    model = _FakeCrossEncoder({"low": 0.1, "high": 0.9, "mid": 0.5})
    adapter = SentenceTransformerRerankerAdapter(_settings(), model=model)
    candidates = (_ctx("low", "b:1"), _ctx("high", "b:2"), _ctx("mid", "b:3"))
    result = await adapter.rerank(query="q", candidates=candidates, top_k=2)
    assert [c.text for c in result] == ["high", "mid"]


async def test_result_is_sub_multiset_of_the_input_objects() -> None:
    model = _FakeCrossEncoder({"a": 0.2, "b": 0.8})
    adapter = SentenceTransformerRerankerAdapter(_settings(), model=model)
    candidates = (_ctx("a", "b:1"), _ctx("b", "b:2"))
    result = await adapter.rerank(query="q", candidates=candidates, top_k=5)
    assert len(result) == 2
    assert all(any(item is original for original in candidates) for item in result)


async def test_is_deterministic_across_calls() -> None:
    model = _FakeCrossEncoder({"a": 0.2, "b": 0.8, "c": 0.5})
    adapter = SentenceTransformerRerankerAdapter(_settings(), model=model)
    candidates = (_ctx("a", "b:1"), _ctx("b", "b:2"), _ctx("c", "b:3"))
    first = await adapter.rerank(query="q", candidates=candidates, top_k=3)
    second = await adapter.rerank(query="q", candidates=candidates, top_k=3)
    assert [c.text for c in first] == [c.text for c in second]


async def test_ties_put_chunk_bearing_before_entities_then_original_order() -> None:
    model = _FakeCrossEncoder({"e1": 0.5, "c1": 0.5, "e2": 0.5, "c2": 0.5})
    adapter = SentenceTransformerRerankerAdapter(_settings(), model=model)
    candidates = (
        _ctx("e1", None),
        _ctx("c1", "b:1"),
        _ctx("e2", None),
        _ctx("c2", "b:2"),
    )
    result = await adapter.rerank(query="q", candidates=candidates, top_k=4)
    assert [c.text for c in result] == ["c1", "c2", "e1", "e2"]


async def test_empty_candidates_return_empty_without_a_model_call() -> None:
    model = _FakeCrossEncoder({})
    adapter = SentenceTransformerRerankerAdapter(_settings(), model=model)
    assert await adapter.rerank(query="q", candidates=(), top_k=10) == ()
    assert model.calls == []


async def test_non_positive_top_k_returns_empty() -> None:
    model = _FakeCrossEncoder({"a": 0.1})
    adapter = SentenceTransformerRerankerAdapter(_settings(), model=model)
    assert await adapter.rerank(query="q", candidates=(_ctx("a", "b:1"),), top_k=0) == ()


async def test_predict_receives_query_candidate_pairs() -> None:
    model = _FakeCrossEncoder({"a": 0.1})
    adapter = SentenceTransformerRerankerAdapter(_settings(), model=model)
    await adapter.rerank(query="the-question", candidates=(_ctx("a", "b:1"),), top_k=1)
    assert model.calls == [[("the-question", "a")]]


@pytest.mark.parametrize(("top_k", "overfetch"), [(10, 40), (10, 10)])
def test_settings_defaults_cover_the_documented_top_k(top_k: int, overfetch: int) -> None:
    settings = Settings.model_validate(
        {
            "neo4j_uri": "bolt://localhost:7687",
            "neo4j_user": "neo4j",
            "neo4j_password": "not-a-real-password",
            "retrieval_top_k": top_k,
            "retrieval_overfetch_n": overfetch,
        }
    )
    assert settings.rerank_enabled is False
    assert settings.reranker_model_id == "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"


def test_settings_reject_overfetch_smaller_than_top_k() -> None:
    with pytest.raises(ValueError, match="must be >= retrieval_top_k"):
        _settings(retrieval_top_k=10, retrieval_overfetch_n=5)


def test_reranker_for_is_flag_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    """ONE rule for every wiring site: off ⇒ None (never constructed), on ⇒ instance.

    Not constructing it when off is the point: it keeps a deployment that never
    enables the reranker from downloading the cross-encoder at boot.
    """
    import book_graph_rag.infrastructure.sentence_transformer_reranker_adapter as module

    built: list[Settings] = []

    class _FakeReranker:
        def __init__(self, settings: Settings) -> None:
            built.append(settings)

    monkeypatch.setattr(module, "SentenceTransformerRerankerAdapter", _FakeReranker)

    assert module.reranker_for(_settings(rerank_enabled=False)) is None
    assert built == []

    on_settings = _settings(rerank_enabled=True)
    on = module.reranker_for(on_settings)
    assert isinstance(on, _FakeReranker)
    assert built == [on_settings]
