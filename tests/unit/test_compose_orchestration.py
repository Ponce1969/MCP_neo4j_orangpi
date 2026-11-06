"""Behavior tests for the T7 composition use case (S1-S8, S13; decisions L13/L14).

Every test drives the public ``compose_answer`` with a real ``RoutingCaller``
wrapped around a recording fake executor, plus fake ports that record their
calls, and asserts on the returned ``CompositionResult`` only. ``ResolvedRoute``
/ ``RouteDecision`` are built from the real domain models, never from a copy of
their shape. No network, no Neo4j: fakes only.

The three binding rulings these tests protect:

* **S2/L14** — an explicit scope means the router executor records *zero*
  calls;
* **L13** — synthesis without a committed scope returns
  ``insufficient_coverage`` while *every* port (evidence, synthesis and text)
  records zero calls;
* **S1** — each mode uses its own port and never the other one's.
"""

from __future__ import annotations

import pytest

from book_graph_rag.application.orchestration.compose import compose_answer
from book_graph_rag.application.orchestration.ports import GraphEvidencePort, TextPassagePort
from book_graph_rag.domain.cache_policy import CacheDecisionReason, should_cache
from book_graph_rag.domain.intent_tuning import CitationStyle
from book_graph_rag.domain.namespaces import SourceNamespace
from book_graph_rag.domain.orchestration_models import Intent, Mode, OrchestrationRequest
from book_graph_rag.domain.orchestration_outcomes import (
    CoverageState,
    GraphEvidence,
    Passage,
)
from book_graph_rag.domain.routing_models import (
    ResolvedRoute,
    RouteDecision,
    RouteKind,
)
from book_graph_rag.infrastructure.routing_caller import RoutingCaller

_AAP = "knowledge:agentic-architectural-patterns"
_ESSENTIAL = "knowledge:essential-graphrag"
_QUESTION = "How does the ReAct pattern work?"


def _ns(namespace_id: str) -> SourceNamespace:
    corpus, source = namespace_id.split(":", maxsplit=1)
    return SourceNamespace(corpus=corpus, source=source)


def _decision(
    reason: str,
    *,
    route_kind: RouteKind = "abstain",
    selected: SourceNamespace | None = None,
) -> RouteDecision:
    return RouteDecision(route_kind=route_kind, reason=reason, selected=selected)


def _route(
    decision: RouteDecision,
    *,
    validated: SourceNamespace | None = None,
    fanout: tuple[SourceNamespace, ...] = (),
) -> ResolvedRoute:
    return ResolvedRoute(
        decision=decision,
        validated_namespace=validated,
        fanout_namespaces=fanout,
    )


def _committed_route(namespace_id: str = _AAP) -> ResolvedRoute:
    namespace = _ns(namespace_id)
    return _route(
        _decision("clear", route_kind="single", selected=namespace),
        validated=namespace,
    )


def _fanout_route() -> ResolvedRoute:
    """S8 shape: low_margin abstention with a validated top-2 proposal."""
    return _route(_decision("low_margin"), fanout=(_ns(_ESSENTIAL), _ns(_AAP)))


def _evidence(namespace_id: str, summary: str, *citations: str) -> GraphEvidence:
    return GraphEvidence(namespace=_ns(namespace_id), summary=summary, citations=tuple(citations))


class _FakeExecutor:
    """Fake router executor: records every route attempt in call order."""

    def __init__(self, route: ResolvedRoute) -> None:
        self.route = route
        self.calls: list[str] = []

    async def execute(self, question: str) -> ResolvedRoute:
        self.calls.append(question)
        return self.route


def _router(route: ResolvedRoute, *, enabled: bool = True) -> tuple[RoutingCaller, _FakeExecutor]:
    executor = _FakeExecutor(route)
    return RoutingCaller(executor, enabled=enabled), executor


class _FakeEvidence(GraphEvidencePort):
    """Graph stub recording every query in call order, keyed by scope source_id."""

    def __init__(self, results: dict[str, GraphEvidence | None] | None = None) -> None:
        self._results = results if results is not None else {}
        self.calls: list[str] = []

    async def query(self, question: str, scope: SourceNamespace) -> GraphEvidence | None:
        self.calls.append(scope.source_id)
        return self._results.get(scope.source_id)


class _FakeText(TextPassagePort):
    """Text stub recording every ``(question, limit)`` call."""

    def __init__(self, passages: tuple[Passage, ...] = ()) -> None:
        self._passages = passages
        self.calls: list[tuple[str, int]] = []

    async def passages(self, question: str, *, limit: int) -> tuple[Passage, ...]:
        self.calls.append((question, limit))
        return self._passages


# ── S2 / L14: explicit scope wins; the router is never consulted ─────────────


