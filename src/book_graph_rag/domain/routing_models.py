"""Pure-domain models and functions for namespace question routing.

This module is the router's decision kernel: it knows how to score a question
embedding against namespace centroids and how to decide a route from ranked
scores. It imports only stdlib and pydantic, and it never touches Neo4j,
SQLite, sentence-transformers, or infrastructure adapters.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from book_graph_rag.domain.namespaces import Catalog, SourceNamespace

RouteKind = Literal["single", "multi", "abstain", "out_of_domain"]


def hints_from_catalog(catalog: Catalog) -> tuple[LexicalHints, ...]:
    """Derive cheap deterministic hint terms from the versioned catalog.

    Terms come from the source slug, its human label, and the label stem
    before any parenthetical qualifier.
    """
    hints: list[LexicalHints] = []
    for corpus, corpus_data in catalog.corpora.items():
        for source, source_data in corpus_data.sources.items():
            if source_data.status != "active":
                continue
            terms = tuple(
                dict.fromkeys((source, source_data.label, source_data.label.split("(")[0].strip()))
            )
            hints.append(
                LexicalHints(
                    namespace=SourceNamespace(corpus=corpus, source=source),
                    terms=terms,
                )
            )
    return tuple(hints)


def route_cache_key(
    question_fingerprint: str,
    *,
    namespace: str | None,
    model_id: str,
    profile_version: str,
    catalog_version: str,
    graph_snapshot: str,
) -> str:
    """Derive a canonical cache key from every routing-relevant dimension.

    A change in model, profile, catalog, or graph snapshot invalidates the key
    so a stale routing decision can never be served by cache.
    """
    payload = json.dumps(
        {
            "question_fingerprint": question_fingerprint,
            "namespace": namespace,
            "model_id": model_id,
            "profile_version": profile_version,
            "catalog_version": catalog_version,
            "graph_snapshot": graph_snapshot,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class NamespaceProfile(BaseModel):
    """Immutable semantic centroid for one active book namespace."""

    model_config = ConfigDict(frozen=True)

    namespace: SourceNamespace
    centroid: tuple[float, ...]
    model_id: str
    dimension: int
    profile_version: str
    catalog_version: str = ""
    graph_snapshot: str = ""
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def _dimension_matches(self) -> NamespaceProfile:
        if len(self.centroid) != self.dimension:
            raise ValueError(
                f"centroid dimension {len(self.centroid)} does not match "
                f"declared dimension {self.dimension}"
            )
        return self


class ScoredCandidate(BaseModel):
    """One namespace ranked by cosine similarity against the question."""

    model_config = ConfigDict(frozen=True)

    namespace: SourceNamespace
    score: float = Field(ge=-1.0, le=1.0)


class LexicalHints(BaseModel):
    """Cheap, deterministic title/TOC-style terms for one namespace."""

    model_config = ConfigDict(frozen=True)

    namespace: SourceNamespace
    terms: tuple[str, ...]


class ResolvedRoute(BaseModel):
    """Outcome of the runtime router: decision plus validated namespace(s).

    ``validated_namespace`` is the selected namespace only when it passed
    catalog validation. ``fanout_namespaces`` carries at most two validated
    candidates for controlled multi-scope dispatch on abstention; it is empty
    when no validated candidate exists.
    """

    model_config = ConfigDict(frozen=True)

    decision: RouteDecision
    validated_namespace: SourceNamespace | None = None
    fanout_namespaces: tuple[SourceNamespace, ...] = ()


class RouteThresholds(BaseModel):
    """Calibration knobs for the abstention policy.

    The initial values are provisional; Unit 3 calibrates them against the
    committed routing dataset before runtime use.
    """

    model_config = ConfigDict(frozen=True)

    min_top_score: float = Field(default=0.45, ge=0.0, le=1.0)
    min_margin: float = Field(default=0.10, ge=0.0, le=1.0)


class RouteDecision(BaseModel):
    """Deterministic outcome of scoring and abstention policy."""

    model_config = ConfigDict(frozen=True)

    selected: SourceNamespace | None = None
    candidates: tuple[ScoredCandidate, ...] = ()
    top_score: float = Field(default=0.0, ge=0.0, le=1.0)
    margin: float = 0.0
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    route_kind: RouteKind
    reason: str


def cosine_similarity(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """Return cosine similarity in ``[-1.0, 1.0]``; zero-norm vectors score 0.0.

    Raises:
        ValueError: When the vectors have different lengths.
    """
    if len(a) != len(b):
        raise ValueError(f"vectors must have the same dimension: {len(a)} != {len(b)}")
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / (norm_a * norm_b)


def normalize_vector(vector: tuple[float, ...]) -> tuple[float, ...]:
    """Return the L2-normalized copy of ``vector``.

    Raises:
        ValueError: When ``vector`` has zero norm (no meaningful direction).
    """
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        raise ValueError("cannot normalize a zero-norm vector")
    return tuple(value / norm for value in vector)


def mean_normalized(vectors: Sequence[tuple[float, ...]]) -> tuple[float, ...]:
    """Average per-dimension values and re-normalize to unit norm.

    Raises:
        ValueError: When ``vectors`` is empty or dimensions differ.
    """
    if not vectors:
        raise ValueError("cannot build a centroid from zero vectors")
    dimension = len(vectors[0])
    if any(len(vector) != dimension for vector in vectors):
        raise ValueError("all vectors must share the same dimension")
    sums = [0.0] * dimension
    for vector in vectors:
        for index, value in enumerate(vector):
            sums[index] += value
    mean = tuple(sum_value / len(vectors) for sum_value in sums)
    return normalize_vector(mean)


def match_lexical_hints(
    question: str,
    hints: Sequence[LexicalHints],
) -> tuple[ScoredCandidate, ...]:
    """Return scored candidates for every lexically matched hint, best-first.

    Matching is case-insensitive substring matching; each hit scores 1.0.
    This is the cheap fast path that avoids embedding obvious questions.
    """
    lower = question.lower()
    scored = [
        ScoredCandidate(namespace=hint.namespace, score=1.0)
        for hint in hints
        if any(term.lower() in lower for term in hint.terms)
    ]
    scored.sort(key=lambda candidate: (-candidate.score, candidate.namespace.source_id))
    return tuple(scored)


def score_against_profiles(
    question_vector: tuple[float, ...],
    profiles: Sequence[NamespaceProfile],
) -> tuple[ScoredCandidate, ...]:
    """Score ``question_vector`` against every profile, ordered best-first.

    Ties are broken by ``source_id`` so the ordering is deterministic.
    """
    scored = [
        ScoredCandidate(
            namespace=profile.namespace,
            score=cosine_similarity(question_vector, profile.centroid),
        )
        for profile in profiles
    ]
    scored.sort(key=lambda candidate: (-candidate.score, candidate.namespace.source_id))
    return tuple(scored)


def decide_route(
    ranked: Sequence[ScoredCandidate],
    thresholds: RouteThresholds,
) -> RouteDecision:
    """Apply the abstention policy to ranked candidates.

    The decision kernel never fabricates a namespace: low score or low margin
    yields an explicit ``abstain``. Multi-scope fan-out is modeled at the
    application layer (Unit 3), not here.
    """
    ordered = tuple(sorted(ranked, key=lambda c: (-c.score, c.namespace.source_id)))
    if not ordered:
        return _abstain(ordered, "no_profiles")

    top = ordered[0]
    second = ordered[1] if len(ordered) > 1 else None
    margin = top.score - second.score if second is not None else top.score

    if top.score < thresholds.min_top_score:
        return _abstain(ordered, "low_score")
    if margin < thresholds.min_margin:
        return _abstain(ordered, "low_margin")

    return RouteDecision(
        selected=top.namespace,
        candidates=ordered,
        top_score=top.score,
        margin=margin,
        confidence=top.score,
        route_kind="single",
        reason="clear",
    )


def _abstain(
    ordered: Sequence[ScoredCandidate],
    reason: str,
) -> RouteDecision:
    """Build an abstention decision from ordered candidates."""
    top_score = ordered[0].score if ordered else 0.0
    return RouteDecision(
        selected=None,
        candidates=tuple(ordered),
        top_score=top_score,
        margin=0.0,
        confidence=0.0,
        route_kind="abstain",
        reason=reason,
    )
