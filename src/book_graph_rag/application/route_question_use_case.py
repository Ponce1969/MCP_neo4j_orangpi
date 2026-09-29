"""Application use case: route a question to one validated namespace.

The router is a classifier, not an authorization mechanism: its output is a
candidate namespace that must still pass the catalog-backed scope resolver.
It never issues an unscoped graph query and never changes MCP signatures.
"""

from __future__ import annotations

from book_graph_rag.domain.mcp_security import InvalidScopeError
from book_graph_rag.domain.namespaces import SourceNamespace
from book_graph_rag.domain.routing_models import (
    LexicalHints,
    ResolvedRoute,
    RouteDecision,
    RouteThresholds,
    ScoredCandidate,
    decide_route,
    match_lexical_hints,
    score_against_profiles,
)
from book_graph_rag.ports.embedding_provider_port import (
    EmbeddingProviderPort,
    EmbeddingRequest,
)
from book_graph_rag.ports.namespace_profile_port import NamespaceProfilePort
from book_graph_rag.ports.scope_resolver_port import ScopeResolverPort


class RouteQuestionUseCase:
    """Route a question to a validated namespace, abstaining when unsure.

    The lexical stage is a cheap deterministic fast path; the local embedding
    stage is the fallback. MCP ``require_scope`` and catalog validation remain
    the security boundary; this use case only ever proposes candidates.
    """

    def __init__(
        self,
        embedding_port: EmbeddingProviderPort,
        profiles_port: NamespaceProfilePort,
        scope_resolver: ScopeResolverPort,
        *,
        model_id: str,
        thresholds: RouteThresholds | None = None,
        lexical_hints: tuple[LexicalHints, ...] = (),
    ) -> None:
        self._embedding_port = embedding_port
        self._profiles_port = profiles_port
        self._scope_resolver = scope_resolver
        self._model_id = model_id
        self._thresholds = thresholds or RouteThresholds()
        self._lexical_hints = lexical_hints

    async def execute(self, question: str) -> ResolvedRoute:
        """Return the resolved route for ``question``, never fabricating scope."""
        decision = decide_route(await self.score_question(question), self._thresholds)
        return await self._validate(decision)

    async def score_question(self, question: str) -> tuple[ScoredCandidate, ...]:
        """Score the question: lexical hints first, embedding fallback.

        Exposed publicly so calibration can sweep thresholds over cached scores
        without re-embedding.
        """
        hints = match_lexical_hints(question, self._lexical_hints)
        if hints:
            return hints
        batch = await self._embedding_port.embed(
            EmbeddingRequest(texts=(question,), model_id=self._model_id)
        )
        question_vector = batch.vectors[0].values
        profiles = await self._profiles_port.load_profiles()
        return score_against_profiles(question_vector, profiles)

    async def _validate(self, decision: RouteDecision) -> ResolvedRoute:
        """Validate candidates against the catalog-backed resolver.

        A clear winner is validated alone. Fan-out applies only to ambiguity
        (``low_margin``); a low-score or stale outcome proposes nothing.
        """
        if decision.selected is not None:
            if self._is_valid(decision.selected):
                return ResolvedRoute(
                    decision=decision,
                    validated_namespace=decision.selected,
                    fanout_namespaces=(),
                )
            return ResolvedRoute(
                decision=decision,
                validated_namespace=None,
                fanout_namespaces=(),
            )

        if decision.reason != "low_margin":
            return ResolvedRoute(
                decision=decision,
                validated_namespace=None,
                fanout_namespaces=(),
            )

        validated: list[SourceNamespace] = []
        for candidate in decision.candidates[:2]:
            if self._is_valid(candidate.namespace):
                validated.append(candidate.namespace)
        return ResolvedRoute(
            decision=decision,
            validated_namespace=None,
            fanout_namespaces=tuple(validated[:2]),
        )

    def _is_valid(self, namespace: SourceNamespace) -> bool:
        """Return whether the catalog-backed resolver accepts ``namespace``."""
        try:
            self._scope_resolver.resolve(namespace.source_id)
        except InvalidScopeError:
            return False
        return True