async def test_explicit_scope_skips_the_router_entirely() -> None:
    """S2/L14: explicit scope → router executor records zero calls."""
    router, executor = _router(_committed_route())
    evidence = _FakeEvidence({_AAP: _evidence(_AAP, "aap summary", "chunk-1")})
    synthesis = _FakeEvidence()
    text = _FakeText()

    result = await compose_answer(
        OrchestrationRequest(question=_QUESTION, scope=(_ns(_AAP),)),
        router=router,
        evidence=evidence,
        synthesis=synthesis,
        text=text,
    )

    assert executor.calls == []  # the router was never touched
    assert evidence.calls == [_AAP]  # every later step used the explicit scope
    assert synthesis.calls == []
    assert result.outcome.state == CoverageState.COVERED
    assert result.scopes == (_ns(_AAP),)
    assert result.outcome.reason.startswith("explicit_scope")


async def test_explicit_multi_scope_dispatches_every_scope_without_router() -> None:
    """S2/L14: a multi-book explicit scope fans out over the caller's scopes."""
    router, executor = _router(_committed_route())
    evidence = _FakeEvidence(
        {
            _ESSENTIAL: _evidence(_ESSENTIAL, "essential summary", "chunk-1"),
            _AAP: _evidence(_AAP, "aap summary", "chunk-2"),
        }
    )
    synthesis = _FakeEvidence()
    text = _FakeText()

    result = await compose_answer(
        OrchestrationRequest(question=_QUESTION, scope=(_ns(_ESSENTIAL), _ns(_AAP))),
        router=router,
        evidence=evidence,
        synthesis=synthesis,
        text=text,
    )

    assert executor.calls == []
    # Both caller scopes dispatched, in the caller's order (S2), evidence port only.
    assert evidence.calls == [_ESSENTIAL, _AAP]
    assert synthesis.calls == []
    assert result.outcome.state == CoverageState.FANOUT
    assert result.scopes == (_ns(_ESSENTIAL), _ns(_AAP))
    # The fallback's internal "low_margin" gate token must never leak into the
    # reported diagnostic: the caller chose these scopes, the router never abstained.
    assert result.outcome.reason.startswith("explicit_scope")
    assert "low_margin" not in result.outcome.reason


async def test_explicit_two_scope_reason_is_explicit_scope_and_never_forges_low_margin() -> None:
    """Anti-forgery regression (T7): an explicit two-scope request dispatches
    both scopes and reports ``explicit_scope`` — no ``low_margin`` token is
    ever forged into a route to open a branch and relabelled afterwards.
    """
    router, executor = _router(_committed_route())
    evidence = _FakeEvidence(
        {
            _ESSENTIAL: _evidence(_ESSENTIAL, "essential summary", "chunk-1"),
            _AAP: _evidence(_AAP, "aap summary", "chunk-2"),
        }
    )
    synthesis = _FakeEvidence()
    text = _FakeText()

    result = await compose_answer(
        OrchestrationRequest(question=_QUESTION, scope=(_ns(_ESSENTIAL), _ns(_AAP))),
        router=router,
        evidence=evidence,
        synthesis=synthesis,
        text=text,
    )

    assert executor.calls == []  # the router never abstained — it never ran
    assert evidence.calls == [_ESSENTIAL, _AAP]  # both caller scopes, caller order
    assert result.outcome.state == CoverageState.FANOUT
    # The reported reason is the truth: it starts with the caller's own token
    # and never carries a router abstention that did not happen.
    assert result.outcome.reason.startswith("explicit_scope")
    assert "low_margin" not in result.outcome.reason


async def test_synthesis_with_explicit_scope_uses_the_synthesis_port() -> None:
    """S2 + L13: an explicit scope IS a committed scope, even in synthesis mode."""
    router, executor = _router(_committed_route())
    evidence = _FakeEvidence()
    synthesis = _FakeEvidence({_AAP: _evidence(_AAP, "synth summary", "comm-1")})
    text = _FakeText()

    result = await compose_answer(
        OrchestrationRequest(question=_QUESTION, mode=Mode.SYNTHESIS, scope=(_ns(_AAP),)),
        router=router,
        evidence=evidence,
        synthesis=synthesis,
        text=text,
    )

    assert executor.calls == []
    assert synthesis.calls == [_AAP]
    assert evidence.calls == []
    assert result.outcome.state == CoverageState.COVERED


