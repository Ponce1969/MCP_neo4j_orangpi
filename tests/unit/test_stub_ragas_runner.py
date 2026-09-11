"""Tests for StubRAGASRunner and SubprocessRAGASRunner (Slice B, T-B.7)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from book_graph_rag.domain.evaluation_models import RAGASSecondaryMetrics
from book_graph_rag.infrastructure.stub_ragas_runner import StubRAGASRunner
from book_graph_rag.infrastructure.subprocess_ragas_runner import SubprocessRAGASRunner


@pytest.fixture
def fixture_path() -> Path:
    return Path("tests/fixtures/evaluation/ragas.json")


@pytest.fixture
def runner(fixture_path: Path) -> StubRAGASRunner:
    return StubRAGASRunner(fixture_path)


def test_stub_ragas_runner_unavailable_returns_warning(
    runner: StubRAGASRunner,
) -> None:
    """Fixture available=False yields a warning metric, not an exception."""
    metrics = asyncio.run(runner.run(
        dataset_id="unavailable_dataset",
        generation_results=(),
        previous_metrics=None,
    ))
    assert metrics.available is False
    assert "import failed" in metrics.notes


def test_stub_ragas_runner_drop_warning_computed(
    runner: StubRAGASRunner,
) -> None:
    """A sharp faithfulness drop vs previous_metrics surfaces drop_warning."""
    previous = RAGASSecondaryMetrics(faithfulness=0.6825, available=True)
    metrics = asyncio.run(runner.run(
        dataset_id="drop_dataset",
        generation_results=(),
        previous_metrics=previous,
    ))
    assert metrics.drop_warning is True
    assert metrics.faithfulness == 0.55


def test_stub_ragas_runner_no_drop_when_stable(
    runner: StubRAGASRunner,
) -> None:
    """Stable faithfulness does not trigger drop_warning."""
    previous = RAGASSecondaryMetrics(faithfulness=0.6825, available=True)
    metrics = asyncio.run(runner.run(
        dataset_id="generation_dataset",
        generation_results=(),
        previous_metrics=previous,
    ))
    assert metrics.drop_warning is False
    assert metrics.faithfulness == 0.6825


def test_subprocess_ragas_runner_parses_json_output_file(tmp_path: Path) -> None:
    """SubprocessRAGASRunner parses the script JSON output format."""
    output_file = tmp_path / "ragas_output.json"
    output_file.write_text(json.dumps({
        "dataset": "generation_dataset.jsonl",
        "detail_level": 1,
        "total_questions": 2,
        "global_questions": 1,
        "local_questions": 1,
        "failed_questions": [],
        "metrics": {
            "faithfulness": 0.7,
            "answer_relevancy": 0.6,
            "llm_context_precision_without_reference": 0.5,
        },
    }), encoding="utf-8")

    runner = SubprocessRAGASRunner(script_path=None, json_output_path=str(output_file))
    metrics = asyncio.run(runner.run(
        dataset_id="generation_dataset",
        generation_results=(),
        previous_metrics=None,
    ))
    assert metrics.available is True
    assert metrics.faithfulness == pytest.approx(0.7)
    assert metrics.answer_relevancy == pytest.approx(0.6)
    assert metrics.context_precision == pytest.approx(0.5)


def test_subprocess_ragas_runner_missing_file_returns_available_false(
    tmp_path: Path,
) -> None:
    """A missing JSON output file is treated as RAGAS unavailable (WARNING)."""
    missing = tmp_path / "missing.json"
    runner = SubprocessRAGASRunner(script_path=None, json_output_path=str(missing))
    metrics = asyncio.run(runner.run(
        dataset_id="generation_dataset",
        generation_results=(),
        previous_metrics=None,
    ))
    assert metrics.available is False
