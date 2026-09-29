"""Behavior tests for the routing threshold calibrator."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from click.testing import CliRunner

from book_graph_rag.domain.namespaces import SourceNamespace
from book_graph_rag.domain.routing_metrics import RoutingLabel
from book_graph_rag.domain.routing_models import ScoredCandidate

_ROOT = Path(__file__).parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "calibrate_namespace_routing",
    _ROOT / "scripts/calibrate_namespace_routing.py",
)
assert _SPEC is not None
assert _SPEC.loader is not None
_MODULE: ModuleType = importlib.util.module_from_spec(_SPEC)
# Register under its module name so module-level dataclasses resolve.
sys.modules["calibrate_namespace_routing"] = _MODULE
_SPEC.loader.exec_module(_MODULE)

_sweep_thresholds = _MODULE.sweep_thresholds
_suggest_thresholds = _MODULE.suggest_thresholds
CalibrationPoint = _MODULE.CalibrationPoint


def _ns(namespace_id: str) -> SourceNamespace:
    corpus, source = namespace_id.split(":", maxsplit=1)
    return SourceNamespace(corpus=corpus, source=source)


def _label(
    question_id: str, kind: str = "single", expected: tuple[str, ...] = ("knowledge:a",)
) -> RoutingLabel:
    return RoutingLabel(
        question_id=question_id,
        question=f"q-{question_id}",
        language="en",
        route_kind=kind,
        expected_namespaces=expected,
    )


def test_calibrator_help_exits_zero() -> None:
    runner = CliRunner()

    result = runner.invoke(_MODULE.calibrate_namespace_routing, ["--help"])

    assert result.exit_code == 0
    assert "--max-wrong-rate" in result.output


def test_sweep_covers_grid_and_is_deterministic() -> None:
    labels = (_label("a"),)
    scores = {
        "a": (
            ScoredCandidate(namespace=_ns("knowledge:a"), score=0.70),
            ScoredCandidate(namespace=_ns("knowledge:b"), score=0.30),
        )
    }

    points = _sweep_thresholds(labels, scores)
    again = _sweep_thresholds(labels, scores)

    assert points == again
    assert len(points) == 84


def test_suggest_prefers_low_abstention_under_zero_wrong() -> None:
    points = (
        CalibrationPoint(
            min_top_score=0.30,
            min_margin=0.00,
            single_accuracy=1.0,
            wrong_namespace_rate=0.0,
            abstention_rate=0.5,
            multi_containment=1.0,
        ),
        CalibrationPoint(
            min_top_score=0.20,
            min_margin=0.00,
            single_accuracy=1.0,
            wrong_namespace_rate=0.0,
            abstention_rate=0.1,
            multi_containment=1.0,
        ),
        CalibrationPoint(
            min_top_score=0.20,
            min_margin=0.00,
            single_accuracy=1.0,
            wrong_namespace_rate=0.5,
            abstention_rate=0.0,
            multi_containment=1.0,
        ),
    )

    suggested = _suggest_thresholds(points)

    assert suggested is not None
    assert suggested.abstention_rate == 0.1


def test_suggest_returns_none_when_constraints_impossible() -> None:
    points = (
        CalibrationPoint(
            min_top_score=0.50,
            min_margin=0.10,
            single_accuracy=0.0,
            wrong_namespace_rate=0.1,
            abstention_rate=0.9,
            multi_containment=0.0,
        ),
    )

    assert _suggest_thresholds(points, max_wrong_rate=0.0) is None


def test_suggest_honors_min_accuracy() -> None:
    points = (
        CalibrationPoint(
            min_top_score=0.10,
            min_margin=0.0,
            single_accuracy=0.3,
            wrong_namespace_rate=0.0,
            abstention_rate=0.0,
            multi_containment=0.0,
        ),
        CalibrationPoint(
            min_top_score=0.60,
            min_margin=0.0,
            single_accuracy=0.8,
            wrong_namespace_rate=0.0,
            abstention_rate=0.6,
            multi_containment=0.0,
        ),
    )

    suggested = _suggest_thresholds(points, min_accuracy=0.5)

    assert suggested is not None
    assert suggested.single_accuracy == 0.8
