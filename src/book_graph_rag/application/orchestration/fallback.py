"""Evidence-based fallback for a routed question: one outcome, three states (S6-S8).

Spec home: ``odd/tasks/tool-intent-router.md``, Regla 2. The resolver never
retries blindly (S7) and never fabricates graph evidence (S6):

* validated scope with evidence        → ``covered``: graph only;
* ``low_margin`` + validated fan-out   → ``fanout``: one graph query per top-2
  scope in route order, merged by citation id — per L2 (5/5 measured
  ``low_margin`` abstentions) this is the normal path, not an error (S8);
* abstain without usable fan-out, or a graph miss → ``insufficient_coverage``
  derived to text passages (S7 / S6).

Each port is called at most once per scope; there are no retries, and port
exceptions propagate untouched — a failure is never rewritten into an outcome.
"""

from __future__ import annotations

from book_graph_rag.application.orchestration.ports import GraphEvidencePort, TextPassagePort
from book_graph_rag.domain.namespaces import SourceNamespace
from book_graph_rag.domain.orchestration_outcomes import (
    CoverageState,
    FallbackOutcome,
    GraphEvidence,
)
from book_graph_rag.domain.routing_models import ResolvedRoute


async def resolve_with_fallback(
    question: str,
    route: ResolvedRoute,
    *,
    graph: GraphEvidencePort,
    text: TextPassagePort,
    passage_limit: int = 5,
) -> FallbackOutcome:
    """Resolve ``question`` for ``route`` into one explicit coverage state.

    Branch order mirrors how :class:`ResolvedRoute` is produced by
    ``RouteQuestionUseCase``: a validated single scope first, then S8's
    ``low_margin`` fan-out, then S7's text derivation. The first two states are
    mutually exclusive in a well-formed route; a route with neither a
    validated scope nor a usable fan-out takes the S7 path without ever
    querying the graph.

    Args:
        question: The question verbatim, as the caller sent it.
        route: The resolved route (decision plus validated/fan-out scopes).
        graph: Scoped graph evidence port; called at most once per scope.
        text: Text passage port; called at most once, and only when the graph
            does not cover the question.
        passage_limit: Upper bound handed to ``text.passages``.

    Returns:
        A frozen ``FallbackOutcome`` in exactly one of ``covered`` (S6 happy
        path), ``fanout`` (S8) or ``insufficient_coverage`` (S6/S7).

    Raises:
        Whatever a port raises — exceptions propagate, never converted.
    """
    reason = route.decision.reason

    # S6 covered / missed branches: only a catalog-validated namespace ever
    # reaches the graph (S4: the router proposes, the catalog disposes).
    if route.validated_namespace is not None:
        scope = route.validated_namespace
        evidence = await graph.query(question, scope)
        if evidence is None:
            # S6: no entity found. Never synthesize a GraphEvidence from the
            # question — report the missing scope in the reason and derive to
            # text. The scope produced no evidence, so it is not in `scopes`.
            passages = await text.passages(question, limit=passage_limit)
            return FallbackOutcome(
                state=CoverageState.INSUFFICIENT_COVERAGE,
                passages=passages,
                reason=f"{reason}: no graph evidence for scope "
                f"{scope.source_id}; derived to text passages",
            )
        # Covered: graph only. The text port is deliberately NOT called here —
        # fetching passages on demand is S5/T3, a different unit of work.
        return FallbackOutcome(
            state=CoverageState.COVERED,
            evidence=(evidence,),
            scopes=(scope,),
            reason=f"{reason}: graph evidence for scope {scope.source_id}",
        )

    # S8: a low_margin abstention with a validated top-2 fan-out is not an
    # error. Query every fan-out scope exactly once (route order), merge by
    # citation id, and do NOT fall back to text just because the router
    # abstained — text only happens below, when no scope produced evidence.
    if reason == "low_margin" and route.fanout_namespaces:
        merged: list[GraphEvidence] = []
        scopes_with_evidence: list[SourceNamespace] = []
        seen_citations: set[str] = set()
        for scope in route.fanout_namespaces:
            evidence = await graph.query(question, scope)
            if evidence is None:
                continue
            scopes_with_evidence.append(scope)
            deduped = _merge_citations(evidence, seen_citations)
            if deduped is not None:
                merged.append(deduped)
        if scopes_with_evidence:
            produced = ", ".join(scope.source_id for scope in scopes_with_evidence)
            return FallbackOutcome(
                state=CoverageState.FANOUT,
                evidence=tuple(merged),
                scopes=tuple(scopes_with_evidence),
                reason=f"{reason}: fan-out merged evidence from {produced}",
            )
        # Every fan-out scope missed: S6 applies per scope — derive to text.
        missing = ", ".join(scope.source_id for scope in route.fanout_namespaces)
        passages = await text.passages(question, limit=passage_limit)
        return FallbackOutcome(
            state=CoverageState.INSUFFICIENT_COVERAGE,
            passages=passages,
            reason=f"{reason}: no graph evidence in fan-out scopes "
            f"{missing}; derived to text passages",
        )

    # S7: an abstention (or any route with no queryable validated scope)
    # derives to the text port on the router's own reason — the graph is not
    # queried at all, and this is an evidence-based decision, not a retry.
    passages = await text.passages(question, limit=passage_limit)
    return FallbackOutcome(
        state=CoverageState.INSUFFICIENT_COVERAGE,
        passages=passages,
        reason=f"{reason}: no queryable validated scope; derived to text passages",
    )


def _merge_citations(evidence: GraphEvidence, seen: set[str]) -> GraphEvidence | None:
    """Return ``evidence`` minus already-merged citation ids, or ``None``.

    Deduplication is by citation id across the whole fan-out merge, preserving
    first-seen order: an id already contributed by an earlier scope is dropped
    from this item, and an item whose ids were all seen before is collapsed
    away entirely (it is a duplicate, S8 says *"top-2 + merge"*). Duplicate
    ids inside one item collapse the same way.

    An item with no citation ids is kept untouched: there is nothing to
    deduplicate, and discarding a citation-less summary would drop the
    graph's answer for no dedup reason. ``seen`` is updated in place with the
    ids this item contributes.
    """
    unique = tuple(dict.fromkeys(evidence.citations))
    if not unique:
        return evidence
    fresh = tuple(citation for citation in unique if citation not in seen)
    if not fresh:
        return None
    seen.update(fresh)
    return evidence.model_copy(update={"citations": fresh})