async def test_synthesis_explicit_multi_scope_spends_synthesis_never_evidence() -> None:
    """The money decision: an explicit multi-book SYNTHESIS is the caller's
    explicit money, not a guess — L13 forbids fan-out on *abstention*, never
    on the caller's own explicit scope choice (S2/L14).

    Pins that mode=synthesis with two explicit scopes spends exactly one
    synthesis (``ask_global``) call per scope, keeps the evidence port at
    zero, and never derives to text.
    """
    router, executor = _router(_committed_route())
    evidence = _FakeEvidence()
    synthesis = _FakeEvidence(
        {
            _ESSENTIAL: _evidence(_ESSENTIAL, "essential summary", "chunk-1"),
            _AAP: _evidence(_AAP, "aap summary", "chunk-2"),
        }
    )
    text = _FakeText()

    result = await compose_answer(
        OrchestrationRequest(
            question=_QUESTION,
            mode=Mode.SYNTHESIS,
            scope=(_ns(_ESSENTIAL), _ns(_AAP)),
        ),
        router=router,
        evidence=evidence,
        synthesis=synthesis,
        text=text,
    )

    assert executor.calls == []  # the router was never consulted (S2/L14)
    assert synthesis.calls == [_ESSENTIAL, _AAP]  # one ask_global per explicit scope
    assert evidence.calls == []  # never the evidence port (S1)
    assert text.calls == []  # no derivation: the caller's choice was dispatched
    assert result.outcome.state == CoverageState.FANOUT
    assert result.outcome.reason.startswith("explicit_scope")


# ── L13: synthesis needs a committed scope — no port is ever touched ─────────


@pytest.mark.parametrize(
    "router_shape",
    ["abstain_with_fanout", "abstain_without_fanout", "router_disabled"],
)
async def test_synthesis_without_committed_scope_never_touches_any_port(
    router_shape: str,
) -> None:
    """L13: synthesis + no validated namespace → insufficient, all ports at zero.

    The money rule: an abstention (low_margin fan-out included) or a disabled
    router must never spend an ``ask_global`` call on a guess — and it must
    not spend the text port either: the caller decides, not the orchestrator.
    """
    if router_shape == "abstain_with_fanout":
        router, _ = _router(_fanout_route())
    elif router_shape == "abstain_without_fanout":
        router, _ = _router(_route(_decision("low_score")))
    else:
        router, _ = _router(_route(_decision("unused")), enabled=False)

    evidence = _FakeEvidence()
    synthesis = _FakeEvidence()
    text = _FakeText()

    result = await compose_answer(
        OrchestrationRequest(question=_QUESTION, mode=Mode.SYNTHESIS),
        router=router,
        evidence=evidence,
        synthesis=synthesis,
        text=text,
    )

    assert result.outcome.state == CoverageState.INSUFFICIENT_COVERAGE
    assert "committed scope" in result.outcome.reason
    assert evidence.calls == []  # no graph query
    assert synthesis.calls == []  # no ask_global
    assert text.calls == []  # not even the text port (L13)
    assert result.outcome.evidence == ()
    assert result.scopes == ()
    assert result.cache.cacheable is False  # S10 fires on the outcome state


# ── S1: port selection by mode — never the other port ────────────────────────


async def test_synthesis_with_committed_route_uses_only_the_synthesis_port() -> None:
    """S1/L13: synthesis + router-committed namespace → synthesis port only."""
    router, _ = _router(_committed_route())
    evidence = _FakeEvidence()
    synthesis = _FakeEvidence({_AAP: _evidence(_AAP, "synth summary", "comm-1")})
    text = _FakeText()

    result = await compose_answer(
        OrchestrationRequest(question=_QUESTION, mode=Mode.SYNTHESIS),
        router=router,
        evidence=evidence,
        synthesis=synthesis,
        text=text,
    )

    assert synthesis.calls == [_AAP]
    assert evidence.calls == []
    assert text.calls == []
    assert result.outcome.state == CoverageState.COVERED
    assert result.mode is Mode.SYNTHESIS


async def test_evidence_with_committed_route_uses_only_the_evidence_port() -> None:
    """S1: evidence + router-committed namespace → evidence port, no synthesis."""
    router, _ = _router(_committed_route())
    evidence = _FakeEvidence({_AAP: _evidence(_AAP, "aap summary", "chunk-1")})
    synthesis = _FakeEvidence()
    text = _FakeText()

    result = await compose_answer(
        OrchestrationRequest(question=_QUESTION),
        router=router,
        evidence=evidence,
        synthesis=synthesis,
        text=text,
    )

    assert evidence.calls == [_AAP]
    assert synthesis.calls == []  # evidence never auto-upgrades to synthesis
    assert text.calls == []
    assert result.outcome.state == CoverageState.COVERED
    assert result.mode is Mode.EVIDENCE


