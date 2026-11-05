"""Behavior tests for the evidence-based fallback resolver (specs S5-S8).

Every test drives the public ``resolve_with_fallback`` with fake ports that
record their calls, and asserts on the returned ``FallbackOutcome`` only —
never on internal helpers. ``ResolvedRoute`` / ``RouteDecision`` are built
from the real domain models, not from a copy of their shape.
"""

from __future__ import annotations

import pytest

from book_graph_rag.application.orchestration.fallback import resolve_with_fallback
from book_graph_rag.application.orchestration.ports import GraphEvidencePort, TextPassagePort
from book_graph_rag.domain.namespaces import SourceNamespace
from book_graph_rag.domain.orchestration_models import Intent
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


def _evidence(namespace_id: str, summary: str, *citations: str) -> GraphEvidence:
    return GraphEvidence(namespace=_ns(namespace_id), summary=summary, citations=tuple(citations))


def _passage(chunk_id: str, text: str) -> Passage:
    return Passage(chunk_id=chunk_id, text=text)


class _FakeGraph(GraphEvidencePort):
    """Graph stub recording every query in call order, keyed by scope source_id."""

    def __init__(self, results: dict[str, GraphEvidence | None]) -> None:
        self._results = results
        self.calls: list[str] = []

    async def query(self, question: str, scope: SourceNamespace) -> GraphEvidence | None:
        self.calls.append(scope.source_id)
        return self._results.get(scope.source_id)


class _RaisingGraph(GraphEvidencePort):
    """Graph stub whose query always fails."""

    async def query(self, question: str, scope: SourceNamespace) -> GraphEvidence | None:
        raise RuntimeError("graph unavailable")


class _RaisingText(TextPassagePort):
    """Text stub whose passages call always fails."""

    async def passages(self, question: str, *, limit: int) -> tuple[Passage, ...]:
        raise RuntimeError("text store unavailable")


class _FakeText(TextPassagePort):
    """Text stub recording every ``(question, limit)`` call."""

    def __init__(self, passages: tuple[Passage, ...] = ()) -> None:
        self._passages = passages
        self.calls: list[tuple[str, int]] = []

    async def passages(self, question: str, *, limit: int) -> tuple[Passage, ...]:
        self.calls.append((question, limit))
        return self._passages


async def test_covered_queries_graph_only_and_preserves_evidence() -> None:
    """S6 happy path: validated scope with evidence → COVERED, text never called."""
    evidence = _evidence(_AAP, "ReAct summary", "chunk-1")
    graph = _FakeGraph({_AAP: evidence})
    text = _FakeText()
    route = _route(
        _decision("clear", route_kind="single", selected=_ns(_AAP)),
        validated=_ns(_AAP),
    )

    outcome = await resolve_with_fallback(_QUESTION, route, graph=graph, text=text)

    assert outcome.state == CoverageState.COVERED
    assert outcome.evidence == (evidence,)
    assert outcome.scopes == (_ns(_AAP),)
    assert graph.calls == [_AAP]
    assert text.calls == []  # passages on demand is S5/T3, not this unit


async def test_low_margin_fanout_queries_route_order_and_merges_by_citation() -> None:
    """S8: one query per fan-out scope in route order, duplicates collapsed, no text."""
    # Route order is deliberately reverse-alphabetic, so the assertion proves
    # route order rather than lexicographic order.
    essential = _evidence(_ESSENTIAL, "essential summary", "chunk-1", "chunk-2")
    aap = _evidence(_AAP, "aap summary", "chunk-2", "chunk-3")
    graph = _FakeGraph({_ESSENTIAL: essential, _AAP: aap})
    text = _FakeText()
    route = _route(_decision("low_margin"), fanout=(_ns(_ESSENTIAL), _ns(_AAP)))

    outcome = await resolve_with_fallback(_QUESTION, route, graph=graph, text=text)

    assert graph.calls == [_ESSENTIAL, _AAP]
    assert outcome.state == CoverageState.FANOUT
    assert outcome.scopes == (_ns(_ESSENTIAL), _ns(_AAP))
    assert text.calls == []
    citations = tuple(c for item in outcome.evidence for c in item.citations)
    assert citations == ("chunk-1", "chunk-2", "chunk-3")  # first-seen order, deduped
    assert outcome.evidence[1].citations == ("chunk-3",)  # duplicate chunk-2 collapsed


async def test_low_score_abstain_derives_to_text_without_calling_graph() -> None:
    """S7: abstention on low_score → text only, INSUFFICIENT_COVERAGE, router reason."""
    passage = _passage("chunk-9", "irrelevant without graph coverage")
    graph = _FakeGraph({})
    text = _FakeText((passage,))
    route = _route(_decision("low_score"))

    outcome = await resolve_with_fallback(
        "What is the best pasta recipe?", route, graph=graph, text=text
    )

    assert graph.calls == []
    assert text.calls == [("What is the best pasta recipe?", 5)]
    assert outcome.state == CoverageState.INSUFFICIENT_COVERAGE
    assert outcome.passages == (passage,)
    assert "low_score" in outcome.reason


