"""Composition use case: request → route → port per mode → one result (T7).

Spec home: ``odd/tasks/tool-intent-router.md`` (S1-S8, S13; decisions L13/L14).
This use case only composes what already exists:

``OrchestrationRequest`` → namespace route (``RoutingCaller``; skipped entirely
when the caller set an explicit scope) → the graph port the mode selects (S1)
→ :func:`resolve_with_fallback` (S6-S8) → :func:`should_cache` (S10-S12) →
one :class:`CompositionResult`.

Decisions taken here all come from the spec's Log and are not re-litigated:

* **S2 / L14 — explicit scope wins.** ``request.scope is not None`` means the
  router is never called; the caller's namespaces are passed to the fallback
  as an explicit ``dispatch_scopes`` — a real, caller-stated dispatch, never a
  forged reason token. Explicit namespaces are taken as committed: catalog
  validation of caller scope is the MCP's scope gate upstream, and the router's
  catalog check is only needed for *proposed* namespaces (S4 posture).
* **L13 — synthesis needs a committed scope.** ``mode=synthesis`` with no
  catalog-validated namespace (router abstained, proposed only a top-2
  fan-out, or returned ``None`` because the flag is off) returns
  ``INSUFFICIENT_COVERAGE`` *before any port is touched* — no evidence call,
  no ``ask_global``, not even the text port. The reason says so; the caller
  decides what to do next.
* **S1 — port selection by mode.** Evidence mode always uses ``evidence``,
  synthesis mode always uses ``synthesis``; evidence never silently upgrades
  to synthesis, and synthesis never degrades to evidence on its own (L13).
* The cache decision is computed and reported, never written (T5 boundary).

The router import is type-only (``TYPE_CHECKING``): application code never
pulls infrastructure in at runtime; the concrete ``RoutingCaller`` is handed
in by the composition root that builds this call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from book_graph_rag.application.orchestration.fallback import resolve_with_fallback
from book_graph_rag.application.orchestration.ports import GraphEvidencePort, TextPassagePort
from book_graph_rag.domain.cache_policy import CacheDecision, should_cache
from book_graph_rag.domain.intent_tuning import IntentTuning, tuning_for
from book_graph_rag.domain.namespaces import SourceNamespace
from book_graph_rag.domain.orchestration_models import Mode, OrchestrationRequest
from book_graph_rag.domain.orchestration_outcomes import CoverageState, FallbackOutcome
from book_graph_rag.domain.routing_models import ResolvedRoute, RouteDecision

if TYPE_CHECKING:
    from book_graph_rag.infrastructure.routing_caller import RoutingCaller

#: Decision reason standing in for "the router was never consulted" (S2/L14).
_EXPLICIT_SCOPE_REASON = "explicit_scope"

#: Decision reason for "router flag off and no explicit scope" (L14: the
#: caller's ``scope=None`` means all books, never a licence for compose to
#: invent a namespace).
_ROUTER_DISABLED_REASON = "router_disabled"


class CompositionResult(BaseModel):
    """One composed answer: the coverage outcome plus the decisions around it.

    ``scopes`` lists the namespaces the composition *dispatched to* — the
    explicit caller scope, the router's committed namespace, or its top-2
    fan-out. That is deliberately wider than ``outcome.scopes`` (only the
    scopes whose graph query produced evidence): ``scopes`` answers "where did
    we look", ``outcome.scopes`` answers "where did we find something".

    ``intent_tuning`` is the S13 row actually applied, so the caller sees the
    ``k`` the resolution used; ``None`` when the caller declared no intent
    (S13 defines no row for it — L7).

    ``cache`` is the write decision (S10-S12), computed and reported here.
    This repo never writes the cache (T5 boundary).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: FallbackOutcome
    cache: CacheDecision
    scopes: tuple[SourceNamespace, ...]
    intent_tuning: IntentTuning | None
    mode: Mode