async def test_evidence_fanout_dispatches_both_scopes_and_leaves_synthesis_alone() -> None:
    """S8 + S1: low_margin fan-out → evidence queried per scope, synthesis untouched."""
    router, _ = _router(_fanout_route())
    evidence = _FakeEvidence(
        {
            _ESSENTIAL: _evidence(_ESSENTIAL, "essential summary", "chunk-1"),
            _AAP: _evidence(_AAP, "aap summary", "chunk-2"),
        }
    )
    synthesis = _FakeEvidence()
    text = _FakeText()

    result = await compose_answer(
        OrchestrationRequest(question=_QUESTION),
        router=router,
        evidence=evidence,
        synthesis=synthesis,
        text=text,
    )

    assert evidence.calls == [_ESSENTIAL, _AAP]  # both fan-out scopes, route order
    assert synthesis.calls == []
    assert result.outcome.state == CoverageState.FANOUT
    assert result.scopes == (_ns(_ESSENTIAL), _ns(_AAP))
    assert "low_margin" in result.outcome.reason  # this one IS the router's reason


async def test_evidence_with_router_disabled_derives_to_text() -> None:
    """S7/L14: router off + no explicit scope → insufficient via the text port."""
    router, executor = _router(_route(_decision("unused")), enabled=False)
    evidence = _FakeEvidence()
    synthesis = _FakeEvidence()
    text = _FakeText((Passage(chunk_id="chunk-9", text="text fallback"),))

    result = await compose_answer(
        OrchestrationRequest(question=_QUESTION),
        router=router,
        evidence=evidence,
        synthesis=synthesis,
        text=text,
    )

    assert executor.calls == []  # disabled router short-circuits without executing
    assert evidence.calls == []
    assert synthesis.calls == []
    assert text.calls == [(_QUESTION, 5)]  # derived to text, S7
    assert result.outcome.state == CoverageState.INSUFFICIENT_COVERAGE
    assert "router_disabled" in result.outcome.reason
    assert result.scopes == ()


# ── S10-S12: the cache decision is computed and reported, never written ──────


async def test_cache_decision_is_reported_and_reflects_should_cache() -> None:
    """S10: insufficient coverage is not cacheable; covered evidence is."""
    router, _ = _router(_committed_route())
    evidence = _FakeEvidence({_AAP: _evidence(_AAP, "aap summary", "chunk-1")})
    text = _FakeText()

    covered = await compose_answer(
        OrchestrationRequest(question=_QUESTION),
        router=router,
        evidence=evidence,
        synthesis=_FakeEvidence(),
        text=text,
    )
    assert covered.cache.cacheable is True
    assert covered.cache == should_cache(covered.outcome, covered.mode)

    disabled_router, _ = _router(_route(_decision("unused")), enabled=False)
    insufficient = await compose_answer(
        OrchestrationRequest(question=_QUESTION),
        router=disabled_router,
        evidence=_FakeEvidence(),
        synthesis=_FakeEvidence(),
        text=text,
    )
    assert insufficient.cache.cacheable is False
    assert insufficient.cache.reason is CacheDecisionReason.INSUFFICIENT_COVERAGE
    assert insufficient.cache == should_cache(insufficient.outcome, insufficient.mode)


# ── S3 / S13: the applied tuning rides out on the result ─────────────────────


@pytest.mark.parametrize(
    ("intent", "k", "traverses", "style"),
    [
        (Intent.FOUNDATION, 3, False, CitationStyle.LITERAL_CHUNK),
        (Intent.DESIGN, 5, True, CitationStyle.CONCEPT_REFERENCE),
        (Intent.IMPLEMENTATION, 4, False, CitationStyle.CODE_WITH_CHUNK),
    ],
)
async def test_intent_tuning_matches_s13_table_for_each_intent(
    intent: Intent,
    k: int,
    traverses: bool,
    style: CitationStyle,
) -> None:
    """S13: the result carries the maintainer's verbatim k / traversal / style."""
    router, _ = _router(_committed_route())

    result = await compose_answer(
        OrchestrationRequest(question=_QUESTION, intent=intent),
        router=router,
        evidence=_FakeEvidence({_AAP: _evidence(_AAP, "aap summary", "chunk-1")}),
        synthesis=_FakeEvidence(),
        text=_FakeText(),
    )

    assert result.intent_tuning is not None
    assert result.intent_tuning.k == k
    assert result.intent_tuning.traverses_relationships is traverses
    assert result.intent_tuning.citation_style is style


async def test_intent_tuning_is_none_when_intent_is_none() -> None:
    """S13/L7: no intent → no tuning row invented; the result reports None."""
    router, _ = _router(_committed_route())

    result = await compose_answer(
        OrchestrationRequest(question=_QUESTION),
        router=router,
        evidence=_FakeEvidence({_AAP: _evidence(_AAP, "aap summary", "chunk-1")}),
        synthesis=_FakeEvidence(),
        text=_FakeText(),
    )

    assert result.intent_tuning is None
