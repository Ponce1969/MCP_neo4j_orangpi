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
    blend_strategy,
    cosine_similarity,
    decide_route,
    match_lexical_hints,
    mean_normalized,
    normalize_vector,
    route_cache_key,
    score_against_profiles,
    score_against_prototypes,
    score_blend,
    select_prototypes,
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


def _profile_v2(
    namespace_id: str,
    centroid: tuple[float, ...],
    prototypes: tuple[tuple[float, ...], ...],
    sources: tuple[str, ...],
) -> NamespaceProfile:
    """Full v2 profile: prototype block explicitly present."""
    return NamespaceProfile(
        namespace=_ns(namespace_id),
        centroid=centroid,
        dimension=len(centroid),
        model_id="test-model",
        profile_version="2.0.0",
        prototypes=prototypes,
        k=len(prototypes),
        prototype_sources=sources,
    )


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
    # S7: no prototype was involved on the lexical fast path.
    assert all(candidate.best_prototype_source is None for candidate in matched)


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


def test_profile_rejects_prototype_dimension_mismatch() -> None:
    with pytest.raises(ValidationError, match="prototype 0 dimension"):
        NamespaceProfile(
            namespace=_ns("knowledge:book-a"),
            centroid=(1.0, 0.0),
            dimension=2,
            model_id="test-model",
            profile_version="2.0.0",
            prototypes=((1.0, 0.0, 0.0),),
            k=1,
            prototype_sources=("chunk:0",),
        )


def test_profile_rejects_mismatched_prototype_sources_length() -> None:
    with pytest.raises(ValidationError, match="prototype_sources length"):
        NamespaceProfile(
            namespace=_ns("knowledge:book-a"),
            centroid=(1.0, 0.0),
            dimension=2,
            model_id="test-model",
            profile_version="2.0.0",
            prototypes=((1.0, 0.0),),
            k=1,
            prototype_sources=(),
        )


def test_profile_rejects_empty_prototypes() -> None:
    with pytest.raises(ValidationError, match="prototypes must not be empty"):
        NamespaceProfile(
            namespace=_ns("knowledge:book-a"),
            centroid=(1.0, 0.0),
            dimension=2,
            model_id="test-model",
            profile_version="2.0.0",
            prototypes=(),
            k=0,
            prototype_sources=(),
        )


def test_profile_rejects_k_below_one() -> None:
    with pytest.raises(ValidationError, match="k must be >= 1"):
        NamespaceProfile(
            namespace=_ns("knowledge:book-a"),
            centroid=(1.0, 0.0),
            dimension=2,
            model_id="test-model",
            profile_version="2.0.0",
            prototypes=((1.0, 0.0),),
            k=0,
            prototype_sources=("chunk:0",),
        )


def test_profile_rejects_k_mismatching_prototype_count() -> None:
    with pytest.raises(ValidationError, match="does not match prototypes length"):
        NamespaceProfile(
            namespace=_ns("knowledge:book-a"),
            centroid=(1.0, 0.0),
            dimension=2,
            model_id="test-model",
            profile_version="2.0.0",
            prototypes=((1.0, 0.0), (0.0, 1.0)),
            k=1,
            prototype_sources=("chunk:0", "chunk:1"),
        )


def test_profile_without_prototype_block_stays_loadable_for_centroid_path() -> None:
    """Legacy v1 construction is valid in memory (it keeps the live router
    green); artifact readers still reject it — see the store tests."""
    profile = _profile("knowledge:book-a", [1.0, 0.0])

    assert profile.prototypes == ()
    assert profile.k == 0
    assert profile.prototype_sources == ()


def test_profile_accepts_absent_central_centroid() -> None:
    """T6/L10: optional on purpose — the pre-T6 production artifact must
    keep loading under the default scoring."""
    profile = _profile("knowledge:book-a", [1.0, 0.0])

    assert profile.central_centroid is None


