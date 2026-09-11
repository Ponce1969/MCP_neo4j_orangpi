"""Tests for JsonEvaluationBaselineLoader (Slice A)."""

from __future__ import annotations

from pathlib import Path

import pytest

from book_graph_rag.domain.evaluation_models import EvaluationBaselineReport
from book_graph_rag.infrastructure.evaluation_baseline_loader import (
    JsonEvaluationBaselineLoader,
)


def test_missing_file_returns_none(tmp_path: Path) -> None:
    """A missing baseline file signals INCOMPLETE via None."""
    loader = JsonEvaluationBaselineLoader(tmp_path)
    assert loader.load("resolution") is None


def test_unfinalized_baseline_loads_without_thresholds(tmp_path: Path) -> None:
    """An unfinalized baseline does not require numeric thresholds."""
    baseline = EvaluationBaselineReport(
        layer="resolution",
        dataset_id="r",
        dataset_sha256="0" * 64,
        thresholds_finalized=False,
    )
    path = tmp_path / "resolution_baseline.json"
    path.write_text(baseline.model_dump_json(), encoding="utf-8")
    loader = JsonEvaluationBaselineLoader(tmp_path)
    loaded = loader.load("resolution")
    assert loaded is not None
    assert loaded.thresholds_finalized is False
    assert loaded.f1_min is None


def test_finalized_resolution_requires_f1_min(tmp_path: Path) -> None:
    """A finalized resolution baseline must have f1_min and hard_over_merge_max."""
    bad = {
        "schema_version": "1.0.0",
        "layer": "resolution",
        "dataset_id": "r",
        "dataset_sha256": "0" * 64,
        "thresholds_finalized": True,
    }
    path = tmp_path / "resolution_baseline.json"
    path.write_text(__import__("json").dumps(bad), encoding="utf-8")
    loader = JsonEvaluationBaselineLoader(tmp_path)
    with pytest.raises(ValueError, match="requires"):
        loader.load("resolution")


def test_finalized_generation_requires_faithfulness_min(tmp_path: Path) -> None:
    """A finalized generation baseline must have faithfulness_min."""
    bad = {
        "schema_version": "1.0.0",
        "layer": "generation",
        "dataset_id": "g",
        "dataset_sha256": "0" * 64,
        "thresholds_finalized": True,
    }
    path = tmp_path / "generation_baseline.json"
    path.write_text(__import__("json").dumps(bad), encoding="utf-8")
    loader = JsonEvaluationBaselineLoader(tmp_path)
    with pytest.raises(ValueError, match="requires"):
        loader.load("generation")


def test_layer_path_mapping(tmp_path: Path) -> None:
    """Each layer maps to the expected filename."""
    (tmp_path / "resolution_baseline.json").write_text(
        EvaluationBaselineReport(
            layer="resolution", dataset_id="x", dataset_sha256="0" * 64
        ).model_dump_json(),
        encoding="utf-8",
    )
    (tmp_path / "generation_baseline.json").write_text(
        EvaluationBaselineReport(
            layer="generation", dataset_id="x", dataset_sha256="0" * 64
        ).model_dump_json(),
        encoding="utf-8",
    )
    loader = JsonEvaluationBaselineLoader(tmp_path)
    assert loader.load("resolution") is not None
    assert loader.load("generation") is not None

def test_generation_baseline_unfinalized_loads() -> None:
    """The committed generation baseline loads with thresholds_finalized=false."""
    loader = JsonEvaluationBaselineLoader(Path("data/evaluation"))
    baseline = loader.load("generation")
    assert baseline is not None
    assert baseline.layer == "generation"
    assert baseline.dataset_id == "generation_dataset"
    assert baseline.thresholds_finalized is False


def test_generation_baseline_metrics_shape() -> None:
    """The committed generation baseline carries the real RAGAS metric names."""
    loader = JsonEvaluationBaselineLoader(Path("data/evaluation"))
    baseline = loader.load("generation")
    assert baseline is not None
    assert "faithfulness" in baseline.metrics
    assert "answer_relevancy" in baseline.metrics
    assert "context_precision" in baseline.metrics
    assert baseline.metrics["faithfulness"] == pytest.approx(0.6825)
    assert baseline.metrics["answer_relevancy"] == pytest.approx(0.5953)
    assert baseline.metrics["context_precision"] == pytest.approx(0.474)