async def test_missing_entity_yields_insufficient_coverage_without_fabrication() -> None:
    """S6 anti-fabrication: graph returns None → no evidence, reason names scope + router."""
    graph = _FakeGraph({_AAP: None})
    passage = _passage("chunk-4", "text derivation")
    text = _FakeText((passage,))
    route = _route(
        _decision("clear", route_kind="single", selected=_ns(_AAP)),
        validated=_ns(_AAP),
    )

    outcome = await resolve_with_fallback(
        "Who is the PatternLanguage architect?", route, graph=graph, text=text
    )

    assert outcome.state == CoverageState.INSUFFICIENT_COVERAGE
    assert outcome.evidence == ()  # nothing invented from the question (S6)
    assert outcome.scopes == ()  # the scope produced no evidence
    assert graph.calls == [_AAP]  # exactly one query, no retries
    assert text.calls == [("Who is the PatternLanguage architect?", 5)]
    assert _AAP in outcome.reason  # missing scope named
    assert "clear" in outcome.reason  # router reason named


async def test_fanout_with_every_scope_missing_derives_to_text_once() -> None:
    """Fan-out where all scopes return None → INSUFFICIENT_COVERAGE, evidence empty."""
    graph = _FakeGraph({_ESSENTIAL: None, _AAP: None})
    passage = _passage("chunk-5", "only text remains")
    text = _FakeText((passage,))
    route = _route(_decision("low_margin"), fanout=(_ns(_ESSENTIAL), _ns(_AAP)))

    outcome = await resolve_with_fallback(_QUESTION, route, graph=graph, text=text)

    assert graph.calls == [_ESSENTIAL, _AAP]
    assert text.calls == [(_QUESTION, 5)]
    assert outcome.state == CoverageState.INSUFFICIENT_COVERAGE
    assert outcome.evidence == ()
    assert outcome.passages == (passage,)
    assert "low_margin" in outcome.reason


async def test_port_exception_propagates_instead_of_becoming_an_outcome() -> None:
    """No silent conversion: a raising graph or text port surfaces as an exception."""
    route_covered = _route(
        _decision("clear", route_kind="single", selected=_ns(_AAP)),
        validated=_ns(_AAP),
    )

    with pytest.raises(RuntimeError, match="graph unavailable"):
        await resolve_with_fallback(
            _QUESTION,
            route_covered,
            graph=_RaisingGraph(),
            text=_FakeText(),
        )

    route_abstain = _route(_decision("low_score"))
    with pytest.raises(RuntimeError, match="text store unavailable"):
        await resolve_with_fallback(
            _QUESTION,
            route_abstain,
            graph=_FakeGraph({}),
            text=_RaisingText(),
        )


async def test_scopes_lists_only_evidence_producing_scopes_in_route_order() -> None:
    """Only scopes that produced evidence appear in `scopes`, in route order."""
    graph = _FakeGraph({_ESSENTIAL: None, _AAP: _evidence(_AAP, "aap summary", "chunk-7")})
    text = _FakeText()
    route = _route(_decision("low_margin"), fanout=(_ns(_ESSENTIAL), _ns(_AAP)))

    outcome = await resolve_with_fallback(_QUESTION, route, graph=graph, text=text)

    assert graph.calls == [_ESSENTIAL, _AAP]  # both queried once, route order
    assert outcome.state == CoverageState.FANOUT
    assert outcome.scopes == (_ns(_AAP),)  # the missing scope is excluded


# ── S5 (T3): requires_passages — the caller's explicit passage requirement ────


def _covered_route() -> ResolvedRoute:
    return _route(
        _decision("clear", route_kind="single", selected=_ns(_AAP)),
        validated=_ns(_AAP),
    )


async def test_requires_passages_fetches_text_on_covered_route_without_changing_state() -> None:
    """S5: covered stays covered, yet text is fetched because the caller demands it."""
    evidence = _evidence(_AAP, "ReAct summary", "chunk-1")
    passage = _passage("chunk-2", "literal coverage")
    graph = _FakeGraph({_AAP: evidence})
    text = _FakeText((passage,))

    outcome = await resolve_with_fallback(
        _QUESTION, _covered_route(), graph=graph, text=text, requires_passages=True
    )

    assert outcome.state == CoverageState.COVERED  # no fourth state invented
    assert outcome.evidence == (evidence,)  # graph evidence still present
    assert outcome.passages == (passage,)  # passages ride along with it
    assert outcome.scopes == (_ns(_AAP),)
    assert graph.calls == [_AAP]  # exactly one graph query, no retries
    assert len(text.calls) == 1  # at most once per resolution


