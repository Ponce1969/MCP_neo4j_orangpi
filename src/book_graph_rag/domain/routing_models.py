"""Pure-domain models and functions for namespace question routing.

This module is the router's decision kernel: it knows how to select profile
prototypes, to score a question embedding against namespace profiles, and to
decide a route from ranked scores. It imports only stdlib and pydantic, and it
never touches Neo4j, SQLite, sentence-transformers, or infrastructure adapters.
"""

from __future__ import annotations

import hashlib
import hmac
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


def hmac_query_fingerprint(
    question: str,
    *,
    key_id: str,
    secret: str | bytes,
) -> str:
    """Return a keyed HMAC-SHA256 fingerprint of ``question`` (hex digest).

    The raw question is never stored and never hashed plainly; the keyed
    digest lets telemetry correlate repeated questions without leaking text
    or the key. A different key or key id yields a different digest.
    """
    key_bytes = secret.encode("utf-8") if isinstance(secret, str) else secret
    canonical = json.dumps(
        {"question": question, "key_id": key_id},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hmac.new(key_bytes, canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def route_cache_key(
    question_fingerprint: str,
    *,
    namespace: str | None,
    mode: str,
    model_id: str,
    profile_version: str,
    catalog_version: str,
    graph_snapshot: str,
) -> str:
    """Derive a canonical cache key from every routing-relevant dimension.

    Seven dimensions: question fingerprint, namespace/scope, mode (evidence
    vs synthesis), model, profile, catalog, and graph snapshot. A change in
    any of them invalidates the key so a stale routing decision can never be
    served by cache.

    Raises:
        ValueError: When ``mode`` is empty or whitespace-only. The value is
            rejected, never silently normalized.
    """
    if not mode.strip():
        raise ValueError("mode must not be blank")
    payload = json.dumps(
        {
            "question_fingerprint": question_fingerprint,
            "namespace": namespace,
            "mode": mode,
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
    """Immutable routing profile for one active book namespace (shape v2, S1).

    ``prototypes`` holds up to ``k`` representative vectors selected from the
    book's own texts, and ``prototype_sources`` identifies, per prototype, the
    ``kind:ordinal`` text that produced it (same length as ``prototypes``).
    ``centroid`` is the normalized mean of the same vectors: the pure-prototype
    path never reads it (S1/L2, pinned by
    ``test_prototype_scoring_ignores_centroid``), while the blend path reads it
    **deliberately** as its weighted term — at the default ``w = 1.0`` it is
    the only term that scores (T5, decisions L5/L6).
    """

    model_config = ConfigDict(frozen=True)

    namespace: SourceNamespace
    centroid: tuple[float, ...]
    model_id: str
    dimension: int
    profile_version: str
    prototypes: tuple[tuple[float, ...], ...] = ()
    k: int = 0
    prototype_sources: tuple[str, ...] = ()
    catalog_version: str = ""
    graph_snapshot: str = ""
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def _profile_shape(self) -> NamespaceProfile:
        """Enforce the v2 prototype shape (S1) without breaking v1 callers.

        Every invariant is checked whenever it can be violated. The prototype
        block invariants (non-empty, ``k >= 1``) apply only when at least one
        prototype field was explicitly provided — in a constructor call or in
        a serialized artifact. A profile constructed *without* any prototype
        field is the legacy v1 shape: it stays valid in memory for callers
        that only read the diagnostic centroid, and artifact readers must
        still reject it (the JSON store checks the ``prototypes`` field).
        """
        if len(self.centroid) != self.dimension:
            raise ValueError(
                f"centroid dimension {len(self.centroid)} does not match "
                f"declared dimension {self.dimension}"
            )
        for index, prototype in enumerate(self.prototypes):
            if len(prototype) != self.dimension:
                raise ValueError(
                    f"prototype {index} dimension {len(prototype)} does not match "
                    f"declared dimension {self.dimension}"
                )
        if len(self.prototypes) != len(self.prototype_sources):
            raise ValueError(
                f"prototype_sources length {len(self.prototype_sources)} does not match "
                f"prototypes length {len(self.prototypes)}"
            )
        prototype_block_present = bool(
            self.model_fields_set & {"prototypes", "prototype_sources", "k"}
        )
        if prototype_block_present:
            if not self.prototypes:
                raise ValueError("prototypes must not be empty")
            if self.k < 1:
                raise ValueError(f"k must be >= 1, got {self.k}")
        if self.k != len(self.prototypes):
            raise ValueError(f"k {self.k} does not match prototypes length {len(self.prototypes)}")
        return self


class ScoredCandidate(BaseModel):
    """One namespace ranked by cosine similarity against the question.

    ``best_prototype_source`` (S7) is the ``prototype_sources`` entry of the
    prototype that produced ``score``, filled by the prototype term so a
    decision names a text instead of a vector. It stays ``None`` when no
    prototype was involved: lexical-hint candidates are matched by string and
    never embedded, and a profile without prototypes has no source to report —
    both yield ``None`` rather than a made-up identifier.

    **At ``w = 1.0`` the source is diagnostic, not causal**: the prototype
    term has weight zero, so no fragment decided the route. A reader must
    consult ``RouteDecision.strategy`` before claiming a fragment did (S7/L5);
    a silently causal-looking field would be worse than none.
    """

    model_config = ConfigDict(frozen=True)

    namespace: SourceNamespace
    score: float = Field(ge=-1.0, le=1.0)
    best_prototype_source: str | None = None


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

    Defaults were re-calibrated on the Orange Pi against the committed routing
    dataset once the fourth namespace existed (2026-10-01): min_top_score=0.10,
    min_margin=0.05 give single_accuracy 0.682, wrong_namespace_rate 0.0,
    abstention_rate 0.484 under full-book-average centroids. The three-namespace
    run (2026-09-29) recommended the same pair with single_accuracy 0.727 and
    abstention_rate 0.452, so the numeric defaults did not change.
    See docs/ops/namespace-routing-baseline.md.
    """

    model_config = ConfigDict(frozen=True)

    min_top_score: float = Field(default=0.10, ge=0.0, le=1.0)
    min_margin: float = Field(default=0.05, ge=0.0, le=1.0)


class RouteDecision(BaseModel):
    """Deterministic outcome of scoring and abstention policy.

    S7 provenance: ``strategy`` names the scoring path that produced
    ``candidates`` — ``"centroid"`` (the default, ``w = 1.0``: cosine against
    the profile means), ``"prototypes"`` (``w = 0.0``: max cosine over profile
    prototypes), ``"blend"`` (``0 < w < 1``: the weighted sum of both),
    ``"lexical_hints"`` (the cheap fast path: no embedding, no prototype),
    or the default ``"unspecified"`` for a decision built without provenance
    (domain-level tests, explicit-scope shortcuts). ``profile_k`` is the
    ``k`` of the profile set that was actually scored; it is ``None``
    whenever no profile set was scored — lexical fast path or empty profile
    set — never a guess. The application layer populates both from the
    profile set it really used.
    """

    model_config = ConfigDict(frozen=True)

    selected: SourceNamespace | None = None
    candidates: tuple[ScoredCandidate, ...] = ()
    top_score: float = Field(default=0.0, ge=0.0, le=1.0)
    margin: float = 0.0
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    route_kind: RouteKind
    reason: str
    strategy: str = "unspecified"
    profile_k: int | None = None


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


def select_prototypes(vectors: Sequence[tuple[float, ...]], k: int) -> tuple[int, ...]:
    """Select ``k`` prototype indices by deterministic farthest-point sampling (S3).

    The seed is the vector closest (by cosine distance) to the arithmetic mean
    of the whole set — the point a single centroid used to represent. Each
    following pick is the vector that maximises its **minimum** cosine distance
    to every vector already chosen (equivalently, minimises its maximum cosine
    similarity to them).

    **Tie-break: at every step a tie resolves to the lowest input index**
    (seed and greedy picks alike), so the function contains no randomness — no
    RNG, no seed — and the same vector list always yields the same index
    tuple, in selection order. That byte-for-byte determinism is what makes an
    A/B measurement over the artifact meaningful.

    When there are fewer vectors than ``k``, every index is returned exactly
    once: no padding, no crash.

    Raises:
        ValueError: When ``vectors`` is empty, ``k < 1``, or the vectors do
            not all share one dimension.
    """
    if not vectors:
        raise ValueError("cannot select prototypes from zero vectors")
    if k < 1:
        raise ValueError(f"k must be >= 1, got {k}")
    dimension = len(vectors[0])
    if any(len(vector) != dimension for vector in vectors):
        raise ValueError("all vectors must share the same dimension")

    mean = tuple(
        sum(vector[index] for vector in vectors) / len(vectors) for index in range(dimension)
    )
    # Seed: closest to the mean; strict comparison keeps the lowest index on ties.
    seed_index = 0
    best_similarity = cosine_similarity(vectors[0], mean)
    for index in range(1, len(vectors)):
        similarity = cosine_similarity(vectors[index], mean)
        if similarity > best_similarity:
            best_similarity = similarity
            seed_index = index

    selected = [seed_index]
    remaining = [index for index in range(len(vectors)) if index != seed_index]
    target = min(k, len(vectors))
    while len(selected) < target:
        # Farthest point: max over remaining of min cosine distance to chosen.
        # Iteration stays in ascending index order and comparisons are strict,
        # so ties resolve to the lowest index.
        best_index = remaining[0]
        best_distance = -math.inf
        for index in remaining:
            distance = min(
                1.0 - cosine_similarity(vectors[index], vectors[chosen]) for chosen in selected
            )
            if distance > best_distance:
                best_distance = distance
                best_index = index
        selected.append(best_index)
        remaining.remove(best_index)
    return tuple(selected)


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
    """Score ``question_vector`` against every profile's ``centroid`` (T5 reference).

    This is the ``w = 1.0`` endpoint: ``score_blend`` at ``blend_w=1.0``
    reproduces these scores value for value (pinned by
    ``test_blend_w_one_reproduces_centroid_scores_value_for_value``) while
    additionally reporting S7 provenance, and the live router scores through
    ``score_blend`` (T5, decisions L5/L6). It survives as the comparison
    ``tests/unit/test_route_question_use_case.py`` uses to prove the prototype
    ranking inverts on multimodal books, and as the regression reference for
    the pre-T1 centroid route. Ties are broken by ``source_id`` so the
    ordering stays deterministic.
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


def _best_prototype_match(
    question_vector: tuple[float, ...],
    profile: NamespaceProfile,
) -> tuple[float, str]:
    """Return ``profile``'s best prototype cosine and its source identifier.

    Argmax with strict ``>`` keeps the lowest prototype index on a tie —
    identical to ``max()`` over the same values, so the score is unaffected
    and the reported source stays deterministic (S7, same lowest-index rule
    ``select_prototypes`` uses per S3).

    Raises:
        ValueError: When the profile carries no prototypes.
    """
    if not profile.prototypes:
        raise ValueError(
            f"profile for {profile.namespace.source_id} has no prototypes; "
            "score_against_prototypes only scores prototype (v2) profiles"
        )
    best_index = 0
    best_score = cosine_similarity(question_vector, profile.prototypes[0])
    for index in range(1, len(profile.prototypes)):
        similarity = cosine_similarity(question_vector, profile.prototypes[index])
        if similarity > best_score:
            best_score = similarity
            best_index = index
    return best_score, profile.prototype_sources[best_index]


def score_against_prototypes(
    question_vector: tuple[float, ...],
    profiles: Sequence[NamespaceProfile],
) -> tuple[ScoredCandidate, ...]:
    """Score each profile by its best prototype (S2), ordered best-first.

    ``score(book) = max(cosine(question, p) for p in prototypes)``. The
    function reads only ``namespace`` and ``prototypes`` — never the passive
    diagnostic ``centroid`` — so corrupting or removing a centroid cannot
    change any score (S1/L2, pinned by ``test_prototype_scoring_ignores_centroid``).

    This is also the ``w = 0.0`` endpoint of the blend (T5): ``score_blend``
    at ``blend_w=0.0`` returns this function's output **verbatim** — scores,
    ordering and ``best_prototype_source`` are bit for bit identical.

    Ties are broken by ``source_id`` exactly like the centroid path, so the
    ranking stays deterministic. Each candidate also reports S7 provenance:
    ``best_prototype_source`` is the ``prototype_sources`` entry of the
    prototype that reached the max. Within one profile a tie for the best
    prototype resolves to the **first prototype in selection order** (strict
    ``>``, the same lowest-index rule ``select_prototypes`` uses), so the
    reported source is deterministic too; the score itself is unaffected
    because a tie cannot change the maximum.

    Raises:
        ValueError: When a profile carries no prototypes. The prototype path
            only scores v2 profiles; prototype-less (legacy) profiles belong
            to ``score_against_profiles``.
    """
    scored: list[ScoredCandidate] = []
    for profile in profiles:
        best_score, best_source = _best_prototype_match(question_vector, profile)
        scored.append(
            ScoredCandidate(
                namespace=profile.namespace,
                score=best_score,
                best_prototype_source=best_source,
            )
        )
    scored.sort(key=lambda candidate: (-candidate.score, candidate.namespace.source_id))
    return tuple(scored)


def blend_strategy(blend_w: float) -> str:
    """Return the S7 strategy label for ``blend_w`` (T5, decisions L5/L6).

    ``1.0`` reports ``"centroid"`` — the prototype term has weight zero, so
    naming it would be false provenance; ``0.0`` reports ``"prototypes"``;
    anything strictly in between reports ``"blend"``. There is no fourth
    label, and ``"prototypes"`` is never reported when that term has zero
    weight.

    Raises:
        ValueError: When ``blend_w`` is outside ``[0.0, 1.0]`` — the label
            only describes a weight the scoring path could actually take.
    """
    if not 0.0 <= blend_w <= 1.0:
        raise ValueError(f"blend_w ({blend_w}) must be between 0.0 and 1.0")
    if blend_w == 1.0:
        return "centroid"
    if blend_w == 0.0:
        return "prototypes"
    return "blend"


def score_blend(
    question_vector: tuple[float, ...],
    profiles: Sequence[NamespaceProfile],
    *,
    blend_w: float,
) -> tuple[ScoredCandidate, ...]:
    """Score ``w * centroid + (1 - w) * max prototype`` per profile (T5).

    The router's single scoring path (S2 as amended by the ADR's T5/L5/L6
    decision): the measured default ``blend_w = 1.0`` is the centroid path,
    ``blend_w = 0.0`` is the prototype path, and values in between are the
    weighted sum. Both endpoints are reproduced **value for value**, not
    approximately:

    * ``blend_w == 1.0`` returns ``score_against_profiles``' scores with the
      same ordering, plus S7 provenance (the source is diagnostic, not
      causal — see ``ScoredCandidate``);
    * ``blend_w == 0.0`` returns ``score_against_prototypes`` **verbatim** —
      scores, ordering and ``best_prototype_source`` are bit for bit
      identical.

    The ranking is sorted by ``(-score, namespace.source_id)`` exactly like
    both component paths, so ties stay deterministic. ``best_prototype_source``
    is filled whenever the profile carries prototypes, including at
    ``w = 1.0``, so the geometry stays inspectable (S7/L5).

    Raises:
        ValueError: When ``blend_w`` is outside ``[0.0, 1.0]``, or when a
            profile carries no prototypes while the prototype term has weight
            (``blend_w < 1.0``).
    """
    if not 0.0 <= blend_w <= 1.0:
        raise ValueError(f"blend_w ({blend_w}) must be between 0.0 and 1.0")
    if blend_w == 0.0:
        # Centroid term has weight zero: reuse the prototype path verbatim so
        # the w=0.0 endpoint cannot drift from it, provenance included.
        return score_against_prototypes(question_vector, profiles)
    if blend_w == 1.0:
        # Prototype term has weight zero: score the centroid exactly like
        # score_against_profiles does, and attach provenance separately.
        centroid_only: list[ScoredCandidate] = []
        for profile in profiles:
            best_source: str | None = None
            if profile.prototypes:
                _prototype_score, best_source = _best_prototype_match(question_vector, profile)
            centroid_only.append(
                ScoredCandidate(
                    namespace=profile.namespace,
                    score=cosine_similarity(question_vector, profile.centroid),
                    best_prototype_source=best_source,
                )
            )
        centroid_only.sort(key=lambda candidate: (-candidate.score, candidate.namespace.source_id))
        return tuple(centroid_only)
    blended: list[ScoredCandidate] = []
    for profile in profiles:
        centroid_score = cosine_similarity(question_vector, profile.centroid)
        prototype_score, best_source = _best_prototype_match(question_vector, profile)
        blended.append(
            ScoredCandidate(
                namespace=profile.namespace,
                score=blend_w * centroid_score + (1.0 - blend_w) * prototype_score,
                best_prototype_source=best_source,
            )
        )
    blended.sort(key=lambda candidate: (-candidate.score, candidate.namespace.source_id))
    return tuple(blended)


def _ranked_margin(ordered: Sequence[ScoredCandidate]) -> float:
    """Return the gap between the best candidate and the runner-up.

    A single candidate has no runner-up, so its whole score is the margin; an
    empty ranking has none. Abstentions report this value instead of zero, so
    how close a question came to committing stays measurable for calibration.
    """
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0].score
    return ordered[0].score - ordered[1].score


def decide_route(
    ranked: Sequence[ScoredCandidate],
    thresholds: RouteThresholds,
    *,
    strategy: str = "unspecified",
    profile_k: int | None = None,
) -> RouteDecision:
    """Apply the abstention policy to ranked candidates.

    The decision kernel never fabricates a namespace: low score or low margin
    yields an explicit ``abstain``. Multi-scope fan-out is modeled at the
    application layer (Unit 3), not here.

    ``strategy`` and ``profile_k`` (S7) are provenance passthrough: the
    kernel does not choose them, it records what the scoring path actually
    used. The application layer passes ``blend_strategy``'s label
    (``"centroid"``/``"prototypes"``/``"blend"``) plus the ``k`` of the
    profile set it scored; a caller with no provenance to report leaves the
    defaults (``"unspecified"``, ``None``).
    """
    ordered = tuple(sorted(ranked, key=lambda c: (-c.score, c.namespace.source_id)))
    if not ordered:
        return _abstain(ordered, "no_profiles", strategy=strategy, profile_k=profile_k)

    top = ordered[0]
    margin = _ranked_margin(ordered)

    if top.score < thresholds.min_top_score:
        return _abstain(ordered, "low_score", strategy=strategy, profile_k=profile_k)
    if margin < thresholds.min_margin:
        return _abstain(ordered, "low_margin", strategy=strategy, profile_k=profile_k)

    return RouteDecision(
        selected=top.namespace,
        candidates=ordered,
        top_score=top.score,
        margin=margin,
        confidence=top.score,
        route_kind="single",
        reason="clear",
        strategy=strategy,
        profile_k=profile_k,
    )


def _abstain(
    ordered: Sequence[ScoredCandidate],
    reason: str,
    *,
    strategy: str = "unspecified",
    profile_k: int | None = None,
) -> RouteDecision:
    """Build an abstention decision from ordered candidates."""
    top_score = ordered[0].score if ordered else 0.0
    return RouteDecision(
        selected=None,
        candidates=tuple(ordered),
        top_score=top_score,
        margin=_ranked_margin(ordered),
        confidence=0.0,
        route_kind="abstain",
        reason=reason,
        strategy=strategy,
        profile_k=profile_k,
    )
