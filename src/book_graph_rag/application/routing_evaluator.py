"""Deterministic routing evaluator over the labeled routing dataset."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from book_graph_rag.domain.routing_metrics import (
    EvaluationPrediction,
    RoutingLabel,
    RoutingMetrics,
)


def evaluate_router(
    labels: Sequence[RoutingLabel],
    predict: Callable[[RoutingLabel], EvaluationPrediction],
) -> RoutingMetrics:
    """Compute routing metrics from labeled expectations and predictions.

    - single accuracy: correct single-label routing over all single labels;
    - wrong-namespace rate: single labels whose validated namespace differs;
    - abstention rate: predictions with no validated namespace;
    - multi containment: multi labels whose expected namespaces are contained
      in the fan-out.
    """
    total = len(labels)
    single_labels = [label for label in labels if label.route_kind == "single"]
    multi_labels = [label for label in labels if label.route_kind == "multi"]

    correct_single = 0
    wrong_single = 0
    for label in single_labels:
        prediction = predict(label)
        if prediction.validated_namespace == label.expected_namespaces[0]:
            correct_single += 1
        elif prediction.validated_namespace is not None:
            # Only resolved-but-wrong routes count as wrong; abstentions are
            # covered by abstention_rate instead.
            wrong_single += 1

    abstained = sum(1 for label in labels if predict(label).validated_namespace is None)

    contained = sum(
        1
        for label in multi_labels
        if set(label.expected_namespaces) <= set(predict(label).fanout_namespaces)
    )

    return RoutingMetrics(
        total=total,
        single_count=len(single_labels),
        multi_count=len(multi_labels),
        abstained_count=abstained,
        single_accuracy=(correct_single / len(single_labels) if single_labels else 0.0),
        wrong_namespace_rate=(wrong_single / len(single_labels) if single_labels else 0.0),
        abstention_rate=abstained / total if total else 0.0,
        multi_containment=contained / len(multi_labels) if multi_labels else 0.0,
    )
