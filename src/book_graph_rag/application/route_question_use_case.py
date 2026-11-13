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
    blend_strategy,
    decide_route,
    match_lexical_hints,
    score_blend,
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
        blend_w: float = 1.0,
        scoring: str = "centroid",
    ) -> None:
        self._embedding_port = embedding_port
        self._profiles_port = profiles_port
        self._scope_resolver = scope_resolver
        self._model_id = model_id
        self._thresholds = thresholds or RouteThresholds()
        self._lexical_hints = lexical_hints
        # T5/L5: 1.0 = centroid only (the measured default), 0.0 = prototypes
        # only, in between = weighted sum. ``score_blend`` re-validates the
        # range on every call, so an out-of-range weight fails loudly.
        self._blend_w = blend_w
        # T6/L10: which global vector the weighted term scores — "centroid"
        # (the default: routes identical to main) or "central" (the L9
        # hypothesis geometry). ``score_blend``/``blend_strategy`` re-validate
        # the label on every call, and "central" on a profile without
        # ``central_centroid`` fails closed there.
        self._scoring = scoring

    async def execute(self, question: str) -> ResolvedRoute:
        """Return the resolved route for ``question``, never fabricating scope.

        The decision carries S7 provenance: ``strategy`` and ``profile_k``
        are populated from the profile set actually scored, so the outcome
        explains itself instead of only reporting scores.
        """
        candidates, strategy, profile_k = await self._score(question)
        decision = decide_route(
            candidates,
            self._thresholds,
            strategy=strategy,
            profile_k=profile_k,
        )
        return await self._validate(decision)

    async def score_question(self, question: str) -> tuple[ScoredCandidate, ...]:
        """Score the question: lexical hints first, embedding fallback.

        Exposed publicly so calibration can sweep thresholds over cached scores
        without re-embedding. It returns the ranking only; the provenance
        (strategy and k) is reported by ``execute`` on the decision.
        """
        candidates, _strategy, _profile_k = await self._score(question)
        return candidates

    async def _score(
        self,
        question: str,
    ) -> tuple[tuple[ScoredCandidate, ...], str, int | None]:
        """Score ``question``, reporting which strategy and k produced it (S7).

        The lexical fast path returns ``("lexical_hints", None)``: no
        embedding ran and no prototype was consulted, so there is no k to
        report. The embedding path scores through ``score_blend`` and reports
        the strategy its weight selects (T5, decisions L5/L6): ``"centroid"``
        at the default ``blend_w = 1.0`` (or ``"central"`` when
        ``scoring == "central"``, T6/L10 — the label always names the global
        vector that actually scored), ``"prototypes"`` at ``0.0``,
        ``"blend"`` strictly in between — never ``"prototypes"`` when the
        prototype term has weight zero — with the ``k`` of the loaded profile
        set (the builder writes one ``--k`` for the whole set), and
        ``profile_k=None`` when the set is empty, while the strategy still
        names the path that found nothing.
        """
        hints = match_lexical_hints(question, self._lexical_hints)
        if hints:
            return hints, "lexical_hints", None
        batch = await self._embedding_port.embed(
            EmbeddingRequest(texts=(question,), model_id=self._model_id)
        )
        question_vector = batch.vectors[0].values
        profiles = await self._profiles_port.load_profiles()
        # T5/T6: one scoring path. The weight decides the pooling, the
        # scoring setting decides the global vector (S2 as amended by L5/L6
        # and L10): the centroid at the default 1.0 + "centroid", the central
        # vector under "central", max-pooling at 0.0, the weighted sum in
        # between. Provenance is filled either way (S7).
        candidates = score_blend(
            question_vector, profiles, blend_w=self._blend_w, scoring=self._scoring
        )
        profile_k = profiles[0].k if profiles else None
        return candidates, blend_strategy(self._blend_w, scoring=self._scoring), profile_k

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
