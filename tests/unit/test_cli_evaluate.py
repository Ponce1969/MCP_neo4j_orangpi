"""CLI tests for book-graph-rag evaluate --layer (Slice C, T-C.5)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from book_graph_rag.domain.evaluation_models import (
    EvaluationLayerResult,
    EvaluationReport,
    LayerMetricValue,
    LayerRunMetadata,
    LayerStatus,
)
from book_graph_rag.main import cli


def _fake_settings(tmp_path: Path) -> type:
    class Settings:
        @classmethod
        def model_validate(cls, _: object) -> Settings:
            return cls()

        neo4j_uri = "bolt://db:7687"
        neo4j_database = "neo4j"
        catalog_path = Path("catalog.yaml")
        gates_policy_path = Path("gates.yaml")
        evaluation_dir = Path("data/evaluation")
        evaluation_manifest_path = Path("data/evaluation/MANIFEST.json")
        evaluation_baseline_dir = Path("data/evaluation")
        claim_extractor_model = ""
        pairwise_judge_model = ""
        ragas_enabled = True
        query_llm_model_name = "q-model"

    return Settings


def _layer_result(layer: str, status: LayerStatus) -> EvaluationLayerResult:
    return EvaluationLayerResult(
        layer=layer,  # type: ignore[arg-type]
        status=status,
        project_owned_metrics=(
            LayerMetricValue(name="metric", value=0.75, threshold=None, comparator=">="),
        ),
        rationale="ok",
        source_dataset_id=f"{layer}_dataset",
        run_metadata=LayerRunMetadata(run_id="run-1", code_commit="abc123"),
    )


def _report(layer: str, status: LayerStatus) -> EvaluationReport:
    result = _layer_result(layer, status)
    return EvaluationReport(
        overall_status=status,
        layer_results=(result,),
        run_id="run-1",
        code_commit="abc123",
    )


class FakeEvaluateCommandUseCase:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def execute(
        self, *, layer: str, scope: str | None = None
    ) -> tuple[EvaluationReport, int]:
        self.calls.append({"layer": layer, "scope": scope})
        if layer == "resolution":
            return _report("resolution", LayerStatus.PASSED), 0
        if layer == "retrieval":
            return _report("retrieval", LayerStatus.PASSED), 0
        if layer == "generation":
            return _report("generation", LayerStatus.PASSED), 0
        if layer == "all":
            return _report("all", LayerStatus.PASSED), 0
        raise ValueError(f"unknown layer: {layer}")


@pytest.fixture
def evaluate_use_case(monkeypatch: pytest.MonkeyPatch) -> FakeEvaluateCommandUseCase:
    fake = FakeEvaluateCommandUseCase()
    monkeypatch.setattr(
        "book_graph_rag.main._build_evaluate_command_use_case", lambda _: fake
    )
    return fake


def test_evaluate_layer_resolution_emits_json(
    monkeypatch: pytest.MonkeyPatch, evaluate_use_case: FakeEvaluateCommandUseCase
) -> None:
    """evaluate --layer resolution prints JSON report to stdout."""
    monkeypatch.setattr("book_graph_rag.main.Settings", _fake_settings(Path(".")))
    result = CliRunner().invoke(cli, ["evaluate", "--layer", "resolution"])
    assert result.exit_code == 0
    parsed = json.loads(result.stdout)
    assert parsed["overall_status"] == "passed"
    assert parsed["layer_results"][0]["layer"] == "resolution"


def test_evaluate_layer_unknown_fails_with_exit_2(
    monkeypatch: pytest.MonkeyPatch, evaluate_use_case: FakeEvaluateCommandUseCase
) -> None:
    """Unknown layer fails deterministically with exit 2."""
    monkeypatch.setattr("book_graph_rag.main.Settings", _fake_settings(Path(".")))
    result = CliRunner().invoke(cli, ["evaluate", "--layer", "nope"])
    assert result.exit_code == 2
    assert "unknown layer" in result.output.lower() or "nope" in result.output


def test_evaluate_layer_output_file_written(
    monkeypatch: pytest.MonkeyPatch,
    evaluate_use_case: FakeEvaluateCommandUseCase,
    tmp_path: Path,
) -> None:
    """--output persists the JSON report."""
    monkeypatch.setattr("book_graph_rag.main.Settings", _fake_settings(tmp_path))
    output_path = tmp_path / "report.json"
    result = CliRunner().invoke(
        cli, ["evaluate", "--layer", "resolution", "--output", str(output_path)]
    )
    assert result.exit_code == 0
    assert output_path.exists()
    parsed = json.loads(output_path.read_text(encoding="utf-8"))
    assert parsed["overall_status"] == "passed"


def test_evaluate_layer_human_summary_to_stderr(
    monkeypatch: pytest.MonkeyPatch, evaluate_use_case: FakeEvaluateCommandUseCase
) -> None:
    """Human-readable summary goes to stderr."""
    monkeypatch.setattr("book_graph_rag.main.Settings", _fake_settings(Path(".")))
    result = CliRunner().invoke(cli, ["evaluate", "--layer", "resolution"])
    assert result.exit_code == 0
    assert "resolution" in result.stderr.lower() or "layer" in result.stderr.lower()


def test_evaluate_layer_propagates_layer_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI exits with the layer's mapped exit code."""
    monkeypatch.setattr("book_graph_rag.main.Settings", _fake_settings(Path(".")))

    class FailingUseCase:
        async def execute(self, *, layer: str, scope: str | None = None) -> tuple[Any, int]:
            return _report(layer, LayerStatus.FAILED), 10

    monkeypatch.setattr(
        "book_graph_rag.main._build_evaluate_command_use_case", lambda _: FailingUseCase()
    )
    result = CliRunner().invoke(cli, ["evaluate", "--layer", "generation"])
    assert result.exit_code == 10