async def test_requires_passages_with_design_intent_uses_s13_k_as_passage_count() -> None:
    """S13: design's k (5) is the passage count, not the fallback default."""
    graph = _FakeGraph({_AAP: _evidence(_AAP, "ReAct summary", "chunk-1")})
    text = _FakeText((_passage("chunk-2", "literal coverage"),))

    outcome = await resolve_with_fallback(
        _QUESTION,
        _covered_route(),
        graph=graph,
        text=text,
        requires_passages=True,
        intent=Intent.DESIGN,
    )

    assert text.calls == [(_QUESTION, 5)]  # S13 k, not the passage_limit default
    assert outcome.state == CoverageState.COVERED
    assert len(outcome.passages) == 1


@pytest.mark.parametrize(
    ("intent", "expected_passages"),
    [
        (Intent.FOUNDATION, 3),
        (Intent.IMPLEMENTATION, 4),
    ],
)
async def test_requires_passages_k_follows_s13_table(
    intent: Intent, expected_passages: int
) -> None:
    """S13: foundation requests 3 and implementation 4 — the table's k, never 3/4/5 hardcoded."""
    graph = _FakeGraph({_AAP: _evidence(_AAP, "ReAct summary", "chunk-1")})
    text = _FakeText((_passage("chunk-2", "literal coverage"),))

    outcome = await resolve_with_fallback(
        _QUESTION,
        _covered_route(),
        graph=graph,
        text=text,
        requires_passages=True,
        intent=intent,
    )

    assert text.calls == [(_QUESTION, expected_passages)]
    assert outcome.state == CoverageState.COVERED


async def test_requires_passages_without_intent_keeps_fallback_default_limit() -> None:
    """intent=None has no S13 row (L7), so passage_limit's default stands."""
    graph = _FakeGraph({_AAP: _evidence(_AAP, "ReAct summary", "chunk-1")})
    text = _FakeText((_passage("chunk-2", "literal coverage"),))

    outcome = await resolve_with_fallback(
        _QUESTION,
        _covered_route(),
        graph=graph,
        text=text,
        requires_passages=True,
        intent=None,
    )

    assert text.calls == [(_QUESTION, 5)]  # the fallback's own default limit
    assert outcome.state == CoverageState.COVERED


async def test_requires_passages_false_keeps_covered_and_fanout_off_the_text_port() -> None:
    """S5 regression guard: without the flag, covered and fanout never touch text."""
    evidence = _evidence(_AAP, "ReAct summary", "chunk-1")
    text = _FakeText((_passage("chunk-2", "must not be fetched"),))

    covered_graph = _FakeGraph({_AAP: evidence})
    covered = await resolve_with_fallback(
        _QUESTION, _covered_route(), graph=covered_graph, text=text, requires_passages=False
    )
    assert covered.state == CoverageState.COVERED
    assert covered.passages == ()
    assert text.calls == []

    fanout_graph = _FakeGraph(
        {_ESSENTIAL: _evidence(_ESSENTIAL, "essential", "chunk-3"), _AAP: evidence}
    )
    fanout_route = _route(_decision("low_margin"), fanout=(_ns(_ESSENTIAL), _ns(_AAP)))
    fanout = await resolve_with_fallback(
        _QUESTION, fanout_route, graph=fanout_graph, text=text, requires_passages=False
    )
    assert fanout.state == CoverageState.FANOUT
    assert fanout.passages == ()
    assert text.calls == []  # still no text call after both resolutions


async def test_requires_passages_fetches_text_on_fanout_route_too() -> None:
    """S5's "in every outcome": fanout keeps its state and carries passages as well."""
    essential = _evidence(_ESSENTIAL, "essential summary", "chunk-1")
    aap = _evidence(_AAP, "aap summary", "chunk-2")
    passage = _passage("chunk-3", "literal coverage")
    graph = _FakeGraph({_ESSENTIAL: essential, _AAP: aap})
    text = _FakeText((passage,))
    route = _route(_decision("low_margin"), fanout=(_ns(_ESSENTIAL), _ns(_AAP)))

    outcome = await resolve_with_fallback(
        _QUESTION,
        route,
        graph=graph,
        text=text,
        requires_passages=True,
        intent=Intent.DESIGN,
    )

    assert outcome.state == CoverageState.FANOUT  # state unchanged
    assert outcome.evidence == (essential, aap)  # merged graph evidence still present
    assert outcome.passages == (passage,)
    assert graph.calls == [_ESSENTIAL, _AAP]
    assert text.calls == [(_QUESTION, 5)]  # once, with S13's k
