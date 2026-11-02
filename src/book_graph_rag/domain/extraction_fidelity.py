"""Pure, read-side extraction fidelity matching and metrics."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from book_graph_rag.domain.extraction_labels import (
    LabeledChunk,
    LabeledEntity,
    key_compact,
)
from book_graph_rag.domain.models import EntityType


@dataclass(frozen=True, slots=True)
class Prediction:
    """The entity fields attached to one chunk by the extractor."""

    id: str
    name: str
    canonical_name: str | None
    aliases: list[str]
    type: EntityType
    merged_into: str | None


class EntityLike(Protocol):
    """Fields copied by the pure domain-entity prediction adapter."""

    id: str
    name: str
    canonical_name: str | None
    aliases: list[str]
    type: EntityType


def predictions_from_entities(entities: Iterable[EntityLike]) -> list[Prediction]:
    """Adapt domain entities to predictions, preserving optional merge links."""
    predictions: list[Prediction] = []
    for entity in entities:
        merged_into = getattr(entity, "merged_into", None)
        if merged_into is not None and not isinstance(merged_into, str):
            raise TypeError("merged_into must be a string or None")
        predictions.append(
            Prediction(
                id=entity.id,
                name=entity.name,
                canonical_name=entity.canonical_name,
                aliases=list(entity.aliases),
                type=entity.type,
                merged_into=merged_into,
            )
        )
    return predictions


@dataclass(frozen=True, slots=True)
class FidelityMetrics:
    """Rates and raw counts for one pooled or per-book slice."""

    precision: float | None
    recall: float | None
    f1: float | None
    type_agreement: float | None
    labels: int
    predictions: int
    matched: int
    matching_types: int


@dataclass(frozen=True, slots=True)
class ExtractionFidelityResult(FidelityMetrics):
    """Headline pooled metrics, per-book results, and the high-confidence view."""

    per_book: dict[str, FidelityMetrics] = field(default_factory=dict)
    excluding_low_confidence: FidelityMetrics = field(
        default_factory=lambda: FidelityMetrics(None, None, None, None, 0, 0, 0, 0)
    )
    #: The headline pass's per-chunk pairing when ``include_detail=True``; ``None``
    #: when not asked for, so the default result is exactly today's, field for field.
    detail: tuple[ChunkFidelityDetail, ...] | None = None


# ── Opt-in per-chunk detail: the adjudication view of the SAME pass ───────────
#
# These records exist only when ``calculate_extraction_fidelity(include_detail=True)``
# is requested. They are the pairing the counts came from — never a second matching
# implementation — so the reconciliation (summed ``matched`` equals the counted
# ``matched``, unmatched predictions equal ``predictions - matched``) holds by
# construction rather than by agreement between two code paths.


@dataclass(frozen=True, slots=True)
class MatchedPairDetail:
    """One matched pair: the label and the COLLAPSED prediction it was paired with."""

    label_canonical: str
    #: The collapsed canonical identity the metric matched on (§4.3).
    prediction_canonical: str
    #: The group's representative raw prediction id, for looking the entity up.
    prediction_id: str
    #: The type axis of this pair (§4.5): right entity, wrong type ⇒ ``False``.
    type_matches: bool


@dataclass(frozen=True, slots=True)
class UnmatchedPredictionDetail:
    """One collapsed prediction no label absorbed, original fields intact (§4.2/§4.3)."""

    id: str
    name: str
    canonical_name: str | None
    aliases: tuple[str, ...]
    type: EntityType
    merged_into: str | None


@dataclass(frozen=True, slots=True)
class UnmatchedLabelDetail:
    """One label the pass did not pair, with the surfaces it was labeled under."""

    canonical: str
    type: EntityType
    surfaces: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ChunkFidelityDetail:
    """Everything one chunk's matching pass decided, in count-reconcilable form."""

    chunk_id: str
    matched: tuple[MatchedPairDetail, ...]
    unmatched_predictions: tuple[UnmatchedPredictionDetail, ...]
    unmatched_labels: tuple[UnmatchedLabelDetail, ...]


@dataclass(frozen=True, slots=True)
class _CollapsedPrediction:
    canonical_id: str
    surfaces: frozenset[str]
    #: The raw prediction the group is reported as (§4.3's representative): its
    #: identity, name, aliases and merge link stay visible in the detail records.
    representative: Prediction


@dataclass(frozen=True, slots=True)
class _Pair:
    """One assignment of the augmenting-path matching (§4.4), type axis included."""

    label_index: int
    prediction: _CollapsedPrediction
    type_matches: bool


