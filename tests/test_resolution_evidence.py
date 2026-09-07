"""Tests for the ResolutionEvidence model set (Slice B)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from book_graph_rag.domain.models import Entity
from book_graph_rag.domain.resolution_models import (
    ConfidenceBand,
    ResolutionEvidence,
    S0NormalizedForm,
    S1EmbeddingSignal,
    S2TypeGateResult,
    S3ContextSignals,
)


def _make_norm(original: str) -> S0NormalizedForm:
    folded = original.casefold()
    return S0NormalizedForm(
        original=original,
        nfkc=folded,
        casefold=folded,
        compact=folded,
        tokens=(folded,),
    )


def test_resolution_evidence_construction_from_entities() -> None:
    """ResolutionEvidence can be built from real Entity domain models."""
    anchor = Entity(
        id="corp:src:anchor-concept",
        name="Anchor",
        type="concept",
        canonical_name="Anchor Canonical",
    )
    candidate = Entity(
        id="corp:src:candidate-concept",
        name="Candidate",
        type="concept",
    )

    ev = ResolutionEvidence(
        anchor_id=anchor.id,
        candidate_id=candidate.id,
        anchor_type=anchor.type,
        candidate_type=candidate.type,
        anchor_namespace="corp:src",
        candidate_namespace="corp:src",
        anchor_normalized=_make_norm(anchor.name),
        candidate_normalized=_make_norm(candidate.name),
        s0_matched_field="canonical",
        s1=S1EmbeddingSignal(
            cosine_similarity=0.95,
            candidate_rank=1,
            input_variant="A",
            model_id="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        ),
        s2=S2TypeGateResult(
            anchor_type="concept",
            candidate_type="concept",
            passed=True,
            reason="anchor type concept matches candidate type concept",
        ),
        s3=S3ContextSignals(
            mentions_jaccard=0.5,
            related_jaccard=0.5,
            description_overlap=0.5,
            mentions_source_count=2,
            mentions_shared_count=1,
            related_neighbor_count=2,
            related_shared_count=1,
            conflict_flag=False,
        ),
        composite_score=0.5,
        band=ConfidenceBand.HIGH,
        cross_namespace=False,
        cross_type=False,
        decided_at=datetime.now(UTC),
    )

    assert ev.anchor_id == anchor.id
    assert ev.candidate_id == candidate.id
    assert ev.s0_matched_field == "canonical"
    assert ev.band == ConfidenceBand.HIGH
    assert ev.s1 is not None
    assert ev.s1.input_variant == "A"
    assert ev.s2 is not None
    assert ev.s2.passed is True
    assert ev.s3 is not None
    assert ev.s3.conflict_flag is False


def test_resolution_evidence_json_round_trip() -> None:
    """Frozen Pydantic models round-trip through JSON byte-equivalently."""
    ev = ResolutionEvidence(
        anchor_id="a",
        candidate_id="b",
        anchor_type="concept",
        candidate_type="concept",
        anchor_namespace="corp:src",
        candidate_namespace="corp:src",
        anchor_normalized=S0NormalizedForm(
            original="A", nfkc="a", casefold="a", compact="a", tokens=("a",)
        ),
        candidate_normalized=S0NormalizedForm(
            original="B", nfkc="b", casefold="b", compact="b", tokens=("b",)
        ),
        s0_matched_field="none",
        band=ConfidenceBand.LOW,
        cross_namespace=False,
        cross_type=False,
        decided_at=datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC),
    )

    json_str = ev.model_dump_json()
    restored = ResolutionEvidence.model_validate_json(json_str)

    assert restored == ev


def test_resolution_evidence_timestamp_is_part_of_identity() -> None:
    """Changing decided_at produces a different evidence value."""
    base = ResolutionEvidence(
        anchor_id="a",
        candidate_id="b",
        anchor_type="concept",
        candidate_type="concept",
        anchor_namespace="corp:src",
        candidate_namespace="corp:src",
        anchor_normalized=S0NormalizedForm(
            original="A", nfkc="a", casefold="a", compact="a", tokens=("a",)
        ),
        candidate_normalized=S0NormalizedForm(
            original="B", nfkc="b", casefold="b", compact="b", tokens=("b",)
        ),
        s0_matched_field="none",
        band=ConfidenceBand.LOW,
        cross_namespace=False,
        cross_type=False,
        decided_at=datetime.now(UTC),
    )

    later = base.decided_at + timedelta(seconds=5)
    updated = base.model_copy(update={"decided_at": later})

    assert updated.decided_at == later
    assert base != updated