def test_profile_accepts_central_centroid_matching_dimension() -> None:
    profile = NamespaceProfile(
        namespace=_ns("knowledge:book-a"),
        centroid=(1.0, 0.0),
        central_centroid=(0.0, 1.0),
        dimension=2,
        model_id="test-model",
        profile_version="2.0.0",
        prototypes=((1.0, 0.0),),
        k=1,
        prototype_sources=("chunk:0",),
    )

    assert profile.central_centroid == (0.0, 1.0)


def test_profile_rejects_central_centroid_dimension_mismatch() -> None:
    """When present, ``central_centroid`` is validated exactly like
    ``centroid`` (T6/S1): length must equal ``dimension``."""
    with pytest.raises(ValidationError, match="central_centroid dimension"):
        NamespaceProfile(
            namespace=_ns("knowledge:book-a"),
            centroid=(1.0, 0.0),
            central_centroid=(1.0, 0.0, 0.0),
            dimension=2,
            model_id="test-model",
            profile_version="1.0.0",
        )


# ── prototype selection (S3) ─────────────────────────────────────────────────

_SELECTION_VECTORS = (
    (1.0, 0.0, 0.0),
    (0.0, 1.0, 0.0),
    (0.0, 0.0, 1.0),
    (-1.0, 0.0, 0.0),
    (0.5, 0.5, 0.0),
    (0.2, -0.3, 0.9),
)


def test_select_prototypes_is_deterministic_across_runs() -> None:
    first = select_prototypes(_SELECTION_VECTORS, 3)
    second = select_prototypes(_SELECTION_VECTORS, 3)

    assert first == second
    assert len(first) == 3
    assert len(set(first)) == 3
    assert all(0 <= index < len(_SELECTION_VECTORS) for index in first)


def test_select_prototypes_spans_tight_and_far_clusters() -> None:
    """The point of the ADR: a mean describes neither cluster, two
    farthest-point prototypes take one from each."""
    tight = tuple((1.0, 0.01 * offset) for offset in range(10))
    far = ((-1.0, 0.05), (-1.0, -0.05))
    vectors = (*tight, *far)

    selected = select_prototypes(vectors, 2)

    assert len(selected) == 2
    assert len(set(selected)) == 2
    # The seed is closest to the mean of the whole set: +x-leaning, so tight.
    assert selected[0] < 10, "seed must come from the tight cluster"
    assert sum(1 for index in selected if index < 10) == 1
    assert sum(1 for index in selected if index >= 10) == 1


def test_select_prototypes_k_one_returns_closest_to_mean() -> None:
    """k=1 degenerates to today's behaviour: the single mean seed."""
    vectors = _SELECTION_VECTORS
    mean = tuple(sum(vector[index] for vector in vectors) / len(vectors) for index in range(3))
    similarities = [cosine_similarity(vector, mean) for vector in vectors]
    best = max(similarities)
    expected_index = similarities.index(best)  # lowest index wins a tie

    assert select_prototypes(vectors, 1) == (expected_index,)


def test_select_prototypes_returns_all_when_fewer_vectors_than_k() -> None:
    three = _SELECTION_VECTORS[:3]

    selected = select_prototypes(three, 8)

    assert len(selected) == 3
    assert set(selected) == {0, 1, 2}


def test_select_prototypes_rejects_invalid_inputs() -> None:
    with pytest.raises(ValueError, match="k must be >= 1"):
        select_prototypes(_SELECTION_VECTORS, 0)
    with pytest.raises(ValueError, match="zero vectors"):
        select_prototypes((), 2)
    with pytest.raises(ValueError, match="same dimension"):
        select_prototypes(((1.0, 0.0), (1.0,)), 1)


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
    # The centroid path scores a mean, never a prototype: no source to report.
    assert all(candidate.best_prototype_source is None for candidate in ranked)


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


