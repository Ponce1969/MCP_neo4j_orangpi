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


def test_retrieval_baseline_loads_with_precision_threshold(tmp_path: Path) -> None:
    """A finalized retrieval baseline loads with its precision_at_k threshold."""
    baseline = EvaluationBaselineReport(
        layer="retrieval",
        dataset_id="retrieval_dataset",
        dataset_sha256="0" * 64,
        thresholds_finalized=True,
        precision_at_k_min=0.45,
    )
    path = tmp_path / "retrieval_baseline.json"
    path.write_text(baseline.model_dump_json(), encoding="utf-8")
    loader = JsonEvaluationBaselineLoader(tmp_path)
    loaded = loader.load("retrieval")
    assert loaded is not None
    assert loaded.thresholds_finalized is True
    assert loaded.precision_at_k_min == 0.45


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


def test_generation_baseline_finalized_loads() -> None:
    """The committed generation baseline loads with finalized thresholds."""
    loader = JsonEvaluationBaselineLoader(Path("data/evaluation"))
    baseline = loader.load("generation")
    assert baseline is not None
    assert baseline.layer == "generation"
    assert baseline.dataset_id == "generation_dataset"
    assert baseline.thresholds_finalized is True


def test_generation_baseline_metrics_shape() -> None:
    """The committed generation baseline carries the metric the gate measures.

    It no longer carries the RAGAS names: that feed is empty by construction in
    this layer (``odd/backlog.md``, E1), so the artifact records the project-owned
    claims faithfulness of the measured run and names the instrument that
    produced it. The threshold is calibrated against the **observed** spread of that
    instrument, not an assumed one: ``0.85`` (derived from a binomial standard error)
    failed on the next gate run, and ``0.80`` sits below both observations on record
    (``0.8363`` and ``0.8988``, mean ``0.868``).
    """
    loader = JsonEvaluationBaselineLoader(Path("data/evaluation"))
    baseline = loader.load("generation")
    assert baseline is not None
    assert set(baseline.metrics) == {"faithfulness"}
    assert baseline.metrics["faithfulness"] == pytest.approx(0.8988195615514334)
    assert baseline.model_ids == ("deepseek-v4-flash",)
    assert baseline.faithfulness_min == pytest.approx(0.80)
    faithfulness_min = baseline.faithfulness_min
    assert faithfulness_min is not None
    assert baseline.metrics["faithfulness"] >= faithfulness_min


def test_retrieval_baseline_reflects_the_shipping_configuration() -> None:
    """The committed retrieval baseline pins the measurement and its threshold.

    The reranker is off in production, so the recorded precision@k is the plain
    fulltext order over the over-fetched pool with the chunk-only denominator.
    """
    loader = JsonEvaluationBaselineLoader(Path("data/evaluation"))
    baseline = loader.load("retrieval")
    assert baseline is not None
    assert baseline.metrics["precision_at_k"] == pytest.approx(0.125)
    assert baseline.precision_at_k_min == pytest.approx(0.1175)
    precision_at_k_min = baseline.precision_at_k_min
    assert precision_at_k_min is not None
    assert baseline.metrics["precision_at_k"] >= precision_at_k_min
