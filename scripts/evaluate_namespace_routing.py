"""Evaluate the namespace router against the committed routing dataset.

Runs the production router (catalog scope resolver, local profile artifact,
local embeddings) over every labeled question and emits deterministic routing
metrics. Read-only: it never mutates Neo4j or production and keeps routing
metrics separate from retrieval/generation metrics.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import click

from book_graph_rag.application.route_question_use_case import RouteQuestionUseCase
from book_graph_rag.application.routing_evaluator import evaluate_router
from book_graph_rag.config import Settings
from book_graph_rag.domain.routing_metrics import (
    EvaluationPrediction,
    RoutingLabel,
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
from scripts.route_question import _catalog_hints


def _load_labels(dataset_path: Path) -> tuple[RoutingLabel, ...]:
    """Parse the committed routing dataset JSONL into labels."""
    labels: list[RoutingLabel] = []
    for line in dataset_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        import json as _json

        record = _json.loads(line)
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


async def _predict_all(
    use_case: RouteQuestionUseCase,
    labels: tuple[RoutingLabel, ...],
) -> dict[str, EvaluationPrediction]:
    predictions: dict[str, EvaluationPrediction] = {}
    for label in labels:
        resolved = await use_case.execute(label.question)
        predictions[label.question_id] = EvaluationPrediction(
            question_id=label.question_id,
            validated_namespace=(
                resolved.validated_namespace.source_id
                if resolved.validated_namespace is not None
                else None
            ),
            fanout_namespaces=tuple(ns.source_id for ns in resolved.fanout_namespaces),
        )
    return predictions


@click.command()
@click.option(
    "--dataset",
    type=Path,
    default=Path("data/evaluation/namespace_routing_dataset.jsonl"),
)
@click.option("--json-only", is_flag=True, default=False, help="Emit metrics as JSON only.")
def evaluate_namespace_routing(
    dataset: Path,
    json_only: bool,
) -> None:
    """Compute routing metrics over the committed dataset (read-only)."""
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
        lexical_hints=_catalog_hints(settings),
    )

    try:
        predictions = asyncio.run(_predict_all(use_case, labels))
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Evaluation error: {exc}", err=True)
        sys.exit(2)

    metrics = evaluate_router(
        labels,
        lambda label: predictions[label.question_id],
    )
    payload = metrics.model_dump(mode="json")
    if not json_only:
        click.echo(
            f"routing: single_accuracy={metrics.single_accuracy:.3f} "
            f"wrong_rate={metrics.wrong_namespace_rate:.3f} "
            f"abstention={metrics.abstention_rate:.3f} "
            f"multi_containment={metrics.multi_containment:.3f} "
            f"({metrics.total} labels)"
        )
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    evaluate_namespace_routing()
