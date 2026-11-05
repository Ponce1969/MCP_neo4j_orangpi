"""Behavior tests for the evidence-based fallback resolver (specs S6, S7, S8).

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
