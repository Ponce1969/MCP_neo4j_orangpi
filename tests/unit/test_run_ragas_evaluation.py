"""Tests for scripts/run_ragas_evaluation.py helpers."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

import pytest
from run_ragas_evaluation import (  # noqa: E402
    _compute_deltas,
    _load_baseline,
)


def test_load_baseline_raises_when_missing(tmp_path: Path) -> None:
    """_load_baseline raises FileNotFoundError when the baseline does not exist."""
    missing = tmp_path / "gr3_baseline.json"

    with pytest.raises(FileNotFoundError):
        _load_baseline(missing)


def test_load_baseline_loads_existing_file(tmp_path: Path) -> None:
    """_load_baseline reads and parses the baseline JSON."""
    baseline_path = tmp_path / "gr3_baseline.json"
    data = {"metrics": {"faithfulness": 0.5}}
    baseline_path.write_text(json.dumps(data), encoding="utf-8")

    loaded = _load_baseline(baseline_path)

    assert loaded == data


def test_compute_deltas_returns_per_metric_diff() -> None:
    """_compute_deltas subtracts baseline from current for each metric."""
    current = {"metrics": {"faithfulness": 0.6, "answer_relevancy": 0.7}}
    baseline = {"metrics": {"faithfulness": 0.5, "answer_relevancy": 0.75}}

    deltas = _compute_deltas(current, baseline)

    assert deltas == {"faithfulness": 0.1, "answer_relevancy": -0.05}


def test_compute_deltas_returns_none_for_missing_baseline_metric() -> None:
    """_compute_deltas returns None when a metric has no baseline value."""
    current = {"metrics": {"new_metric": 0.8}}
    baseline: dict[str, dict[str, float]] = {"metrics": {}}

    deltas = _compute_deltas(current, baseline)

    assert deltas == {"new_metric": None}