def test_score_against_prototypes_takes_the_best_prototype() -> None:
    profiles = (
        _profile_v2(
            "knowledge:book-a",
            (1.0, 0.0),
            ((0.0, 1.0), (0.7071, 0.7071)),
            ("chunk:0", "chunk:1"),
        ),
        _profile_v2(
            "knowledge:book-b",
            (0.0, 1.0),
            ((0.0, 1.0),),
            ("chunk:0",),
        ),
    )

    ranked = score_against_prototypes((1.0, 0.0), profiles)

    # book-a wins on its *best* prototype, not on any average of them.
    assert [c.namespace.source_id for c in ranked] == [
        "knowledge:book-a",
        "knowledge:book-b",
    ]
    assert ranked[0].score == pytest.approx(0.7071067811865475)
    assert ranked[1].score == pytest.approx(0.0)
    # S7: the score names the prototype that produced it.
    assert ranked[0].best_prototype_source == "chunk:1"
    assert ranked[1].best_prototype_source == "chunk:0"


def test_score_against_prototypes_tie_reports_first_prototype_in_selection_order() -> None:
    """A tie for the best prototype resolves to the lowest prototype index.

    Both prototypes sit at the same cosine from the question, so the score
    cannot tell them apart; the reported source stays deterministic because
    the argmax uses strict ``>`` — the same lowest-index rule
    ``select_prototypes`` uses (S3) — never the last one or an arbitrary one.
    """
    question = (1.0, 0.0)
    profiles = (
        _profile_v2(
            "knowledge:book-a",
            (1.0, 0.0),
            ((1.0, 1.0), (1.0, -1.0)),
            ("chunk:0", "chunk:7"),
        ),
    )

    ranked = score_against_prototypes(question, profiles)

    first = cosine_similarity(question, (1.0, 1.0))
    second = cosine_similarity(question, (1.0, -1.0))
    assert first == second, "the fixture must tie exactly"
    assert ranked[0].score == pytest.approx(first)
    assert ranked[0].best_prototype_source == "chunk:0"


def test_prototype_scoring_ignores_centroid() -> None:
    """S1/L2: the centroid is a passive diagnostic. Corrupting it changes no
    score on the prototype path."""
    prototypes_a = ((1.0, 0.0), (0.3, 0.4))
    prototypes_b = ((0.0, 1.0), (0.0, 0.5))
    honest = (
        _profile_v2(
            "knowledge:book-a", mean_normalized(prototypes_a), prototypes_a, ("c:0", "c:1")
        ),
        _profile_v2(
            "knowledge:book-b", mean_normalized(prototypes_b), prototypes_b, ("c:0", "c:1")
        ),
    )
    garbage = (99.0, -77.0)
    corrupted = (
        _profile_v2("knowledge:book-a", garbage, prototypes_a, ("c:0", "c:1")),
        _profile_v2("knowledge:book-b", garbage, prototypes_b, ("c:0", "c:1")),
    )
    question = (1.0, 0.0)

    from_honest_centroid = score_against_prototypes(question, honest)
    from_garbage_centroid = score_against_prototypes(question, corrupted)

    assert from_honest_centroid == from_garbage_centroid
    assert from_honest_centroid[0].namespace.source_id == "knowledge:book-a"
    assert from_honest_centroid[0].score == pytest.approx(1.0)


def test_prototype_scoring_rejects_profile_without_prototypes() -> None:
    legacy = _profile("knowledge:book-a", [1.0, 0.0])

    with pytest.raises(ValueError, match="no prototypes"):
        score_against_prototypes((1.0,), (legacy,))


def test_prototype_scoring_empty_is_empty() -> None:
    assert score_against_prototypes((1.0,), ()) == ()


# ── blend scoring (T5: the centroid default behind ROUTER_BLEND_W) ───────────


