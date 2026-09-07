"""Immutable evidence models for semantic entity resolution (Slice B).

All models are pure Pydantic + stdlib. They are frozen and forbid extra fields
so evidence records are hash-stable and serializable.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from book_graph_rag.domain.models import EntityType


class ConfidenceBand(StrEnum):
    """Confidence band assigned by the S0–S4 resolution pipeline."""

    EXACT = "exact"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class S0NormalizedForm(BaseModel):
    """Deterministic normalized form used for S0 exact matching."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    original: str
    nfkc: str
    casefold: str
    compact: str
    tokens: tuple[str, ...]


class S2TypeGateResult(BaseModel):
    """Result of the S2 hard type-boundary check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    anchor_type: EntityType
    candidate_type: EntityType
    passed: bool
    reason: str


class S3ContextSignals(BaseModel):
    """Per-spec evidence fields for context/relationship validation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mentions_jaccard: float = Field(ge=0.0, le=1.0)
    related_jaccard: float = Field(ge=0.0, le=1.0)
    description_overlap: float = Field(ge=0.0, le=1.0)
    mentions_source_count: int = Field(ge=0)
    mentions_shared_count: int = Field(ge=0)
    related_neighbor_count: int = Field(ge=0)
    related_shared_count: int = Field(ge=0)
    conflict_flag: bool


class S1EmbeddingSignal(BaseModel):
    """Embedding-based similarity signal produced by S1 retrieval."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cosine_similarity: float = Field(ge=0.0, le=1.0)
    candidate_rank: int = Field(ge=1)
    input_variant: Literal["A", "B"]
    model_id: str


class ResolutionEvidence(BaseModel):
    """Per-candidate-pair evidence. Immutable record of the staged evaluation.

    S1/S2/S3 are optional because S0 exact matches short-circuit later stages;
    downstream stages create a complete copy via ``model_copy(update=...)``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    anchor_id: str
    candidate_id: str
    anchor_type: EntityType
    candidate_type: EntityType
    anchor_namespace: str
    candidate_namespace: str

    # S0
    anchor_normalized: S0NormalizedForm
    candidate_normalized: S0NormalizedForm
    s0_matched_field: Literal["id", "canonical", "alias", "none"]

    # S1 (populated when S1 runs; None for S0-only exact matches)
    s1: S1EmbeddingSignal | None = None

    # S2 (populated when S2 runs; None for S0-only exact matches)
    s2: S2TypeGateResult | None = None

    # S3 (populated when S3 runs; None for S0-only exact matches)
    s3: S3ContextSignals | None = None

    # S4
    composite_score: float | None = Field(default=None, ge=0.0, le=1.0)
    band: ConfidenceBand
    cross_namespace: bool
    cross_type: bool
    decided_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