@dataclass(slots=True)
class _Tally:
    labels: int = 0
    predictions: int = 0
    matched: int = 0
    matching_types: int = 0

    def freeze(self) -> FidelityMetrics:
        precision = self.matched / self.predictions if self.predictions else None
        recall = self.matched / self.labels if self.labels else None
        f1_denominator = self.labels + self.predictions
        f1 = 2 * self.matched / f1_denominator if f1_denominator else None
        type_agreement = self.matching_types / self.matched if self.matched else None
        return FidelityMetrics(
            precision=precision,
            recall=recall,
            f1=f1,
            type_agreement=type_agreement,
            labels=self.labels,
            predictions=self.predictions,
            matched=self.matched,
            matching_types=self.matching_types,
        )


def calculate_extraction_fidelity(
    labeled_chunks: Sequence[LabeledChunk],
    predictions_by_chunk: Mapping[str, Sequence[Prediction]],
    canonical_of: Mapping[str, str],
    *,
    include_detail: bool = False,
) -> ExtractionFidelityResult:
    """Compute pooled and per-book fidelity without reading or mutating a graph.

    ``include_detail=True`` additionally returns the headline pass's per-chunk
    pairing as ``detail``. The counts come from that SAME pass — there is no second
    matching implementation that could disagree with them — and the default (``False``)
    reproduces today's result exactly, field for field.
    """
    chunk_ids = [chunk.chunk_id for chunk in labeled_chunks]
    if len(set(chunk_ids)) != len(chunk_ids):
        raise ValueError("labeled_chunks must not contain duplicate chunk_id values")

    headline, per_book, detail = _measure(
        labeled_chunks,
        predictions_by_chunk,
        canonical_of,
        include_low_confidence=True,
        include_detail=include_detail,
    )
    stable, _, _ = _measure(
        labeled_chunks,
        predictions_by_chunk,
        canonical_of,
        include_low_confidence=False,
        # The stability view never carries detail: the adjudication view describes
        # the headline measurement, the one the report prints.
    )
    return ExtractionFidelityResult(
        precision=headline.precision,
        recall=headline.recall,
        f1=headline.f1,
        type_agreement=headline.type_agreement,
        labels=headline.labels,
        predictions=headline.predictions,
        matched=headline.matched,
        matching_types=headline.matching_types,
        per_book=per_book,
        excluding_low_confidence=stable,
        detail=detail,
    )


def _measure(
    labeled_chunks: Sequence[LabeledChunk],
    predictions_by_chunk: Mapping[str, Sequence[Prediction]],
    canonical_of: Mapping[str, str],
    *,
    include_low_confidence: bool,
    include_detail: bool = False,
) -> tuple[
    FidelityMetrics,
    dict[str, FidelityMetrics],
    tuple[ChunkFidelityDetail, ...] | None,
]:
    pooled = _Tally()
    by_book: dict[str, _Tally] = {}
    details: list[ChunkFidelityDetail] | None = [] if include_detail else None
    for chunk in labeled_chunks:
        labels = [
            entity
            for entity in chunk.entities
            if include_low_confidence or entity.confidence == "high"
        ]
        predictions = _collapse_predictions(
            predictions_by_chunk.get(chunk.chunk_id, ()), canonical_of
        )
        pairs, matching_types = _match(labels, predictions)

        for tally in (pooled, by_book.setdefault(chunk.book_id, _Tally())):
            tally.labels += len(labels)
            tally.predictions += len(predictions)
            tally.matched += len(pairs)
            tally.matching_types += matching_types

        if details is not None:
            # The records are a view of THIS chunk's pass: same labels, same
            # collapsed predictions, same pairs that just fed the tallies.
            details.append(_chunk_detail(chunk.chunk_id, labels, predictions, pairs))

    detail = None if details is None else tuple(details)
    return (
        pooled.freeze(),
        {book_id: tally.freeze() for book_id, tally in by_book.items()},
        detail,
    )


