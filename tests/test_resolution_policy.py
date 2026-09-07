"""Tests for the binding band-to-action matrix (Slice D).

This module is the load-bearing pin for the policy clarification in engram 1165.
Any future refactor that changes routing must keep these parametrized cases green.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from book_graph_rag.domain.models import EntityType
from book_graph_rag.domain.resolution_models import (
    ConfidenceBand,
    ResolutionEvidence,
    S0NormalizedForm,
    S1EmbeddingSignal,
    S2TypeGateResult,
    S3ContextSignals,
)
from book_graph_rag.domain.resolution_policy import (
    MergeAction,
    decide,
)
from book_graph_rag.domain.s4_band_assignment import BandThresholds


def _norm(original: str) -> S0NormalizedForm:
    folded = original.casefold()
    return S0NormalizedForm(
        original=original,
        nfkc=folded,
        casefold=folded,
        compact=folded,
        tokens=(folded,),
    )


def _s3(
    mentions: float = 0.0,
    related: float = 0.0,
    desc: float = 0.0,
    conflict: bool = False,
) -> S3ContextSignals:
    return S3ContextSignals(
        mentions_jaccard=mentions,
        related_jaccard=related,
        description_overlap=desc,
        mentions_source_count=1,
        mentions_shared_count=0,
        related_neighbor_count=1,
        related_shared_count=0,
        conflict_flag=conflict,
    )


def _evidence(
    *,
    band: ConfidenceBand,
    cross_type: bool,
    cross_namespace: bool,
    anchor_type: EntityType = "concept",
    candidate_type: EntityType = "concept",
) -> ResolutionEvidence:
    """Build a minimal but valid evidence record for policy routing tests."""
    return ResolutionEvidence(
        anchor_id="corp:src:anchor-concept",
        candidate_id="corp:src:candidate-concept",
        anchor_type=anchor_type,
        candidate_type=candidate_type,
        anchor_namespace="corp:src",
        candidate_namespace="other:src" if cross_namespace else "corp:src",
        anchor_normalized=_norm("Anchor"),
        candidate_normalized=_norm("Candidate"),
        s0_matched_field="id" if band == ConfidenceBand.EXACT else "none",
        s1=S1EmbeddingSignal(
            cosine_similarity=0.95 if band == ConfidenceBand.HIGH else 0.85,
            candidate_rank=1,
            input_variant="A",
            model_id="paraphrase-multilingual-MiniLM-L12-v2",
        ),
        s2=S2TypeGateResult(
            anchor_type=anchor_type,
            candidate_type=candidate_type,
            passed=not cross_type,
            reason="type gate result",
        ),
        s3=_s3(0.6, 0.6, 0.6),
        composite_score=0.6,
        band=band,
        cross_namespace=cross_namespace,
        cross_type=cross_type,
        decided_at=datetime.now(UTC),
    )


@pytest.mark.parametrize(
    ("band", "cross_namespace", "expected_action"),
    [
        (ConfidenceBand.EXACT, False, MergeAction.AUTO_MERGE),
        (ConfidenceBand.HIGH, False, MergeAction.QUARANTINE),  # human-confirm queue
        (ConfidenceBand.MEDIUM, False, MergeAction.QUARANTINE),
        (ConfidenceBand.LOW, False, MergeAction.NO_MERGE),
        (ConfidenceBand.EXACT, True, MergeAction.QUARANTINE),  # cross-namespace wins
        (ConfidenceBand.HIGH, True, MergeAction.QUARANTINE),
        (ConfidenceBand.MEDIUM, True, MergeAction.QUARANTINE),
        (ConfidenceBand.LOW, True, MergeAction.QUARANTINE),
    ],
)
def test_policy_band_matrix(
    band: ConfidenceBand,
    cross_namespace: bool,
    expected_action: MergeAction,
) -> None:
    evidence = _evidence(band=band, cross_type=False, cross_namespace=cross_namespace)
    decision = decide(evidence, BandThresholds())
    assert decision.action == expected_action
    assert isinstance(decision.rationale, str)
    assert len(decision.rationale) > 0


@pytest.mark.parametrize("band", list(ConfidenceBand))
def test_cross_type_always_no_merge(band: ConfidenceBand) -> None:
    evidence = _evidence(band=band, cross_type=True, cross_namespace=False)
    decision = decide(evidence, BandThresholds())
    assert decision.action == MergeAction.NO_MERGE
    assert "R6.1" in decision.rationale


@pytest.mark.parametrize("band", list(ConfidenceBand))
def test_cross_namespace_always_quarantine(band: ConfidenceBand) -> None:
    evidence = _evidence(band=band, cross_type=False, cross_namespace=True)
    decision = decide(evidence, BandThresholds())
    assert decision.action == MergeAction.QUARANTINE
    assert "R6.2" in decision.rationale


def test_cross_type_takes_precedence_over_cross_namespace() -> None:
    """Both flags set: cross-type (NO_MERGE) is checked first per design."""
    evidence = _evidence(
        band=ConfidenceBand.EXACT,
        cross_type=True,
        cross_namespace=True,
    )
    decision = decide(evidence, BandThresholds())
    assert decision.action == MergeAction.NO_MERGE
    assert "R6.1" in decision.rationale


def test_high_band_rationale_cites_human_confirm() -> None:
    evidence = _evidence(band=ConfidenceBand.HIGH, cross_type=False, cross_namespace=False)
    decision = decide(evidence, BandThresholds())
    assert decision.action == MergeAction.QUARANTINE
    assert "human-confirm" in decision.rationale.lower()


def test_exact_band_rationale_cites_auto_merge() -> None:
    evidence = _evidence(band=ConfidenceBand.EXACT, cross_type=False, cross_namespace=False)
    decision = decide(evidence, BandThresholds())
    assert decision.action == MergeAction.AUTO_MERGE
    assert "exact" in decision.rationale.lower()


def test_decision_returns_evidence_unchanged() -> None:
    evidence = _evidence(band=ConfidenceBand.LOW, cross_type=False, cross_namespace=False)
    decision = decide(evidence, BandThresholds())
    assert decision.evidence == evidence
