"""Behavior tests for the cache write policy (T5, specs S10-S11).

Every test drives the public ``should_cache`` predicate only and asserts both
halves of the returned ``CacheDecision`` — the boolean *and* the reason —
because S10-S11 are exactly the kind of rule a later debugging session
questions.

S12 is intentionally absent: key rotation with ``graph_version`` is
``route_cache_key``'s ``graph_snapshot`` dimension (T4, already tested in
``test_routing_domain.py``), not this module's concern.
"""

from __future__ import annotations

from book_graph_rag.domain.cache_policy import CacheDecisionReason, should_cache
from book_graph_rag.domain.namespaces import SourceNamespace
from book_graph_rag.domain.orchestration_models import Mode
from book_graph_rag.domain.orchestration_outcomes import (
    CoverageState,
    FallbackOutcome,
    GraphEvidence,
    Passage,
)

_AAP = "knowledge:agentic-architectural-patterns"


def _ns(namespace_id: str) -> SourceNamespace:
    corpus, source = namespace_id.split(":", maxsplit=1)
    return SourceNamespace(corpus=corpus, source=source)


def _evidence(*citations: str) -> GraphEvidence:
    return GraphEvidence(namespace=_ns(_AAP), summary="graph summary", citations=tuple(citations))


def _passage(chunk_id: str) -> Passage:
    return Passage(chunk_id=chunk_id, text="literal coverage")


def _outcome(
    state: CoverageState,
    *,
    evidence: tuple[GraphEvidence, ...] = (),
    passages: tuple[Passage, ...] = (),
) -> FallbackOutcome:
    return FallbackOutcome(
        state=state,
        evidence=evidence,
        passages=passages,
        reason="router reason",
    )


def test_insufficient_coverage_is_never_cacheable() -> None:
    """S10: a fallback-derived answer is never cached as a graph answer."""
    outcome = _outcome(
        CoverageState.INSUFFICIENT_COVERAGE,
        passages=(_passage("chunk-9"),),
    )

    decision = should_cache(outcome, Mode.EVIDENCE)

    assert decision.cacheable is False
    assert decision.reason is CacheDecisionReason.INSUFFICIENT_COVERAGE


def test_covered_outcome_is_cacheable() -> None:
    """Baseline: covered graph evidence with a citation passes the policy."""
    outcome = _outcome(CoverageState.COVERED, evidence=(_evidence("chunk-1"),))

    decision = should_cache(outcome, Mode.EVIDENCE)

    assert decision.cacheable is True
    assert decision.reason is CacheDecisionReason.CACHEABLE


def test_fanout_with_merged_evidence_is_cacheable() -> None:
    """S8: merged top-2 fan-out evidence is graph coverage — cacheable."""
    outcome = _outcome(
        CoverageState.FANOUT,
        evidence=(_evidence("chunk-1", "chunk-2"), _evidence("chunk-3")),
    )

    decision = should_cache(outcome, Mode.EVIDENCE)

    assert decision.cacheable is True
    assert decision.reason is CacheDecisionReason.CACHEABLE


def test_covered_with_caller_demanded_passages_is_cacheable() -> None:
    """S10 nuance (L8/L9): passages on demand ≠ "entidades incompletas".

    The graph still covered the question; the passages ride along only
    because the caller demanded them (S5's ``requires_passages``). The
    decision reads ``state`` and never ``passages``, so this outcome stays
    cacheable and the coverage reason must not appear.
    """
    outcome = _outcome(
        CoverageState.COVERED,
        evidence=(_evidence("chunk-1"),),
        passages=(_passage("chunk-2"),),
    )

    decision = should_cache(outcome, Mode.EVIDENCE)

    assert decision.cacheable is True
    assert decision.reason is not CacheDecisionReason.INSUFFICIENT_COVERAGE
    assert decision.reason is CacheDecisionReason.CACHEABLE


def test_synthesis_with_citation_is_cacheable() -> None:
    """S11 happy path: synthesis with provenance (one non-empty citation)."""
    outcome = _outcome(CoverageState.COVERED, evidence=(_evidence("chunk-1"),))

    decision = should_cache(outcome, Mode.SYNTHESIS)

    assert decision.cacheable is True
    assert decision.reason is CacheDecisionReason.CACHEABLE


def test_synthesis_without_citations_is_not_cacheable() -> None:
    """S11: synthesis with zero citations everywhere lacks clear provenance."""
    outcome = _outcome(CoverageState.COVERED, evidence=(_evidence(), _evidence()))

    decision = should_cache(outcome, Mode.SYNTHESIS)

    assert decision.cacheable is False
    assert decision.reason is CacheDecisionReason.SYNTHESIS_WITHOUT_PROVENANCE


def test_evidence_mode_with_zero_citations_is_cacheable() -> None:
    """S11 is synthesis-scoped: thin citations must not block evidence mode."""
    outcome = _outcome(CoverageState.COVERED, evidence=(_evidence(),))

    decision = should_cache(outcome, Mode.EVIDENCE)

    assert decision.cacheable is True
    assert decision.reason is CacheDecisionReason.CACHEABLE


def test_evidence_mode_with_empty_evidence_is_not_cacheable() -> None:
    """Defensive: evidence mode with an empty evidence tuple fails closed."""
    outcome = _outcome(CoverageState.COVERED)

    decision = should_cache(outcome, Mode.EVIDENCE)

    assert decision.cacheable is False
    assert decision.reason is CacheDecisionReason.NO_EVIDENCE


def test_coverage_rule_outranks_synthesis_rule() -> None:
    """Precedence: both rules fire here; S10's reason wins over S11's.

    Empty evidence in synthesis mode would trip the coverage rule (state) and
    the synthesis rule (no provenance) alike; the documented order reports
    the coverage reason, never the synthesis one.
    """
    outcome = _outcome(CoverageState.INSUFFICIENT_COVERAGE)

    decision = should_cache(outcome, Mode.SYNTHESIS)

    assert decision.cacheable is False
    assert decision.reason is CacheDecisionReason.INSUFFICIENT_COVERAGE
