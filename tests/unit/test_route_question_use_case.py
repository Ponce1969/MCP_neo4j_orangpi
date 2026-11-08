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
    mean_normalized,
    score_against_profiles,
    score_against_prototypes,
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


async def test_decision_reports_prototype_strategy_and_profile_k() -> None:
    """The decision names the strategy and the k of the profile set scored."""
    profiles = (
        _profile(_AAP, ((1.0, 0.0, 0.0), (0.5, 0.5, 0.0))),
        _profile(_ESSENTIAL, ((0.0, 1.0, 0.0), (0.0, 0.5, 0.5))),
    )
    use_case, _embedding, _resolver = _use_case(profiles=profiles)

    result = await use_case.execute("What is the ReAct agent pattern?")

    assert result.decision.strategy == "prototypes"
    assert result.decision.profile_k == 2
    assert result.validated_namespace == _ns(_AAP)
    # End to end: the winning candidate names its prototype text (S7).
    assert result.decision.candidates[0].best_prototype_source == "chunk:0"


async def test_empty_profile_set_reports_prototypes_strategy_without_k() -> None:
    """No profiles ⇒ profile_k is None, but the strategy still names the path."""
    use_case, embedding, resolver = _use_case(profiles=())

    result = await use_case.execute("What is the ReAct agent pattern?")

    assert embedding.calls == ["What is the ReAct agent pattern?"]
    assert result.decision.reason == "no_profiles"
    assert result.decision.route_kind == "abstain"
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

    use_case, embedding, resolver = _use_case(profiles=profiles)

    result = await use_case.execute("What is the ReAct agent pattern?")

    assert embedding.calls == ["What is the ReAct agent pattern?"]
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
