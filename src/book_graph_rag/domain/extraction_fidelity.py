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


@dataclass(frozen=True, slots=True)
class _CollapsedPrediction:
    canonical_id: str
    surfaces: frozenset[str]
    type: EntityType


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
) -> ExtractionFidelityResult:
    """Compute pooled and per-book fidelity without reading or mutating a graph."""
    chunk_ids = [chunk.chunk_id for chunk in labeled_chunks]
    if len(set(chunk_ids)) != len(chunk_ids):
        raise ValueError("labeled_chunks must not contain duplicate chunk_id values")

    headline, per_book = _measure(
        labeled_chunks,
        predictions_by_chunk,
        canonical_of,
        include_low_confidence=True,
    )
    stable, _ = _measure(
        labeled_chunks,
        predictions_by_chunk,
        canonical_of,
        include_low_confidence=False,
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
    )


def _measure(
    labeled_chunks: Sequence[LabeledChunk],
    predictions_by_chunk: Mapping[str, Sequence[Prediction]],
    canonical_of: Mapping[str, str],
    *,
    include_low_confidence: bool,
) -> tuple[FidelityMetrics, dict[str, FidelityMetrics]]:
    pooled = _Tally()
    by_book: dict[str, _Tally] = {}
    for chunk in labeled_chunks:
        labels = [
            entity
            for entity in chunk.entities
            if include_low_confidence or entity.confidence == "high"
        ]
        predictions = _collapse_predictions(
            predictions_by_chunk.get(chunk.chunk_id, ()), canonical_of
        )
        matched, matching_types = _match(labels, predictions)

        for tally in (pooled, by_book.setdefault(chunk.book_id, _Tally())):
            tally.labels += len(labels)
            tally.predictions += len(predictions)
            tally.matched += matched
            tally.matching_types += matching_types

    return pooled.freeze(), {book_id: tally.freeze() for book_id, tally in by_book.items()}


def _collapse_predictions(
    predictions: Sequence[Prediction], canonical_of: Mapping[str, str]
) -> list[_CollapsedPrediction]:
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
                type=representative.type,
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
) -> tuple[int, int]:
    """Return a maximum-cardinality one-to-one match, then its type agreements."""
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

    matching_types = sum(
        labels[label_index].type == prediction.type
        for label_index, prediction in label_match.items()
    )
    return len(label_match), matching_types