async def compose_answer(
    request: OrchestrationRequest,
    *,
    router: RoutingCaller,
    evidence: GraphEvidencePort,
    synthesis: GraphEvidencePort,
    text: TextPassagePort,
) -> CompositionResult:
    """Resolve ``request`` into one mode-aware, routed composition.

    Behaviour, in order:

    1. **S2 / L14:** an explicit ``request.scope`` wins outright — the router
       is never called, those namespaces become the fallback's explicit
       ``dispatch_scopes``, and ``reason`` reports ``explicit_scope`` as the
       truth.
    2. Otherwise the router is consulted once. Three shapes are handled:
       ``None`` (flag off), a route with ``validated_namespace``, and an
       abstention with a top-2 ``fanout_namespaces`` proposal (S8).
    3. **L13:** synthesis without a committed namespace — abstention,
       fan-out proposal, or router disabled — returns
       ``INSUFFICIENT_COVERAGE`` with a reason naming the committed-scope
       requirement, **without touching any port** (no graph, no ``ask_global``,
       no text). Never two ``ask_global`` calls spent on a guess.
    4. Otherwise the fallback resolves the route with the mode's graph port
       (S1): evidence mode never upgrades to synthesis, and an evidence
       resolution with no queryable scope derives to text (S7).
    5. ``requires_passages`` and ``intent`` pass through to the fallback so
       S5/S13 keep working; the cache decision is computed from the outcome
       and reported, never written.

    Args:
        request: The parsed caller request (question verbatim, mode, intent,
            explicit scope or ``None``).
        router: The namespace router adapter; never called when
            ``request.scope`` is set.
        evidence: ``GraphEvidencePort`` used in evidence mode (S1).
        synthesis: ``GraphEvidencePort`` used in synthesis mode (S1).
        text: Text passage port used by the fallback on misses (S6/S7) and
            whenever the caller declared ``requires_passages`` (S5).

    Returns:
        The frozen :class:`CompositionResult`.

    Raises:
        Whatever a port raises — exceptions propagate, never converted into
        an outcome.
    """
    mode = request.mode
    tuning = tuning_for(request.intent)
    dispatch: tuple[SourceNamespace, ...] | None = None

    # S2/L14: explicit scope wins outright; the router is never consulted.
    if request.scope is not None:
        scopes = request.scope
        route = _route_from_explicit_scope(request.scope)
        # The caller stated the scopes, so the fallback dispatches them
        # explicitly (dispatch_scopes) — no reason-string token is forged
        # into the route to steer a branch.
        dispatch = request.scope
    else:
        scopes = ()
        resolved = await router.route_for_caller(request.question)
        # L13: no synthesis without a committed, catalog-validated namespace.
        # This fires for an abstention, a fan-out proposal (low_margin
        # included) and the disabled-router ``None`` — before any port runs.
        if mode is Mode.SYNTHESIS and (resolved is None or resolved.validated_namespace is None):
            outcome = FallbackOutcome(
                state=CoverageState.INSUFFICIENT_COVERAGE,
                reason=_synthesis_needs_scope_reason(resolved),
            )
            return CompositionResult(
                outcome=outcome,
                cache=should_cache(outcome, mode),
                scopes=(),
                intent_tuning=tuning,
                mode=mode,
            )
        if resolved is None:
            # Evidence mode, router off, no explicit scope: there is no
            # namespace to query (L14), so the route below derives to text.
            route = ResolvedRoute(
                decision=RouteDecision(
                    route_kind="abstain",
                    reason=_ROUTER_DISABLED_REASON,
                ),
            )
        else:
            route = resolved
            if route.validated_namespace is not None:
                scopes = (route.validated_namespace,)
            elif route.fanout_namespaces:
                # Evidence-only: synthesis with only a fan-out proposal hit
                # L13 above and returned. These are the dispatched scopes
                # even if some of them produce no evidence.
                scopes = route.fanout_namespaces

    graph = evidence if mode is Mode.EVIDENCE else synthesis
    outcome = await resolve_with_fallback(
        request.question,
        route,
        graph=graph,
        text=text,
        requires_passages=request.requires_passages,
        intent=request.intent,
        dispatch_scopes=dispatch,
    )

    return CompositionResult(
        outcome=outcome,
        cache=should_cache(outcome, mode),
        scopes=scopes,
        intent_tuning=tuning,
        mode=mode,
    )


def _route_from_explicit_scope(scopes: tuple[SourceNamespace, ...]) -> ResolvedRoute:
    """Turn the caller's explicit scope into an honest route for the fallback.

    A single namespace becomes the committed ``validated_namespace``;
    several namespaces become a ``multi`` route carrying the caller's scopes
    as the fan-out proposal. Either way ``decision.reason`` is
    ``explicit_scope`` — the truth: the caller chose these scopes (S2), the
    router never abstained — and no token is ever forged into the reason to
    steer a branch. The actual multi-scope dispatch happens through
    :func:`resolve_with_fallback`'s explicit ``dispatch_scopes``, so the
    route here is diagnostics plus the committed single scope, nothing more.
    """
    if len(scopes) == 1:
        only = scopes[0]
        return ResolvedRoute(
            decision=RouteDecision(
                selected=only,
                route_kind="single",
                reason=_EXPLICIT_SCOPE_REASON,
            ),
            validated_namespace=only,
        )
    return ResolvedRoute(
        decision=RouteDecision(route_kind="multi", reason=_EXPLICIT_SCOPE_REASON),
        fanout_namespaces=scopes,
    )


def _synthesis_needs_scope_reason(route: ResolvedRoute | None) -> str:
    """Explain an L13 stop in the ``reason`` field, naming the requirement."""
    if route is None:
        detail = "router disabled and no explicit scope was given"
    else:
        detail = f"router reason {route.decision.reason!r} produced no validated namespace"
    return f"synthesis needs a committed scope: {detail}; no graph or text port was called (L13)"
