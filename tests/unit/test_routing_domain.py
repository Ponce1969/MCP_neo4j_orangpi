"""Behavior tests for the namespace routing domain kernel."""

from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from book_graph_rag.domain.namespaces import SourceNamespace
from book_graph_rag.domain.routing_models import (
    LexicalHints,
    NamespaceProfile,
    RouteDecision,
    RouteThresholds,
    ScoredCandidate,
    cosine_similarity,
    decide_route,
    match_lexical_hints,
    mean_normalized,
    normalize_vector,
    route_cache_key,
    score_against_profiles,
)


def _profile(namespace_id: str, centroid: list[float]) -> NamespaceProfile:
    corpus, source = namespace_id.split(":", maxsplit=1)
    return NamespaceProfile(
        namespace=SourceNamespace(corpus=corpus, source=source),
        centroid=tuple(centroid),
        dimension=len(centroid),
        model_id="test-model",
        profile_version="1.0.0",
    )


def _ns(namespace_id: str) -> SourceNamespace:
    corpus, source = namespace_id.split(":", maxsplit=1)
    return SourceNamespace(corpus=corpus, source=source)


# ── cosine_similarity ─────────────────────────────────────────────────────────


def test_cosine_identical_vectors_is_one() -> None:
    assert cosine_similarity((1.0, 0.0, 0.0), (2.0, 0.0, 0.0)) == pytest.approx(1.0)
    assert cosine_similarity((1.0, 2.0, 3.0), (1.0, 2.0, 3.0)) == pytest.approx(1.0)


def test_cosine_orthogonal_vectors_is_zero() -> None:
    assert cosine_similarity((1.0, 0.0), (0.0, 1.0)) == pytest.approx(0.0)


def test_cosine_opposite_vectors_is_minus_one() -> None:
    assert cosine_similarity((1.0, 0.0), (-3.0, 0.0)) == pytest.approx(-1.0)


def test_cosine_zero_norm_returns_zero_not_nan() -> None:
    assert cosine_similarity((0.0, 0.0), (1.0, 2.0)) == 0.0
    assert cosine_similarity((0.0, 0.0), (0.0, 0.0)) == 0.0


def test_cosine_dimension_mismatch_raises() -> None:
    with pytest.raises(ValueError, match="same dimension"):
        cosine_similarity((1.0,), (1.0, 2.0))


# ── cache key derivation ────────────────────────────────────────────────────


def _cache_key(
    question_fingerprint: str = "fp-1",
    *,
    namespace: str | None = "knowledge:book-a",
    mode: str = "evidence",
    model_id: str = "m",
    profile_version: str = "1.0.0",
    catalog_version: str = "1",
    graph_snapshot: str = "s",
) -> str:
    """Baseline key: every dimension test overrides exactly one input."""
    return route_cache_key(
        question_fingerprint,
        namespace=namespace,
        mode=mode,
        model_id=model_id,
        profile_version=profile_version,
        catalog_version=catalog_version,
        graph_snapshot=graph_snapshot,
    )


def test_route_cache_key_is_deterministic() -> None:
    key_a = _cache_key()
    key_b = _cache_key()

    assert key_a == key_b
    assert re.fullmatch(r"[0-9a-f]{64}", key_a) is not None


def test_route_cache_key_binds_question_fingerprint() -> None:
    assert _cache_key("fp-1") != _cache_key("fp-2")


def test_route_cache_key_binds_namespace() -> None:
    baseline = _cache_key(namespace="knowledge:book-a")

    assert baseline != _cache_key(namespace="knowledge:book-b")
    assert baseline != _cache_key(namespace=None)


def test_route_cache_key_binds_mode() -> None:
    assert _cache_key(mode="evidence") != _cache_key(mode="synthesis")


def test_route_cache_key_binds_model_id() -> None:
    assert _cache_key(model_id="m") != _cache_key(model_id="other-model")


def test_route_cache_key_binds_profile_version() -> None:
    assert _cache_key(profile_version="1.0.0") != _cache_key(profile_version="1.0.1")


def test_route_cache_key_binds_catalog_version() -> None:
    assert _cache_key(catalog_version="1") != _cache_key(catalog_version="2")


def test_route_cache_key_binds_graph_snapshot() -> None:
    assert _cache_key(graph_snapshot="s") != _cache_key(graph_snapshot="s2")


@pytest.mark.parametrize("blank_mode", ["", "   "], ids=["empty", "whitespace"])
def test_route_cache_key_rejects_blank_mode(blank_mode: str) -> None:
    with pytest.raises(ValueError, match="mode"):
        _cache_key(mode=blank_mode)


