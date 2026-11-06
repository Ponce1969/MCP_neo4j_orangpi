"""Evidence-based fallback for a routed question: one outcome, three states (S6-S8).

Spec home: ``odd/tasks/tool-intent-router.md``, Regla 2. The resolver never
retries blindly (S7) and never fabricates graph evidence (S6):

* validated scope with evidence        → ``covered``: graph evidence — plus
  passages when the caller declared ``requires_passages`` (S5), never
  inferred from the route or the intent;
* ``low_margin`` + validated fan-out   → ``fanout``: one graph query per top-2
  scope in route order, merged by citation id — per L2 (5/5 measured
  ``low_margin`` abstentions) this is the normal path, not an error (S8),
  with passages appended when the caller requires them (S5);
* abstain without usable fan-out, or a graph miss → ``insufficient_coverage``
  derived to text passages (S7 / S6);
* caller-stated ``dispatch_scopes`` (T7 explicit scope) → exactly those scopes
  are queried, with the same citation-id merge: ``covered`` when one produced
  evidence, ``fanout`` when several did, ``insufficient_coverage`` when none
  did.

``FallbackOutcome.reason`` is a **diagnostic string** — the router's own
reason plus what the resolver did — reported for humans and logs. It is never
an input to a branch: the only branch inputs are the route's committed
fields (``validated_namespace`` / ``fanout_namespaces``) and the caller's
explicit ``dispatch_scopes``.

Passage budget (S5/S13): when the caller declares an intent, ``tuning_for``
returns a row whose ``k`` *is* the passage count; otherwise ``passage_limit``
stands (S13 defines no row for no intent, so no k is invented for it). The
text port is called at most once per resolution; the graph, at most once per
scope. Port exceptions propagate untouched — a failure is never rewritten
into an outcome.
"""

from __future__ import annotations

from book_graph_rag.application.orchestration.ports import GraphEvidencePort, TextPassagePort
from book_graph_rag.domain.intent_tuning import tuning_for
from book_graph_rag.domain.namespaces import SourceNamespace
from book_graph_rag.domain.orchestration_models import Intent
from book_graph_rag.domain.orchestration_outcomes import (
    CoverageState,
    FallbackOutcome,
    GraphEvidence,
    Passage,
)
from book_graph_rag.domain.routing_models import ResolvedRoute


