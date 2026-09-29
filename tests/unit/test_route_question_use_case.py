"""Behavior tests for the runtime route-question use case."""

from __future__ import annotations

from book_graph_rag.application.route_question_use_case import RouteQuestionUseCase
from book_graph_rag.domain.mcp_security import InvalidScopeError, ScopeContext
from book_graph_rag.domain.namespaces import SourceNamespace
from book_graph_rag.domain.routing_models import (
    LexicalHints,
    NamespaceProfile,
    RouteThresholds,
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


def _profile(namespace_id: str, centroid: tuple[float, ...]) -> NamespaceProfile:
    return NamespaceProfile(
        namespace=_ns(namespace_id),
        centroid=centroid,
        dimension=len(centroid),
        model_id="test-model",
        profile_version="1.0.0",
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
    _profile(_AAP, (1.0, 0.0, 0.0)),
    _profile(_ESSENTIAL, (0.0, 1.0, 0.0)),
    _profile(_AGENTIC, (0.0, 0.0, 1.0)),
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
    stale_profiles = (_profile("knowledge:removed-book", (1.0, 0.0, 0.0)),)
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