def _blend_profiles() -> tuple[NamespaceProfile, NamespaceProfile]:
    """Two books whose centroid and prototype geometries disagree.

    book-a's three orthogonal prototypes average to (1,1,1)/sqrt(3) — far
    from the question — while one of them sits exactly on it. book-b's two
    prototypes average onto the question while neither reaches it. Same
    fixture family as ``_multimodal_profiles`` in the use-case tests, with
    the centroid derived exactly as the builder derives it.
    """
    prototypes_a = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    prototypes_b = ((0.6, 0.8, 0.0), (0.6, -0.8, 0.0))
    return (
        _profile_v2(
            "knowledge:book-a",
            mean_normalized(prototypes_a),
            prototypes_a,
            ("chunk:0", "chunk:1", "chunk:2"),
        ),
        _profile_v2(
            "knowledge:book-b",
            mean_normalized(prototypes_b),
            prototypes_b,
            ("chunk:0", "chunk:1"),
        ),
    )


def test_blend_w_one_reproduces_centroid_scores_value_for_value() -> None:
    """T5/S5 (L5): at w=1.0 the blend scores exactly like the centroid path.

    Value for value — the same floats and the same order, not an
    approximation — while S7 provenance stays filled (diagnostic, not
    causal: no fragment decided anything at w=1.0).
    """
    question = (1.0, 0.0, 0.0)
    profiles = _blend_profiles()

    centroid_ranking = score_against_profiles(question, profiles)
    blended = score_blend(question, profiles, blend_w=1.0)

    assert [(c.namespace.source_id, c.score) for c in blended] == [
        (c.namespace.source_id, c.score) for c in centroid_ranking
    ]
    # Provenance available even though the prototype term has weight zero.
    assert [c.best_prototype_source for c in blended] == ["chunk:0", "chunk:0"]


def test_blend_w_zero_reproduces_prototype_scoring_including_sources() -> None:
    """T5/S2+S7: at w=0.0 the blend IS the prototype path, source for source.

    Full model equality: scores, ordering and ``best_prototype_source`` are
    bit for bit identical to ``score_against_prototypes``.
    """
    question = (1.0, 0.0, 0.0)
    profiles = _blend_profiles()

    assert score_blend(question, profiles, blend_w=0.0) == score_against_prototypes(
        question, profiles
    )


def test_blend_w_half_is_the_arithmetic_mean_of_both_component_scores() -> None:
    """w=0.5 is the plain mean of both component scores, hand-computed.

    Question (1, 0, 0):
      book-a: centroid cosine 0.5773502691896258 (mean (1,1,1)/sqrt(3)),
              best prototype 1.0
              => 0.5 * 0.5773502691896258 + 0.5 * 1.0 = 0.7886751345948129
      book-b: centroid cosine 1.0, best prototype 0.6
              => 0.5 * 1.0 + 0.5 * 0.6 = 0.8
    """
    question = (1.0, 0.0, 0.0)
    profiles = _blend_profiles()

    blended = score_blend(question, profiles, blend_w=0.5)
    centroid_by_id = {
        c.namespace.source_id: c.score for c in score_against_profiles(question, profiles)
    }
    prototype_by_id = {
        c.namespace.source_id: c.score for c in score_against_prototypes(question, profiles)
    }
    blended_by_id = {c.namespace.source_id: c.score for c in blended}

    for source_id, score in blended_by_id.items():
        assert score == 0.5 * centroid_by_id[source_id] + 0.5 * prototype_by_id[source_id]

    assert blended_by_id["knowledge:book-a"] == pytest.approx(0.7886751345948129)
    assert blended_by_id["knowledge:book-b"] == pytest.approx(0.8)
    # The blend ranks book-b first, unlike the prototype endpoint (book-a).
    assert [c.namespace.source_id for c in blended] == ["knowledge:book-b", "knowledge:book-a"]


def test_blend_strategy_labels_the_weight_endpoints() -> None:
    """S7/T5: three labels, no fourth; 'prototypes' only when that term scores."""
    assert blend_strategy(1.0) == "centroid"
    assert blend_strategy(0.0) == "prototypes"
    assert blend_strategy(0.3) == "blend"
    assert blend_strategy(0.5) == "blend"
    with pytest.raises(ValueError, match="blend_w"):
        blend_strategy(-0.1)
    with pytest.raises(ValueError, match="blend_w"):
        blend_strategy(1.1)