async def resolve_with_fallback(
    question: str,
    route: ResolvedRoute,
    *,
    graph: GraphEvidencePort,
    text: TextPassagePort,
    passage_limit: int = 5,
    requires_passages: bool = False,
    intent: Intent | None = None,
    dispatch_scopes: tuple[SourceNamespace, ...] | None = None,
) -> FallbackOutcome:
    """Resolve ``question`` for ``route`` into one explicit coverage state.

    When the caller states ``dispatch_scopes`` (T7's explicit scope), those
    and only those scopes are dispatched, and the route's reason string is
    never consulted — ``reason`` is a diagnostic reported on the outcome,
    never an input to a branch. Without it, branch order mirrors how
    :class:`ResolvedRoute` is produced by ``RouteQuestionUseCase``: a
    validated single scope first, then S8's ``low_margin`` fan-out, then S7's
    text derivation. The first two states are mutually exclusive in a
    well-formed route; a route with neither a validated scope nor a usable
    fan-out takes the S7 path without ever querying the graph.

    Args:
        question: The question verbatim, as the caller sent it.
        route: The resolved route (decision plus validated/fan-out scopes).
        graph: Scoped graph evidence port; called at most once per scope.
        text: Text passage port; called at most once per resolution — on any
            graph miss (S6/S7), and on *every* outcome, covered and fan-out
            included, when ``requires_passages`` is true (S5:
            *"independientemente de lo que diga el grafo"*).
        passage_limit: Upper bound handed to ``text.passages`` when the
            caller declares no intent (or no intent tuning exists).
        requires_passages: The caller's explicit requirement (S5), never
            inferred from ``intent`` or ``mode``: when true, passages are
            fetched even for ``covered`` and ``fanout`` outcomes, which keep
            their state and carry the passages alongside the graph evidence.
        intent: Optional caller intent (S3). When S13 tuning exists for it,
            its ``k`` *is* the passage count and overrides
            ``passage_limit``; ``None`` keeps ``passage_limit`` (S13 defines
            no row for "no intent").
        dispatch_scopes: The exact scopes to query when the caller stated
            them explicitly (T7 / S2). When provided, these — and only these
            — are dispatched in the given order, each queried at most once,
            merged by citation id exactly like the S8 fan-out: ``covered``
            when exactly one scope produced evidence, ``fanout`` when more
            than one did, ``insufficient_coverage`` (derived to text) when
            none did. When ``None``, the route-driven branches below apply
            unchanged, byte-identical to the pre-``dispatch_scopes``
            behaviour, including the ``low_margin`` gate.

    Returns:
        A frozen ``FallbackOutcome`` in exactly one of ``covered`` (S6 happy
        path), ``fanout`` (S8) or ``insufficient_coverage`` (S6/S7).

    Raises:
        Whatever a port raises — exceptions propagate, never converted.
    """
    reason = route.decision.reason
    # S13: the intent's k IS the passage count. No tuning row (intent None)
    # → the caller's passage_limit stands; no k is invented for None (L7).
    tuning = tuning_for(intent)
    limit = tuning.k if tuning is not None else passage_limit
    # INTENTIONAL, not a bug (maintainer, 2026-11-05): the tuned k also governs
    # the S7 derivation to text, so a foundation request that misses the graph
    # asks for 3 passages, not 5. A caller who asked for a *fundamento* wants a
    # concise answer; if the graph misses, handing them five passages instead of
    # three is noise, not generosity. implementation behaves the same (4).
    # Do not "correct" this to always use passage_limit.
    passages: tuple[Passage, ...] = ()
    s5_note = ""
    if requires_passages:
        s5_note = "; passages fetched because the caller requires them (S5)"

    # T7 explicit dispatch: the caller stated the exact scopes, so they are
    # dispatched directly — route.decision.reason is NOT read here (it is a
    # diagnostic, never a branch input; the low_margin gate below only runs
    # when this parameter is absent).
    if dispatch_scopes is not None:
        merged, scopes_with_evidence = await _query_scopes(question, dispatch_scopes, graph)
        if not scopes_with_evidence:
            # Every dispatched scope missed: S6 applies per scope — derive to
            # text, once, with nothing fabricated (evidence stays empty).
            missing = ", ".join(scope.source_id for scope in dispatch_scopes)
            passages = await text.passages(question, limit=limit)
            return FallbackOutcome(
                state=CoverageState.INSUFFICIENT_COVERAGE,
                passages=passages,
                reason=f"{reason}: no graph evidence in dispatch scopes "
                f"{missing}; derived to text passages",
            )
        # S5 rides along exactly as in the route-driven branches: passages
        # are fetched for every state when the caller requires them.
        if requires_passages:
            passages = await text.passages(question, limit=limit)
        if len(scopes_with_evidence) == 1:
            # Exactly one scope produced evidence: a single committed scope,
            # so the outcome is covered — no fourth state invented (S6).
            only = scopes_with_evidence[0]
            return FallbackOutcome(
                state=CoverageState.COVERED,
                evidence=tuple(merged),
                passages=passages,
                scopes=tuple(scopes_with_evidence),
                reason=f"{reason}: graph evidence for scope {only.source_id}{s5_note}",
            )
        produced = ", ".join(scope.source_id for scope in scopes_with_evidence)
        return FallbackOutcome(
            state=CoverageState.FANOUT,
            evidence=tuple(merged),
            passages=passages,
            scopes=tuple(scopes_with_evidence),
            reason=f"{reason}: dispatch merged evidence from {produced}{s5_note}",
        )

    # S6 covered / missed branches: only a catalog-validated namespace ever
    # reaches the graph (S4: the router proposes, the catalog disposes).
    if route.validated_namespace is not None:
        scope = route.validated_namespace
        evidence = await graph.query(question, scope)
        if evidence is None:
            # S6: no entity found. Never synthesize a GraphEvidence from the
            # question — report the missing scope in the reason and derive to
            # text. The scope produced no evidence, so it is not in `scopes`.
            passages = await text.passages(question, limit=limit)
            return FallbackOutcome(
                state=CoverageState.INSUFFICIENT_COVERAGE,
                passages=passages,
                reason=f"{reason}: no graph evidence for scope "
                f"{scope.source_id}; derived to text passages",
            )
        # Covered: graph evidence stands on its own unless the caller declared
        # S5's requirement. The state stays `covered` — passages ride along
        # with the evidence; no fourth state is invented. With the flag off,
        # the text port is deliberately NOT called here, exactly as before:
        # whether passages are fetched is the caller's word, never inferred.
        if requires_passages:
            passages = await text.passages(question, limit=limit)
        return FallbackOutcome(
            state=CoverageState.COVERED,
            evidence=(evidence,),
            passages=passages,
            scopes=(scope,),
            reason=f"{reason}: graph evidence for scope {scope.source_id}{s5_note}",
        )

    # S8: a low_margin abstention with a validated top-2 fan-out is not an
    # error. Query every fan-out scope exactly once (route order), merge by
    # citation id, and do NOT fall back to text just because the router
    # abstained — text only happens below, when no scope produced evidence.
    if reason == "low_margin" and route.fanout_namespaces:
        merged, scopes_with_evidence = await _query_scopes(question, route.fanout_namespaces, graph)
        if scopes_with_evidence:
            produced = ", ".join(scope.source_id for scope in scopes_with_evidence)
            # S5: "independientemente de lo que diga el grafo" applies to the
            # fan-out outcome too — state stays `fanout`, passages ride along.
            if requires_passages:
                passages = await text.passages(question, limit=limit)
            return FallbackOutcome(
                state=CoverageState.FANOUT,
                evidence=tuple(merged),
                passages=passages,
                scopes=tuple(scopes_with_evidence),
                reason=f"{reason}: fan-out merged evidence from {produced}{s5_note}",
            )
        # Every fan-out scope missed: S6 applies per scope — derive to text.
        missing = ", ".join(scope.source_id for scope in route.fanout_namespaces)
        passages = await text.passages(question, limit=limit)
        return FallbackOutcome(
            state=CoverageState.INSUFFICIENT_COVERAGE,
            passages=passages,
            reason=f"{reason}: no graph evidence in fan-out scopes "
            f"{missing}; derived to text passages",
        )

    # S7: an abstention (or any route with no queryable validated scope)
    # derives to the text port on the router's own reason — the graph is not
    # queried at all, and this is an evidence-based decision, not a retry.
    passages = await text.passages(question, limit=limit)
    return FallbackOutcome(
        state=CoverageState.INSUFFICIENT_COVERAGE,
        passages=passages,
        reason=f"{reason}: no queryable validated scope; derived to text passages",
    )


async def _query_scopes(
    question: str,
    scopes: tuple[SourceNamespace, ...],
    graph: GraphEvidencePort,
) -> tuple[list[GraphEvidence], list[SourceNamespace]]:
    """Query each scope exactly once, in order; merge by citation id.

    Shared by the T7 explicit dispatch and S8's ``low_margin`` fan-out so the
    two paths cannot drift apart: scope order is preserved, a scope whose
    query misses is skipped (it produced no evidence), and citation ids
    already contributed by an earlier scope are dropped
    (:func:`_merge_citations`).

    Returns:
        ``(merged_evidence, scopes_with_evidence)`` — the deduplicated items
        and the scopes that produced evidence, both in dispatch order.
    """
    merged: list[GraphEvidence] = []
    scopes_with_evidence: list[SourceNamespace] = []
    seen_citations: set[str] = set()
    for scope in scopes:
        evidence = await graph.query(question, scope)
        if evidence is None:
            continue
        scopes_with_evidence.append(scope)
        deduped = _merge_citations(evidence, seen_citations)
        if deduped is not None:
            merged.append(deduped)
    return merged, scopes_with_evidence


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
