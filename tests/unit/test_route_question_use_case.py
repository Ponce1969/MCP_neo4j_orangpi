"""Behavior tests for the runtime route-question use case."""

from __future__ import annotations

import pytest

from book_graph_rag.application.route_question_use_case import RouteQuestionUseCase
from book_graph_rag.domain.mcp_security import InvalidScopeError, ScopeContext
from book_graph_rag.domain.namespaces import SourceNamespace
from book_graph_rag.domain.routing_models import (
    LexicalHints,
    NamespaceProfile,
    RouteThresholds,
    ScoredCandidate,
    decide_route,
    mean_normalized,
    score_against_profiles,
    score_against_prototypes,
    score_blend,
)
from book_graph_rag.ports.embedding_provider_port import (
    EmbeddingBatch,
    EmbeddingProviderPort,
    EmbeddingRequest,
    EmbeddingVector,
)
from book_graph_rag.ports.namespace_profile_port import NamespaceProfilePort
from book_graph_rag.ports.scope_resolver_port import ScopeResolverPort

_AAP = "knowledge:agentic-architectural-patterns"
_ESSENTIAL = "knowledge:essential-graphrag"
_AGENTIC = "knowledge:graphrag-agentic"

_ACTIVE = {_AAP, _ESSENTIAL, _AGENTIC}


def _ns(namespace_id: str) -> SourceNamespace:
    corpus, source = namespace_id.split(":", maxsplit=1)
    return SourceNamespace(corpus=corpus, source=source)


def _profile(namespace_id: str, prototypes: tuple[tuple[float, ...], ...]) -> NamespaceProfile:
    """Build a v2 profile (S1): prototypes scored, centroid diagnostic only.

    The centroid is derived exactly as the builder derives it (mean of the
    prototype vectors, re-normalized) so these fixtures carry the same
    passive diagnostic a real artifact would.
    """
    return NamespaceProfile(
        namespace=_ns(namespace_id),
        centroid=mean_normalized(prototypes),
        dimension=len(prototypes[0]),
        model_id="test-model",
        profile_version="2.0.0",
        prototypes=prototypes,
        k=len(prototypes),
        prototype_sources=tuple(f"chunk:{index}" for index in range(len(prototypes))),
    )


