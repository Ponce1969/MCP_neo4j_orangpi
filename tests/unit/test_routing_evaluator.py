"""Behavior tests for the deterministic routing evaluator."""

from __future__ import annotations

from book_graph_rag.application.routing_evaluator import evaluate_router
from book_graph_rag.domain.routing_metrics import (
    EvaluationPrediction,
    RoutingLabel,
)


def _label(
    question_id: str,
    *,
    route_kind: str,
    expected: tuple[str, ...],
) -> RoutingLabel:
    return RoutingLabel(
        question_id=question_id,
        question=f"q-{question_id}",
        language="en",
        route_kind=route_kind,
        expected_namespaces=expected,
    )


def test_single_accuracy_and_abstention() -> None:
    labels = (
        _label("s1", route_kind="single", expected=("knowledge:a",)),
        _label("s2", route_kind="single", expected=("knowledge:a",)),
        _label("s3", route_kind="single", expected=("knowledge:b",)),
    )

    def predict(label: RoutingLabel) -> EvaluationPrediction:
        namespace = {"s1": "knowledge:a", "s2": None, "s3": "knowledge:c"}[label.question_id]
        return EvaluationPrediction(question_id=label.question_id, validated_namespace=namespace)

    metrics = evaluate_router(labels, predict)

    assert metrics.total == 3
    assert metrics.single_count == 3
    assert metrics.single_accuracy == 1 / 3
    assert metrics.wrong_namespace_rate == 1 / 3
    assert metrics.abstained_count == 1
    assert metrics.abstention_rate == 1 / 3


def test_multi_containment_requires_all_expected() -> None:
    labels = (
        _label(
            "m1",
            route_kind="multi",
            expected=("knowledge:a", "knowledge:b"),
        ),
        _label(
            "m2",
            route_kind="multi",
            expected=("knowledge:a", "knowledge:b"),
        ),
    )

    def predict(label: RoutingLabel) -> EvaluationPrediction:
        fanout = (
            ("knowledge:a", "knowledge:b")
            if label.question_id == "m1"
            else ("knowledge:a", "knowledge:c")
        )
        return EvaluationPrediction(question_id=label.question_id, fanout_namespaces=fanout)

    metrics = evaluate_router(labels, predict)

    assert metrics.multi_count == 2
    assert metrics.multi_containment == 0.5


def test_empty_dataset_returns_zero_metrics() -> None:
    metrics = evaluate_router((), lambda label: EvaluationPrediction(question_id=label.question_id))

    assert metrics.total == 0
    assert metrics.single_accuracy == 0.0
    assert metrics.multi_containment == 0.0