def test_score_blend_rejects_out_of_range_weight() -> None:
    """The weight is a range, not a hint: both endpoints rejected loudly."""
    with pytest.raises(ValueError, match="blend_w"):
        score_blend((1.0,), (), blend_w=-0.1)
    with pytest.raises(ValueError, match="blend_w"):
        score_blend((1.0,), (), blend_w=1.1)


def test_score_blend_empty_is_empty() -> None:
    assert score_blend((1.0,), (), blend_w=1.0) == ()
    assert score_blend((1.0,), (), blend_w=0.0) == ()
    assert score_blend((1.0,), (), blend_w=0.5) == ()


def test_blend_requires_prototypes_only_when_the_term_has_weight() -> None:
    """A legacy v1 profile still scores at w=1.0 (no source), fails below it.

    At the centroid endpoint the prototype term cannot contribute, so a
    prototype-less profile is scored honestly with ``best_prototype_source =
    None``; as soon as the term carries weight (``w < 1.0``) the profile is
    invalid input and fails loudly, like ``score_against_prototypes``.
    """
    legacy = _profile("knowledge:book-a", [1.0, 0.0])

    at_centroid = score_blend((1.0, 0.0), (legacy,), blend_w=1.0)
    assert at_centroid[0].score == pytest.approx(1.0)
    assert at_centroid[0].best_prototype_source is None

    with pytest.raises(ValueError, match="no prototypes"):
        score_blend((1.0, 0.0), (legacy,), blend_w=0.5)


# ── T6 (L9/L10): ROUTER_SCORING — the central geometry, switchable, no fallback ──


def _central_profile(
    namespace_id: str,
    *,
    centroid: tuple[float, ...],
    central: tuple[float, ...],
    prototype: tuple[float, ...] = (0.0, 1.0),
) -> NamespaceProfile:
    """v2 profile whose mean and central geometries are set independently."""
    return NamespaceProfile(
        namespace=_ns(namespace_id),
        centroid=centroid,
        central_centroid=central,
        dimension=len(centroid),
        model_id="test-model",
        profile_version="2.0.0",
        prototypes=(prototype,),
        k=1,
        prototype_sources=("chunk:0",),
    )


def test_score_blend_central_scores_the_central_vector_at_w_one() -> None:
    """Hand numbers, question (1, 0): centroid (0, 1) scores 0.0, central
    (1, 0) scores 1.0 — the global term is the central vector, full stop."""
    profile = _central_profile("knowledge:book-a", centroid=(0.0, 1.0), central=(1.0, 0.0))

    ranked = score_blend((1.0, 0.0), (profile,), blend_w=1.0, scoring="central")

    assert ranked[0].score == pytest.approx(1.0)


def test_score_blend_central_never_reads_the_centroid() -> None:
    """T6/S1 symmetry: under scoring=central the passive ``centroid`` is as
    unreadable as the ``central_centroid`` is under scoring=centroid."""
    honest = _central_profile("knowledge:book-a", centroid=(0.0, 1.0), central=(1.0, 0.0))
    corrupted = _central_profile("knowledge:book-a", centroid=(0.6, 0.8), central=(1.0, 0.0))
    question = (1.0, 0.0)

    assert score_blend(question, (honest,), blend_w=1.0, scoring="central") == score_blend(
        question, (corrupted,), blend_w=1.0, scoring="central"
    )