class _FakeEmbedding(EmbeddingProviderPort):
    """Deterministic question embeddings by signal substring."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def embed(self, request: EmbeddingRequest) -> EmbeddingBatch:
        self.calls.extend(request.texts)
        question = request.texts[0].lower()
        if "text2cypher" in question or "graphrag evaluation" in question:
            values = (0.0, 1.0, 0.0)
        elif "dual" in question or "graph memory" in question:
            values = (0.0, 0.0, 1.0)
        elif "pasta" in question or "weather" in question:
            values = (0.0, 0.0, 0.0)
        elif "compare" in question or "y " in question:
            values = (1.0, 1.0, 0.0)
        else:
            values = (1.0, 0.0, 0.0)
        return EmbeddingBatch(
            model_id=request.model_id,
            vectors=[EmbeddingVector(values=values, model_id=request.model_id)],
        )

    def model_dim(self, model_id: str) -> int:
        return 3


class _FakeProfiles(NamespaceProfilePort):
    def __init__(self, profiles: tuple[NamespaceProfile, ...]) -> None:
        self._profiles = profiles

    async def load_profiles(self) -> tuple[NamespaceProfile, ...]:
        return self._profiles


class _FakeScopeResolver(ScopeResolverPort):
    def __init__(self) -> None:
        self.checked: list[str] = []

    def resolve(
        self,
        source_id: str,
        *,
        book_ids: tuple[str, ...] = (),
        entity_types: tuple[str, ...] = (),
        relationship_types: tuple[str, ...] = (),
    ) -> ScopeContext:
        del book_ids, entity_types, relationship_types
        self.checked.append(source_id)
        if source_id not in _ACTIVE:
            raise InvalidScopeError(f"unknown source: {source_id}", scope_id=source_id)
        return ScopeContext(source=SourceNamespace.parse_book_id(source_id))


_PROFILES = (
    # One prototype per book: the compat shape that must behave exactly like
    # the old centroid path (single-prototype equivalence, asserted below).
    _profile(_AAP, ((1.0, 0.0, 0.0),)),
    _profile(_ESSENTIAL, ((0.0, 1.0, 0.0),)),
    _profile(_AGENTIC, ((0.0, 0.0, 1.0),)),
)

_HINTS = (
    LexicalHints(namespace=_ns(_ESSENTIAL), terms=("Text2Cypher",)),
    LexicalHints(namespace=_ns(_AGENTIC), terms=("dual-graph",)),
)


def _use_case(
    profiles: tuple[NamespaceProfile, ...] = _PROFILES,
    embedding: _FakeEmbedding | None = None,
    thresholds: RouteThresholds | None = None,
    blend_w: float = 1.0,
    scoring: str = "centroid",
) -> tuple[RouteQuestionUseCase, _FakeEmbedding, _FakeScopeResolver]:
    fake_embedding = embedding or _FakeEmbedding()
    resolver = _FakeScopeResolver()
    use_case = RouteQuestionUseCase(
        fake_embedding,
        _FakeProfiles(profiles),
        resolver,
        model_id="test-model",
        thresholds=thresholds,
        lexical_hints=_HINTS,
        blend_w=blend_w,
        scoring=scoring,
    )
    return use_case, fake_embedding, resolver


async def test_lexical_hint_routes_without_embedding() -> None:
    use_case, embedding, _resolver = _use_case()

    result = await use_case.execute("¿Cómo se usa Text2Cypher para consultar el grafo?")

    assert result.validated_namespace == _ns(_ESSENTIAL)
    assert embedding.calls == []
    # S7: the fast path names itself — no prototype was involved, no k exists.
    assert result.decision.strategy == "lexical_hints"
    assert result.decision.profile_k is None


async def test_clear_embedding_winner_is_validated() -> None:
    use_case, embedding, resolver = _use_case()

    result = await use_case.execute("What is the ReAct agent pattern?")

    assert result.validated_namespace == _ns(_AAP)
    assert embedding.calls == ["What is the ReAct agent pattern?"]
    assert resolver.checked == [_AAP]


async def test_low_margin_abstains_with_two_validated_fanout() -> None:
    use_case, _embedding, _resolver = _use_case()

    result = await use_case.execute("Compare GraphRAG systems")

    assert result.validated_namespace is None
    assert result.decision.route_kind == "abstain"
    assert result.decision.reason == "low_margin"
    assert [ns.source_id for ns in result.fanout_namespaces] == [_AAP, _ESSENTIAL]


async def test_out_of_domain_abstains_without_fanout() -> None:
    use_case, _embedding, resolver = _use_case()

    result = await use_case.execute("What is the best pasta recipe?")

    assert result.validated_namespace is None
    assert result.decision.reason == "low_score"
    assert result.fanout_namespaces == ()
    assert resolver.checked == []


async def test_stale_profile_is_rejected_by_catalog() -> None:
    stale_profiles = (_profile("knowledge:removed-book", ((1.0, 0.0, 0.0),)),)
    use_case, _embedding, resolver = _use_case(profiles=stale_profiles)

    result = await use_case.execute("Any question about patterns")

    assert result.validated_namespace is None
    assert result.fanout_namespaces == ()
    assert "knowledge:removed-book" in resolver.checked


async def test_cross_language_spanish_question_routes() -> None:
    use_case, _embedding, _resolver = _use_case()

    result = await use_case.execute("¿Qué diferencia hay entre el grafo vertical y el dual-graph?")

    assert result.validated_namespace == _ns(_AGENTIC)


async def test_custom_thresholds_change_the_decision() -> None:
    strict = RouteThresholds(min_top_score=0.999)
    use_case, _embedding, _resolver = _use_case(thresholds=strict)

    result = await use_case.execute("Compare GraphRAG systems")

    assert result.decision.route_kind == "abstain"
    assert result.decision.reason == "low_score"
    assert result.validated_namespace is None


# ── S7: the decision explains itself ───────────────────────────────────────


async def test_decision_reports_default_centroid_strategy_and_profile_k() -> None:
    """The decision names the default strategy and the k of the profile set.

    T5/L5: the default weight (w=1.0) scores centroids, so the strategy must
    say ``"centroid"`` — and S7 provenance stays filled end to end: the
    winning candidate still names its prototype text, which at w=1.0 is
    diagnostic rather than causal (S7/L5).
    """
    profiles = (
        _profile(_AAP, ((1.0, 0.0, 0.0), (0.5, 0.5, 0.0))),
        _profile(_ESSENTIAL, ((0.0, 1.0, 0.0), (0.0, 0.5, 0.5))),
    )
    use_case, _embedding, _resolver = _use_case(profiles=profiles)

    result = await use_case.execute("What is the ReAct agent pattern?")

    assert result.decision.strategy == "centroid"
    assert result.decision.profile_k == 2
    assert result.validated_namespace == _ns(_AAP)
    # End to end: the winning candidate names its prototype text (S7).
    assert result.decision.candidates[0].best_prototype_source == "chunk:0"


async def test_empty_profile_set_reports_default_strategy_without_k() -> None:
    """No profiles ⇒ profile_k is None, but the strategy still names the path.

    T5: with the default w=1.0 the path that found nothing is the centroid
    path, so it must not claim prototypes.
    """
    use_case, embedding, resolver = _use_case(profiles=())

    result = await use_case.execute("What is the ReAct agent pattern?")

    assert embedding.calls == ["What is the ReAct agent pattern?"]
    assert result.decision.reason == "no_profiles"
    assert result.decision.route_kind == "abstain"
    assert result.decision.strategy == "centroid"
    assert result.decision.profile_k is None
    assert resolver.checked == []


async def test_empty_profile_set_at_w_zero_reports_prototypes() -> None:
    """T5 triangulation: the empty set names the path its weight selected."""
    use_case, embedding, resolver = _use_case(profiles=(), blend_w=0.0)

    result = await use_case.execute("What is the ReAct agent pattern?")

    assert embedding.calls == ["What is the ReAct agent pattern?"]
    assert result.decision.reason == "no_profiles"
    assert result.decision.strategy == "prototypes"
    assert result.decision.profile_k is None
    assert resolver.checked == []


# ── S2: the geometry change, pinned end to end ───────────────────────────────


def _multimodal_profiles() -> tuple[NamespaceProfile, NamespaceProfile]:
    """Two books engineered so the two geometries disagree.

    Question direction is (1, 0, 0) — the ``_FakeEmbedding`` else-branch.

    Book A (``_AAP``): three orthogonal prototypes ((1,0,0), (0,1,0),
    (0,0,1)). Its mean centroid is (1,1,1)/√3, whose cosine with the question
    is 1/√3 ≈ 0.5773502691896258 — *farther* than book B's centroid. But its
    prototype (1,0,0) sits exactly on the question: max-cosine = 1.0.

    Book B (``_ESSENTIAL``): two prototypes (0.6,0.8,0) and (0.6,-0.8,0),
    both unit vectors whose cosine with the question is 0.6. Their mean is
    (0.6,0,0) → centroid (1,0,0): cosine 1.0 with the question, i.e. B's
    centroid is closer to the question than *any* of B's own prototypes.

    T5 made the centroid the default (w=1.0), so this behavioural pin of the
    prototype endpoint selects it explicitly with ``blend_w=0.0``.
    """
    return (
        _profile(_AAP, ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))),
        _profile(_ESSENTIAL, ((0.6, 0.8, 0.0), (0.6, -0.8, 0.0))),
    )


async def test_far_centroid_with_one_close_prototype_beats_nearer_centroid() -> None:
    """The behavioural point of the ADR (S2): max-pooling, not the mean.

    Book A's mean centroid (cosine 0.5773502691896258) is farther from the
    question than book B's mean centroid (cosine 1.0), yet A owns one
    prototype exactly on the question (cosine 1.0) while B's best prototype
    only reaches 0.6. Under ``score_against_prototypes`` A must win; the
    legacy centroid ranking — asserted here, so the inversion is explicit —
    would have picked B.
    """
    profiles = _multimodal_profiles()
    question_vector = (1.0, 0.0, 0.0)

    # Old geometry, stated as evidence: centroid B (1.0) beats centroid A
    # (0.5773502691896258), so the centroid router would select _ESSENTIAL.
    centroid_ranking = score_against_profiles(question_vector, profiles)
    assert [c.namespace.source_id for c in centroid_ranking] == [_ESSENTIAL, _AAP]
    assert centroid_ranking[0].score == pytest.approx(1.0)
    assert centroid_ranking[1].score == pytest.approx(0.5773502691896258)

    # T5: the default is the centroid, so the prototype endpoint is explicit.
    use_case, embedding, resolver = _use_case(profiles=profiles, blend_w=0.0)

    result = await use_case.execute("What is the ReAct agent pattern?")

    assert embedding.calls == ["What is the ReAct agent pattern?"]
    assert result.decision.strategy == "prototypes"
    assert result.validated_namespace == _ns(_AAP)
    assert result.decision.route_kind == "single"
    assert result.decision.top_score == pytest.approx(1.0)
    assert result.decision.margin == pytest.approx(0.4)  # 1.0 − 0.6
    assert resolver.checked == [_AAP]

    # And the live path really is max-over-prototypes: A = 1.0, B = 0.6.
    prototype_ranking = score_against_prototypes(question_vector, profiles)
    assert [c.namespace.source_id for c in prototype_ranking] == [_AAP, _ESSENTIAL]
    assert prototype_ranking[1].score == pytest.approx(0.6)


async def test_question_matching_no_prototype_still_abstains() -> None:
    """A question orthogonal to every prototype abstains: no accidental commit."""
    profiles = (
        _profile(_AAP, ((0.0, 1.0, 0.0),)),
        _profile(_ESSENTIAL, ((0.0, 0.0, 1.0),)),
    )
    use_case, _embedding, resolver = _use_case(profiles=profiles)

    result = await use_case.execute("What is the ReAct agent pattern?")

    assert result.decision.route_kind == "abstain"
    assert result.decision.reason == "low_score"
    assert result.decision.top_score == 0.0
    assert result.validated_namespace is None
    assert result.fanout_namespaces == ()
    assert resolver.checked == []


async def test_single_prototype_profiles_match_centroid_path_exactly() -> None:
    """Compat (S2): one prototype per book degenerates to the old behaviour.

    Cosine ignores scale and a single prototype's centroid is that prototype
    re-normalized, so both scoring functions must agree vector-for-vector,
    and the use case must reproduce the centroid-era low-margin fan-out.
    """
    for question_vector in ((1.0, 1.0, 0.0), (0.2, 0.5, 0.8), (0.0, 0.0, 1.0)):
        prototype_ranking = score_against_prototypes(question_vector, _PROFILES)
        centroid_ranking = score_against_profiles(question_vector, _PROFILES)
        # Identical geometry vector-for-vector; only S7 provenance differs:
        # the centroid path reports no source instead of inventing one.
        assert (
            tuple(
                candidate.model_copy(update={"best_prototype_source": None})
                for candidate in prototype_ranking
            )
            == centroid_ranking
        )

    use_case, _embedding, _resolver = _use_case()

    result = await use_case.execute("Compare GraphRAG systems")

    assert result.decision.reason == "low_margin"
    assert [ns.source_id for ns in result.fanout_namespaces] == [_AAP, _ESSENTIAL]


# ── T5: the blend weight decides geometry AND strategy (L5/L6) ───────────────


async def test_blend_w_one_scores_exactly_the_centroid_path() -> None:
    """S2/S5 (L5): w=1.0 — same floats as the centroid path, strategy
    ``"centroid"``, and provenance still filled (diagnostic, not causal)."""
    profiles = _multimodal_profiles()
    use_case, _embedding, _resolver = _use_case(profiles=profiles, blend_w=1.0)

    result = await use_case.execute("What is the ReAct agent pattern?")

    question_vector = (1.0, 0.0, 0.0)
    centroid_ranking = score_against_profiles(question_vector, profiles)
    assert result.decision.strategy == "centroid"
    assert result.decision.profile_k == 3
    # Value for value: strip only the provenance field, then full equality.
    stripped = tuple(
        candidate.model_copy(update={"best_prototype_source": None})
        for candidate in result.decision.candidates
    )
    assert stripped == centroid_ranking
    # S7/L5: the source stays available even though it decided nothing.
    assert all(
        candidate.best_prototype_source is not None for candidate in result.decision.candidates
    )


async def test_blend_w_zero_scores_exactly_the_prototype_path() -> None:
    """S2/S7: w=0.0 — scores AND best_prototype_source identical to the
    prototype path, strategy ``"prototypes"``."""
    profiles = _multimodal_profiles()
    use_case, _embedding, _resolver = _use_case(profiles=profiles, blend_w=0.0)

    result = await use_case.execute("What is the ReAct agent pattern?")

    question_vector = (1.0, 0.0, 0.0)
    prototype_ranking = score_against_prototypes(question_vector, profiles)
    assert result.decision.strategy == "prototypes"
    assert tuple(result.decision.candidates) == prototype_ranking


async def test_blend_w_half_means_both_components_and_says_blend() -> None:
    """S2: w=0.5 scores the arithmetic mean of both components, strategy
    ``"blend"``.

    Hand numbers for question (1, 0, 0) on ``_multimodal_profiles``:
      book-a: 0.5 * 0.5773502691896258 (centroid) + 0.5 * 1.0 (best proto)
              = 0.7886751345948129
      book-b: 0.5 * 1.0 (centroid) + 0.5 * 0.6 (best proto) = 0.8
    """
    profiles = _multimodal_profiles()
    use_case, _embedding, _resolver = _use_case(profiles=profiles, blend_w=0.5)

    result = await use_case.execute("What is the ReAct agent pattern?")

    question_vector = (1.0, 0.0, 0.0)
    centroid_by_id = {
        c.namespace.source_id: c.score for c in score_against_profiles(question_vector, profiles)
    }
    prototype_by_id = {
        c.namespace.source_id: c.score for c in score_against_prototypes(question_vector, profiles)
    }
    assert result.decision.strategy == "blend"
    assert len(result.decision.candidates) == 2
    for candidate in result.decision.candidates:
        source_id = candidate.namespace.source_id
        assert candidate.score == 0.5 * centroid_by_id[source_id] + 0.5 * prototype_by_id[source_id]

    by_id = {c.namespace.source_id: c.score for c in result.decision.candidates}
    assert by_id[_AAP] == pytest.approx(0.7886751345948129)
    assert by_id[_ESSENTIAL] == pytest.approx(0.8)
    assert [c.namespace.source_id for c in result.decision.candidates] == [_ESSENTIAL, _AAP]


async def test_w_one_preserves_the_pre_t1_centroid_route_exactly() -> None:
    """S5 regression guard: default w=1.0 cannot change a route vs main.

    For a fixed question and profiles, the default blend must agree with the
    pre-T1 centroid behaviour — ``score_against_profiles`` →
    ``decide_route`` → catalog validation — on route_kind, margin (exact
    float equality, not approx) and validated_namespace, so merging this
    branch cannot move a route relative to the centroid baseline.
    """
    scenarios = (
        # (question, the vector _FakeEmbedding returns for it, profiles)
        ("What is the ReAct agent pattern?", (1.0, 0.0, 0.0), _multimodal_profiles()),
        ("Compare GraphRAG systems", (1.0, 1.0, 0.0), _PROFILES),
        ("What is the best pasta recipe?", (0.0, 0.0, 0.0), _PROFILES),
    )
    for question, question_vector, profiles in scenarios:
        # Default blend_w=1.0 — the shipped configuration.
        use_case, _embedding, _resolver = _use_case(profiles=profiles)

        result = await use_case.execute(question)

        # The pre-T1 behaviour, assembled from the primitives main used.
        reference = decide_route(
            score_against_profiles(question_vector, profiles),
            RouteThresholds(),
        )
        expected_validated = (
            reference.selected
            if reference.route_kind == "single"
            and reference.selected is not None
            and reference.selected.source_id in _ACTIVE
            else None
        )
        assert result.decision.strategy == "centroid"
        assert result.decision.route_kind == reference.route_kind
        assert result.decision.margin == reference.margin  # exact float equality
        assert result.decision.top_score == reference.top_score
        assert result.validated_namespace == expected_validated


# ── T6 (L10): ROUTER_SCORING — switchable, safe default, no fallback ────────


def _central_profile(
    namespace_id: str,
    *,
    centroid: tuple[float, ...],
    central: tuple[float, ...],
) -> NamespaceProfile:
    """Profile whose mean and central geometries are set independently (T6).

    One prototype per book so the w=0.0 endpoint stays scoreable; the
    prototype equals the book's mean, as the builder would derive it.
    """
    return NamespaceProfile(
        namespace=_ns(namespace_id),
        centroid=centroid,
        central_centroid=central,
        dimension=len(centroid),
        model_id="test-model",
        profile_version="2.0.0",
        prototypes=(centroid,),
        k=1,
        prototype_sources=("chunk:0",),
    )


# Two books whose mean and central geometries point in opposite directions.
_CENTRAL_PROFILES = (
    _central_profile(_AAP, centroid=(0.0, 1.0, 0.0), central=(1.0, 0.0, 0.0)),
    _central_profile(_ESSENTIAL, centroid=(1.0, 0.0, 0.0), central=(0.0, 1.0, 0.0)),
)


async def test_scoring_centroid_reproduces_todays_scores_exactly() -> None:
    """S5/T6: scoring=centroid (the default) must not move one float.

    Checked against ``score_against_profiles`` — today's centroid path,
    assembled from the primitives main used — on profiles **without** the
    new field (the shipped artifact's shape) and on profiles **with** it
    (it must stay unread). Exact model equality: same scores, same order,
    same provenance.
    """
    question_vector = (1.0, 0.0, 0.0)  # _FakeEmbedding else-branch

    for profiles in (_multimodal_profiles(), _CENTRAL_PROFILES):
        use_case, _embedding, _resolver = _use_case(profiles=profiles, scoring="centroid")

        result = await use_case.execute("What is the ReAct agent pattern?")

        reference = score_against_profiles(question_vector, profiles)
        stripped = tuple(
            candidate.model_copy(update={"best_prototype_source": None})
            for candidate in result.decision.candidates
        )
        assert stripped == reference  # byte-identical scores and ordering
        # And against today's live path with the shipped defaults: full model
        # equality, S7 provenance included.
        assert tuple(result.decision.candidates) == score_blend(
            question_vector, profiles, blend_w=1.0
        )
        assert result.decision.strategy == "centroid"
        expected = decide_route(
            reference, RouteThresholds(), strategy="centroid", profile_k=profiles[0].k
        )
        assert result.decision.route_kind == expected.route_kind
        assert result.decision.top_score == expected.top_score  # exact float equality
        assert result.decision.margin == expected.margin  # exact float equality


async def test_scoring_central_scores_the_central_vector_and_reports_central() -> None:
    """S2/T6 hand numbers, question (1, 0, 0) (the else-branch):

      _AAP:       cos(q, centroid (0,1,0)) 0.0 · cos(q, central (1,0,0)) 1.0
      _ESSENTIAL: cos(q, centroid (1,0,0)) 1.0 · cos(q, central (0,1,0)) 0.0

    ⇒ scoring=central inverts the default winner (top 1.0, margin 1.0) and
    the decision names the geometry that actually ran.
    """
    # Contrast first: the default picks _ESSENTIAL.
    default_use_case, _embedding, _resolver = _use_case(
        profiles=_CENTRAL_PROFILES, scoring="centroid"
    )
    default_result = await default_use_case.execute("What is the ReAct agent pattern?")
    assert default_result.validated_namespace == _ns(_ESSENTIAL)
    assert default_result.decision.strategy == "centroid"

    # … scoring=central picks the other book, on the hand-computed scores.
    use_case, embedding, resolver = _use_case(profiles=_CENTRAL_PROFILES, scoring="central")

    result = await use_case.execute("What is the ReAct agent pattern?")

    assert embedding.calls == ["What is the ReAct agent pattern?"]
    assert result.decision.strategy == "central"
    assert result.decision.profile_k == 1
    by_id = {
        candidate.namespace.source_id: candidate.score for candidate in result.decision.candidates
    }
    assert by_id[_AAP] == pytest.approx(1.0)
    assert by_id[_ESSENTIAL] == pytest.approx(0.0)
    assert result.decision.top_score == pytest.approx(1.0)
    assert result.decision.margin == pytest.approx(1.0)
    assert result.decision.route_kind == "single"
    assert result.validated_namespace == _ns(_AAP)
    assert resolver.checked == [_AAP]


async def test_w_zero_reports_prototypes_under_either_scoring_setting() -> None:
    """T6 triangulation: at w=0.0 the global term has zero weight, so both
    settings run — and report — the prototype path, bit for bit identical."""
    strategies: dict[str, str] = {}
    rankings: dict[str, tuple[ScoredCandidate, ...]] = {}
    for scoring in ("centroid", "central"):
        use_case, _embedding, _resolver = _use_case(
            profiles=_CENTRAL_PROFILES, blend_w=0.0, scoring=scoring
        )
        result = await use_case.execute("What is the ReAct agent pattern?")
        strategies[scoring] = result.decision.strategy
        rankings[scoring] = tuple(result.decision.candidates)

    assert strategies == {"centroid": "prototypes", "central": "prototypes"}
    assert rankings["centroid"] == rankings["central"]


async def test_scoring_central_on_profile_without_field_fails_closed() -> None:
    """No silent fallback to the centroid: the error names the field and
    the rebuild command (T6/L10), at the scoring point itself."""
    use_case, _embedding, _resolver = _use_case(profiles=_multimodal_profiles(), scoring="central")

    with pytest.raises(ValueError, match="central_centroid") as excinfo:
        await use_case.execute("What is the ReAct agent pattern?")

    message = str(excinfo.value)
    assert "central_centroid" in message
    assert "scripts/build_namespace_profiles.py" in message