def test_route_cache_key_stores_mode_verbatim_without_normalizing() -> None:
    assert _cache_key(mode="evidence") != _cache_key(mode="evidence ")


# ── lexical hints ────────────────────────────────────────────────────────────


def test_lexical_hint_matches_case_insensitive() -> None:
    hints = (
        LexicalHints(
            namespace=_ns("knowledge:essential-graphrag"),
            terms=("Text2Cypher", "Essential GraphRAG"),
        ),
        LexicalHints(
            namespace=_ns("knowledge:agentic-architectural-patterns"),
            terms=("multi-agent", "agentic patterns"),
        ),
    )

    matched = match_lexical_hints(
        "¿cómo se usa text2cypher para consultar el grafo?",
        hints,
    )

    assert [c.namespace.source_id for c in matched] == [
        "knowledge:essential-graphrag",
    ]


def test_lexical_hint_no_match_returns_empty() -> None:
    hints = (
        LexicalHints(
            namespace=_ns("knowledge:essential-graphrag"),
            terms=("Text2Cypher",),
        ),
    )

    assert match_lexical_hints("What is pasta al dente?", hints) == ()


def test_lexical_hint_multiple_matches_are_deterministic() -> None:
    hints = (
        LexicalHints(
            namespace=_ns("knowledge:graphrag-agentic"),
            terms=("dual-graph",),
        ),
        LexicalHints(
            namespace=_ns("knowledge:essential-graphrag"),
            terms=("GraphRAG",),
        ),
        LexicalHints(
            namespace=_ns("knowledge:agentic-architectural-patterns"),
            terms=("GraphRAG",),
        ),
    )

    matched = match_lexical_hints("Compare GraphRAG systems", hints)

    assert all(candidate.score == 1.0 for candidate in matched)
    assert [c.namespace.source_id for c in matched] == [
        "knowledge:agentic-architectural-patterns",
        "knowledge:essential-graphrag",
    ]


# ── centroid construction ────────────────────────────────────────────────────


def test_normalize_vector_returns_unit_norm() -> None:
    normalized = normalize_vector((3.0, 4.0))

    assert normalized == pytest.approx((0.6, 0.8))
    assert sum(value * value for value in normalized) == pytest.approx(1.0)


def test_normalize_vector_zero_norm_raises() -> None:
    with pytest.raises(ValueError, match="zero-norm"):
        normalize_vector((0.0, 0.0))


def test_mean_normalized_averages_then_normalizes() -> None:
    centroid = mean_normalized(((1.0, 0.0), (0.0, 1.0)))

    assert centroid == pytest.approx((0.7071067811865476, 0.7071067811865476))
    assert sum(value * value for value in centroid) == pytest.approx(1.0)


def test_mean_normalized_empty_raises() -> None:
    with pytest.raises(ValueError, match="zero vectors"):
        mean_normalized(())


def test_mean_normalized_dimension_mismatch_raises() -> None:
    with pytest.raises(ValueError, match="same dimension"):
        mean_normalized(((1.0, 0.0), (1.0,)))


# ── profile validation ────────────────────────────────────────────────────────


def test_profile_rejects_centroid_dimension_mismatch() -> None:
    with pytest.raises(ValidationError, match="centroid dimension"):
        NamespaceProfile(
            namespace=_ns("knowledge:book-a"),
            centroid=(1.0, 2.0),
            dimension=3,
            model_id="test-model",
            profile_version="1.0.0",
        )


# ── scoring and ranking ───────────────────────────────────────────────────────


def test_score_against_profiles_orders_by_similarity() -> None:
    question = (1.0, 0.0)
    profiles = (
        _profile("knowledge:book-b", [0.0, 1.0]),
        _profile("knowledge:book-a", [1.0, 0.0]),
    )

    ranked = score_against_profiles(question, profiles)

    assert [c.namespace.source_id for c in ranked] == [
        "knowledge:book-a",
        "knowledge:book-b",
    ]


def test_score_against_profiles_breaks_ties_deterministically() -> None:
    question = (1.0, 0.0, 0.0)
    profiles = (
        _profile("knowledge:book-b", [1.0, 0.0, 0.0]),
        _profile("knowledge:book-a", [1.0, 0.0, 0.0]),
    )

    ranked = score_against_profiles(question, profiles)

    assert [c.namespace.source_id for c in ranked] == [
        "knowledge:book-a",
        "knowledge:book-b",
    ]
    assert ranked[0].score == ranked[1].score