def test_default_scoring_ignores_central_centroid_whether_present_or_absent() -> None:
    """T6: the default route cannot move because a field appeared or is
    missing — byte-identical scores either way (S5, L10's safe default)."""
    with_field = _central_profile("knowledge:book-a", centroid=(1.0, 0.0), central=(0.0, 1.0))
    without_field = NamespaceProfile(
        namespace=_ns("knowledge:book-a"),
        centroid=(1.0, 0.0),
        dimension=2,
        model_id="test-model",
        profile_version="2.0.0",
        prototypes=((0.0, 1.0),),
        k=1,
        prototype_sources=("chunk:0",),
    )
    question = (1.0, 0.0)

    from_field = score_blend(question, (with_field,), blend_w=1.0)
    from_no_field = score_blend(question, (without_field,), blend_w=1.0)

    assert from_field == from_no_field
    assert from_field[0].score == pytest.approx(1.0)


def test_score_blend_central_fails_closed_when_central_centroid_missing() -> None:
    """No silent fallback to centroid — at **any** weight — and the message
    names the field and the rebuild (T6/L10)."""
    legacy = _profile("knowledge:book-a", [1.0, 0.0])  # no central_centroid

    for blend_w in (1.0, 0.5, 0.0):
        with pytest.raises(ValueError, match="central_centroid") as excinfo:
            score_blend((1.0, 0.0), (legacy,), blend_w=blend_w, scoring="central")
        message = str(excinfo.value)
        assert "central_centroid" in message
        assert "scripts/build_namespace_profiles.py" in message


def test_score_blend_central_with_empty_profiles_is_empty() -> None:
    """No profiles to fail on: composition keeps composing (abstains later)."""
    assert score_blend((1.0,), (), blend_w=1.0, scoring="central") == ()


def test_score_blend_rejects_unknown_scoring() -> None:
    with pytest.raises(ValueError, match="scoring"):
        score_blend((1.0,), (), blend_w=1.0, scoring="mean")


def test_score_blend_central_mid_weight_blends_central_with_prototypes() -> None:
    """Hand numbers, question (1, 0), w = 0.5:
    scoring=central → 0.5 * cos(q, central (1,0)) 1.0
                      + 0.5 * best proto cos(q, (0,1)) 0.0 = 0.5
    scoring=centroid → 0.5 * cos(q, centroid (0,1)) 0.0
                       + 0.5 * 0.0 = 0.0
    """
    profile = _central_profile("knowledge:book-a", centroid=(0.0, 1.0), central=(1.0, 0.0))
    question = (1.0, 0.0)

    central_blend = score_blend(question, (profile,), blend_w=0.5, scoring="central")
    centroid_blend = score_blend(question, (profile,), blend_w=0.5, scoring="centroid")

    assert central_blend[0].score == pytest.approx(0.5)
    assert centroid_blend[0].score == pytest.approx(0.0)


def test_blend_strategy_names_the_geometry_that_actually_scored() -> None:
    """T6/S7: 'central' at w=1.0 only under scoring=central; 'prototypes'
    owns w=0.0 under either setting; 'blend' owns the middle either way."""
    assert blend_strategy(1.0) == "centroid"
    assert blend_strategy(1.0, scoring="central") == "central"
    assert blend_strategy(0.0, scoring="central") == "prototypes"
    assert blend_strategy(0.3, scoring="central") == "blend"
    assert blend_strategy(0.5, scoring="central") == "blend"
    with pytest.raises(ValueError, match="scoring"):
        blend_strategy(1.0, scoring="bogus")


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


def test_decide_route_passes_through_strategy_and_profile_k() -> None:
    """S7: the kernel records the provenance the scoring path reports."""
    ranked = (ScoredCandidate(namespace=_ns("knowledge:book-a"), score=0.90),)

    decision = decide_route(
        ranked,
        RouteThresholds(),
        strategy="prototypes",
        profile_k=8,
    )

    assert decision.strategy == "prototypes"
    assert decision.profile_k == 8
    # Abstentions carry the same provenance passthrough.
    abstain = decide_route((), RouteThresholds(), strategy="prototypes")
    assert abstain.strategy == "prototypes"
    assert abstain.profile_k is None
    # A caller with nothing to report gets the honest defaults, not a guess.
    bare = decide_route(ranked, RouteThresholds())
    assert bare.strategy == "unspecified"
    assert bare.profile_k is None
