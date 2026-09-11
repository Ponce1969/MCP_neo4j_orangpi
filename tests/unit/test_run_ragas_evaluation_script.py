"""Tests for scripts/run_ragas_evaluation.py flag extensions (Slice B, T-B.9)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

import run_ragas_evaluation as run_ragas_module

from book_graph_rag.domain.models import CommunitySummary


@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set minimal settings env vars for the script to construct Settings."""
    monkeypatch.setenv("NEO4J_URI", "bolt://localhost:7687")
    monkeypatch.setenv("NEO4J_USER", "neo4j")
    monkeypatch.setenv("NEO4J_PASSWORD", "password")
    monkeypatch.setenv("QUERY_LLM_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("QUERY_LLM_MODEL_NAME", "model")
    monkeypatch.setenv("GRAPH_LLM_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("GRAPH_LLM_MODEL_NAME", "model")


class _FakeQueryAdapter:
    def __init__(self, settings: Any) -> None:
        pass

    async def close(self) -> None:
        pass

    async def search_chunks(self, question: str, limit: int = 10) -> list[dict[str, Any]]:
        return [{"text": "chunk"}]

    async def find_entity(self, name: str, entity_type: Any | None) -> list[Any]:
        return []


class _FakeCommunityAdapter:
    def __init__(self, settings: Any) -> None:
        pass

    async def close(self) -> None:
        pass

    async def get_summaries_by_level(self, level: int) -> list[CommunitySummary]:
        return []


class _FakeLLMAdapter:
    def __init__(self, settings: Any) -> None:
        pass


class _FakeGlobalUC:
    def __init__(self, **kwargs: Any) -> None:
        pass

    async def ask(self, question: str, detail_level: int) -> dict[str, Any]:
        return {"answer": "fake answer", "citations": []}


@pytest.fixture
def patched_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace infrastructure imports in the script module with fakes."""
    monkeypatch.setattr(run_ragas_module, "Neo4jQueryAdapter", _FakeQueryAdapter)
    monkeypatch.setattr(run_ragas_module, "Neo4jCommunityAdapter", _FakeCommunityAdapter)
    monkeypatch.setattr(run_ragas_module, "LLMAdapter", _FakeLLMAdapter)
    monkeypatch.setattr(run_ragas_module, "GlobalQueryUseCase", _FakeGlobalUC)
    monkeypatch.setattr(
        run_ragas_module, "_load_dataset", lambda path: [{"type": "local", "question": "q?"}]
    )


def test_json_output_flag_writes_file(
    cli_runner: CliRunner,
    env_vars: None,
    patched_module: None,
    tmp_path: Path,
) -> None:
    """--json-output writes the after-metrics JSON to the requested path."""
    output_file = tmp_path / "out.json"
    result = cli_runner.invoke(
        run_ragas_module.main,
        [
            "--no-ragas",
            "--no-baseline",
            "--json-output",
            str(output_file),
            "--after-output",
            str(tmp_path / "after.json"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert output_file.exists()
    parsed = json.loads(output_file.read_text(encoding="utf-8"))
    assert parsed["total_questions"] == 1
    assert parsed["baseline"] is None
    assert parsed["delta"] is None


def test_no_baseline_alias_accepted(
    cli_runner: CliRunner,
    env_vars: None,
    patched_module: None,
    tmp_path: Path,
) -> None:
    """--no-baseline is accepted as an alias for --no-compare."""
    output_file = tmp_path / "out.json"
    result = cli_runner.invoke(
        run_ragas_module.main,
        [
            "--no-ragas",
            "--no-baseline",
            "--json-output",
            str(output_file),
            "--after-output",
            str(tmp_path / "after.json"),
        ],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(output_file.read_text(encoding="utf-8"))
    assert parsed["baseline"] is None
    assert parsed["delta"] is None


def test_existing_flags_unmodified(
    cli_runner: CliRunner,
    env_vars: None,
    patched_module: None,
    tmp_path: Path,
) -> None:
    """The existing --no-ragas and --no-compare flags still work."""
    result = cli_runner.invoke(
        run_ragas_module.main,
        [
            "--no-ragas",
            "--no-compare",
            "--after-output",
            str(tmp_path / "after.json"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Skipping RAGAS" in result.output


def test_after_output_redirect_never_touches_repo_benchmark(
    cli_runner: CliRunner,
    env_vars: None,
    patched_module: None,
    tmp_path: Path,
) -> None:
    """--after-output redirects the default gr3_after.json write (regression)."""
    benchmark = Path("docs/benchmarks/gr3_after.json")
    before = benchmark.read_text(encoding="utf-8") if benchmark.exists() else None
    after_file = tmp_path / "after.json"
    result = cli_runner.invoke(
        run_ragas_module.main,
        [
            "--no-ragas",
            "--no-compare",
            "--after-output",
            str(after_file),
        ],
    )
    assert result.exit_code == 0, result.output
    assert after_file.exists()
    if before is not None:
        assert benchmark.read_text(encoding="utf-8") == before
    else:
        assert not benchmark.exists(), (
            "--after-output recreated the default docs/benchmarks/gr3_after.json file"
        )
