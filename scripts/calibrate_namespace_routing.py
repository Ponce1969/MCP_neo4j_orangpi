"""Sweep routing thresholds over the committed dataset to recommend values.

Uses the scoring stage exactly once per question (lexical hints, then local
embeddings) and then sweeps ``RouteThresholds`` in memory with the pure
decision kernel, so calibration is deterministic and cheap. Read-only: it
never mutates the graph and writes no artifact by default.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import click

from book_graph_rag.application.route_question_use_case import RouteQuestionUseCase
from book_graph_rag.application.routing_evaluator import evaluate_router
from book_graph_rag.config import Settings
from book_graph_rag.domain.routing_metrics import (
    EvaluationPrediction,
    RoutingLabel,
)
from book_graph_rag.domain.routing_models import (
    RouteThresholds,
    ScoredCandidate,
    decide_route,
    hints_from_catalog,
)
from book_graph_rag.infrastructure.catalog_loader import CatalogLoader
from book_graph_rag.infrastructure.catalog_scope_resolver import CatalogScopeResolver
from book_graph_rag.infrastructure.json_namespace_profile_reader import (
    JsonNamespaceProfileReader,
)
from book_graph_rag.infrastructure.json_namespace_profile_store import (
    JsonNamespaceProfileStore,
)
from book_graph_rag.infrastructure.sentence_transformer_adapter import (
    SentenceTransformerAdapter,
)

_TOP_SCORE_STEP = 0.05
_TOP_SCORE_RANGE = (0.10, 0.75)
_MARGIN_STEP = 0.05
_MARGIN_RANGE = (0.00, 0.25)


@dataclass(frozen=True)
class CalibrationPoint:
    """One metric row for a threshold combination."""

    min_top_score: float
    min_margin: float
    single_accuracy: float
    wrong_namespace_rate: float
    abstention_rate: float
    multi_containment: float


def _predict(
    ranked: tuple[ScoredCandidate, ...],
    thresholds: RouteThresholds,
    label: RoutingLabel,
) -> EvaluationPrediction:
    """Predict from precomputed scores using the threshold combination."""
    decision = decide_route(ranked, thresholds)
    if label.route_kind == "multi":
        if decision.reason == "low_margin":
            fanout = tuple(candidate.namespace.source_id for candidate in decision.candidates[:2])
        else:
            fanout = ()
        return EvaluationPrediction(question_id=label.question_id, fanout_namespaces=fanout)
    validated = decision.selected.source_id if decision.selected is not None else None
    return EvaluationPrediction(
        question_id=label.question_id,
        validated_namespace=validated,
    )


def _predictor(
    scores_by_id: dict[str, tuple[ScoredCandidate, ...]],
    thresholds: RouteThresholds,
) -> Callable[[RoutingLabel], EvaluationPrediction]:
    """Build a typed predict function bound to one threshold combination."""

    def predict(label: RoutingLabel) -> EvaluationPrediction:
        return _predict(scores_by_id[label.question_id], thresholds, label)

    return predict


def sweep_thresholds(
    labels: tuple[RoutingLabel, ...],
    scores_by_id: dict[str, tuple[ScoredCandidate, ...]],
) -> tuple[CalibrationPoint, ...]:
    """Compute one metric row per threshold combination (deterministic)."""
    points: list[CalibrationPoint] = []
    top = _TOP_SCORE_RANGE[0]
    while top <= _TOP_SCORE_RANGE[1] + 1e-9:
        margin = _MARGIN_RANGE[0]
        while margin <= _MARGIN_RANGE[1] + 1e-9:
            thresholds = RouteThresholds(
                min_top_score=round(top, 2),
                min_margin=round(margin, 2),
            )
            metrics = evaluate_router(labels, _predictor(scores_by_id, thresholds))
            points.append(
                CalibrationPoint(
                    min_top_score=round(top, 2),
                    min_margin=round(margin, 2),
                    single_accuracy=metrics.single_accuracy,
                    wrong_namespace_rate=metrics.wrong_namespace_rate,
                    abstention_rate=metrics.abstention_rate,
                    multi_containment=metrics.multi_containment,
                )
            )
            margin += _MARGIN_STEP
        top += _TOP_SCORE_STEP
    return tuple(points)


def suggest_thresholds(
    points: tuple[CalibrationPoint, ...],
    *,
    max_wrong_rate: float = 0.0,
    min_accuracy: float = 0.0,
) -> CalibrationPoint | None:
    """Pick the point with lowest abstention, bounded by constraints."""
    eligible = [
        point
        for point in points
        if point.wrong_namespace_rate <= max_wrong_rate and point.single_accuracy >= min_accuracy
    ]
    if not eligible:
        return None
    return min(eligible, key=lambda point: (point.abstention_rate, point.min_top_score))


def _load_labels(dataset_path: Path) -> tuple[RoutingLabel, ...]:
    labels: list[RoutingLabel] = []
    for line in dataset_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        labels.append(
            RoutingLabel(
                question_id=record["question_id"],
                question=record["question"],
                language=record["language"],
                route_kind=record["route_kind"],
                expected_namespaces=tuple(record["expected_namespaces"]),
                ambiguity=record.get("ambiguity", ""),
                provenance=record.get("provenance", ""),
            )
        )
    return tuple(labels)


async def _score_all(
    use_case: RouteQuestionUseCase,
    labels: tuple[RoutingLabel, ...],
) -> dict[str, tuple[ScoredCandidate, ...]]:
    return {label.question_id: await use_case.score_question(label.question) for label in labels}


@click.command()
@click.option(
    "--dataset",
    type=Path,
    default=Path("data/evaluation/namespace_routing_dataset.jsonl"),
)
@click.option("--max-wrong-rate", type=float, default=0.0)
@click.option("--min-accuracy", type=float, default=0.0)
@click.option("--json-only", is_flag=True, default=False)
def calibrate_namespace_routing(
    dataset: Path,
    max_wrong_rate: float,
    min_accuracy: float,
    json_only: bool,
) -> None:
    """Sweep thresholds and recommend the tightest abstention point."""
    try:
        settings = Settings.model_validate({})
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Configuration error: {exc}", err=True)
        sys.exit(1)

    labels = _load_labels(dataset)
    use_case = RouteQuestionUseCase(
        SentenceTransformerAdapter(settings),
        JsonNamespaceProfileReader(
            JsonNamespaceProfileStore(settings.namespace_profile_store_path)
        ),
        CatalogScopeResolver(CatalogLoader(settings.catalog_path)),
        model_id=settings.embedding_model_id,
        lexical_hints=hints_from_catalog(CatalogLoader(settings.catalog_path).load()),
    )

    try:
        scores = asyncio.run(_score_all(use_case, labels))
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Calibration error: {exc}", err=True)
        sys.exit(2)

    points = sweep_thresholds(labels, scores)
    suggested = suggest_thresholds(points, max_wrong_rate=max_wrong_rate, min_accuracy=min_accuracy)

    if not json_only:
        click.echo(f"swept {len(points)} threshold combinations over {len(labels)} labels")
        if suggested is not None:
            click.echo(
                f"suggested: min_top_score={suggested.min_top_score:.2f} "
                f"min_margin={suggested.min_margin:.2f} "
                f"accuracy={suggested.single_accuracy:.3f} "
                f"wrong={suggested.wrong_namespace_rate:.3f} "
                f"abstention={suggested.abstention_rate:.3f} "
                f"multi_containment={suggested.multi_containment:.3f}"
            )
        else:
            click.echo("no threshold combination meets the constraints", err=True)
    click.echo(
        json.dumps(
            {
                "suggested": (
                    None
                    if suggested is None
                    else {
                        "min_top_score": suggested.min_top_score,
                        "min_margin": suggested.min_margin,
                        "single_accuracy": suggested.single_accuracy,
                        "wrong_namespace_rate": suggested.wrong_namespace_rate,
                        "abstention_rate": suggested.abstention_rate,
                        "multi_containment": suggested.multi_containment,
                    }
                ),
                "points": [
                    {
                        "min_top_score": point.min_top_score,
                        "min_margin": point.min_margin,
                        "single_accuracy": point.single_accuracy,
                        "wrong_namespace_rate": point.wrong_namespace_rate,
                        "abstention_rate": point.abstention_rate,
                        "multi_containment": point.multi_containment,
                    }
                    for point in points
                ],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    calibrate_namespace_routing()