def test_score_against_profiles_empty_is_empty() -> None:
    assert score_against_profiles((1.0,), ()) == ()


# ── abstention policy ─────────────────────────────────────────────────────────


def test_decide_route_empty_candidates_abstains() -> None:
    decision = decide_route((), RouteThresholds())

    assert decision.route_kind == "abstain"
    assert decision.reason == "no_profiles"
    assert decision.selected is None


def test_decide_route_low_top_score_abstains() -> None:
    ranked = (
        ScoredCandidate(namespace=_ns("knowledge:book-a"), score=0.30),
        ScoredCandidate(namespace=_ns("knowledge:book-b"), score=0.20),
    )
    thresholds = RouteThresholds(min_top_score=0.45, min_margin=0.10)

    decision = decide_route(ranked, thresholds)

    assert decision.route_kind == "abstain"
    assert decision.reason == "low_score"
    assert decision.selected is None


def test_decide_route_low_margin_abstains() -> None:
    ranked = (
        ScoredCandidate(namespace=_ns("knowledge:book-a"), score=0.80),
        ScoredCandidate(namespace=_ns("knowledge:book-b"), score=0.75),
    )
    thresholds = RouteThresholds(min_top_score=0.45, min_margin=0.10)

    decision = decide_route(ranked, thresholds)

    assert decision.route_kind == "abstain"
    assert decision.reason == "low_margin"
    assert decision.selected is None


def test_low_margin_abstention_reports_the_real_margin() -> None:
    ranked = (
        ScoredCandidate(namespace=_ns("knowledge:book-a"), score=0.80),
        ScoredCandidate(namespace=_ns("knowledge:book-b"), score=0.75),
    )
    thresholds = RouteThresholds(min_top_score=0.45, min_margin=0.10)

    decision = decide_route(ranked, thresholds)

    assert decision.reason == "low_margin"
    assert decision.margin == pytest.approx(0.05)
    assert decision.top_score == pytest.approx(0.80)


def test_low_score_abstention_reports_the_real_margin() -> None:
    ranked = (
        ScoredCandidate(namespace=_ns("knowledge:book-a"), score=0.30),
        ScoredCandidate(namespace=_ns("knowledge:book-b"), score=0.20),
    )
    thresholds = RouteThresholds(min_top_score=0.45, min_margin=0.10)

    decision = decide_route(ranked, thresholds)

    assert decision.reason == "low_score"
    assert decision.margin == pytest.approx(0.10)


def test_abstention_without_profiles_reports_zero_margin() -> None:
    decision = decide_route((), RouteThresholds())

    assert decision.reason == "no_profiles"
    assert decision.top_score == 0.0
    assert decision.margin == 0.0


def test_decide_route_clear_winner_selects_namespace() -> None:
    ranked = (
        ScoredCandidate(namespace=_ns("knowledge:book-a"), score=0.90),
        ScoredCandidate(namespace=_ns("knowledge:book-b"), score=0.55),
    )

    decision = decide_route(ranked, RouteThresholds())

    assert decision.route_kind == "single"
    assert decision.reason == "clear"
    assert decision.selected == _ns("knowledge:book-a")
    assert decision.top_score == pytest.approx(0.90)
    assert decision.margin == pytest.approx(0.35)
    assert decision.confidence == pytest.approx(0.90)
    assert decision.candidates == ranked


def test_decide_route_single_candidate_margin_equals_score() -> None:
    ranked = (ScoredCandidate(namespace=_ns("knowledge:book-a"), score=0.90),)

    decision = decide_route(ranked, RouteThresholds())

    assert decision.route_kind == "single"
    assert decision.margin == pytest.approx(0.90)


def test_decide_route_tied_candidates_abstain_low_margin() -> None:
    ranked = (
        ScoredCandidate(namespace=_ns("knowledge:book-b"), score=0.80),
        ScoredCandidate(namespace=_ns("knowledge:book-a"), score=0.80),
    )

    decision = decide_route(ranked, RouteThresholds())

    assert decision.route_kind == "abstain"
    assert decision.reason == "low_margin"
    assert decision.selected is None
    assert decision.candidates[0].namespace == _ns("knowledge:book-a")


def test_route_decision_carries_route_kind_and_candidates() -> None:
    decision = RouteDecision(
        route_kind="abstain",
        reason="test",
    )

    assert decision.route_kind == "abstain"
    assert decision.candidates == ()
    assert decision.selected is None