def _collapse_predictions(
    predictions: Sequence[Prediction], canonical_of: Mapping[str, str]
) -> list[_CollapsedPrediction]:
    """Group predictions by canonical identity (§4.3) and pick each group's representative.

    The representative is the canonical member when the group contains it, so an
    UNMATCHED group holding both a merge loser and its canonical target is reported
    as the canonical member: §4.3 counts the group once, and the loser's
    ``merged_into`` is therefore not shown for that group — it is shown when the
    loser is the group's only member (its target absent from the chunk).
    """
    grouped: dict[str, list[Prediction]] = {}
    for prediction in predictions:
        canonical_id = _resolve_canonical_id(prediction, canonical_of)
        grouped.setdefault(canonical_id, []).append(prediction)

    collapsed: list[_CollapsedPrediction] = []
    for canonical_id, group in sorted(grouped.items()):
        surfaces = {
            key_compact(surface)
            for prediction in group
            for surface in (
                prediction.name,
                *([prediction.canonical_name] if prediction.canonical_name else []),
                *prediction.aliases,
            )
        }
        surfaces.discard("")
        representative = min(
            group,
            key=lambda prediction: (prediction.id != canonical_id, prediction.id),
        )
        collapsed.append(
            _CollapsedPrediction(
                canonical_id=canonical_id,
                surfaces=frozenset(surfaces),
                representative=representative,
            )
        )
    return collapsed


def _resolve_canonical_id(prediction: Prediction, canonical_of: Mapping[str, str]) -> str:
    current_id = prediction.merged_into or prediction.id
    visited: set[str] = set()
    while current_id in canonical_of:
        if current_id in visited:
            raise ValueError(f"cycle in canonical_of merge map at {current_id!r}")
        visited.add(current_id)
        next_id = canonical_of[current_id]
        if next_id == current_id:
            return current_id
        current_id = next_id
    return current_id


def _match(
    labels: Sequence[LabeledEntity], predictions: Sequence[_CollapsedPrediction]
) -> tuple[list[_Pair], int]:
    """Return the maximum-cardinality one-to-one pairing, then its type agreements.

    The pairing AND ``matching_types`` come out of ONE walk over ``label_match`` —
    the dict that ``len`` counts — so a detail record and the counted number can
    never describe different matches.
    """
    label_surfaces = [{key_compact(surface) for surface in label.surfaces} for label in labels]
    candidates: list[list[tuple[int, _CollapsedPrediction]]] = []
    for surfaces in label_surfaces:
        options = [
            (len(surfaces & prediction.surfaces), prediction)
            for prediction in predictions
            if surfaces & prediction.surfaces
        ]
        options.sort(key=lambda item: (-item[0], item[1].canonical_id))
        candidates.append(options)

    prediction_owner: dict[str, int] = {}
    label_match: dict[int, _CollapsedPrediction] = {}

    def assign(label_index: int, visited: set[str]) -> bool:
        for _, prediction in candidates[label_index]:
            if prediction.canonical_id in visited:
                continue
            visited.add(prediction.canonical_id)
            owner = prediction_owner.get(prediction.canonical_id)
            if owner is None or assign(owner, visited):
                prediction_owner[prediction.canonical_id] = label_index
                label_match[label_index] = prediction
                return True
        return False

    label_order = sorted(
        range(len(labels)),
        key=lambda index: (len(candidates[index]), index),
    )
    for label_index in label_order:
        assign(label_index, set())

    pairs: list[_Pair] = []
    matching_types = 0
    for label_index in sorted(label_match):
        prediction = label_match[label_index]
        type_matches = labels[label_index].type == prediction.representative.type
        pairs.append(
            _Pair(label_index=label_index, prediction=prediction, type_matches=type_matches)
        )
        matching_types += type_matches
    return pairs, matching_types


def _chunk_detail(
    chunk_id: str,
    labels: Sequence[LabeledEntity],
    predictions: Sequence[_CollapsedPrediction],
    pairs: Sequence[_Pair],
) -> ChunkFidelityDetail:
    """Slice one chunk's already-computed pass into records. No matching happens here."""
    paired_labels = {pair.label_index for pair in pairs}
    paired_predictions = {pair.prediction.canonical_id for pair in pairs}
    return ChunkFidelityDetail(
        chunk_id=chunk_id,
        matched=tuple(
            MatchedPairDetail(
                label_canonical=labels[pair.label_index].canonical,
                prediction_canonical=pair.prediction.canonical_id,
                prediction_id=pair.prediction.representative.id,
                type_matches=pair.type_matches,
            )
            for pair in pairs
        ),
        unmatched_predictions=tuple(
            UnmatchedPredictionDetail(
                id=prediction.representative.id,
                name=prediction.representative.name,
                canonical_name=prediction.representative.canonical_name,
                aliases=tuple(prediction.representative.aliases),
                type=prediction.representative.type,
                merged_into=prediction.representative.merged_into,
            )
            for prediction in predictions
            if prediction.canonical_id not in paired_predictions
        ),
        unmatched_labels=tuple(
            UnmatchedLabelDetail(
                canonical=label.canonical,
                type=label.type,
                surfaces=tuple(label.surfaces),
            )
            for index, label in enumerate(labels)
            if index not in paired_labels
        ),
    )
