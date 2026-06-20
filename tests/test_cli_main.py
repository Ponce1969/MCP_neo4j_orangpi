"""Tests for the book-graph-rag CLI entrypoint."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from book_graph_rag.main import cli


def test_cli_help_exits_zero() -> None:
    """AC-05.1: --help shows available commands and exits 0."""
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])

    assert result.exit_code == 0
    assert "index" in result.output


def test_cli_index_help_exits_zero() -> None:
    """AC-05.1: index --help shows the command usage and exits 0."""
    runner = CliRunner()
    result = runner.invoke(cli, ["index", "--help"])

    assert result.exit_code == 0
    assert "PDF_PATH" in result.output


def test_cli_version_option() -> None:
    """--version prints the package version and exits 0."""
    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])

    assert result.exit_code == 0
    assert "book-graph-rag" in result.output


def test_cli_index_failfast_on_missing_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-05.2: missing/invalid .env produces a clean error and exit code 1."""
    monkeypatch.chdir(tmp_path)
    for var in ("NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD"):
        monkeypatch.delenv(var, raising=False)

    pdf = tmp_path / "book.pdf"
    pdf.write_text("fake pdf")

    runner = CliRunner()
    result = runner.invoke(cli, ["index", str(pdf)])

    assert result.exit_code == 1
    assert "Configuration error:" in result.output
    assert "Traceback" not in result.output


def test_cli_index_composition_correct_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-05.3: Settings -> adapters -> use case -> asyncio.run, in that order."""

    class FakeSettings:
        def __init__(self) -> None:
            calls.append("settings")
            self.llm_max_concurrency = 7
            self.processing_batch_size = 11
            self.dead_letter_path = Path("data/dead_letter.log")

    class FakePDFAdapter:
        def __init__(self, settings: object) -> None:
            calls.append(("pdf_adapter", settings))

    class FakeLLMAdapter:
        def __init__(self, settings: object) -> None:
            calls.append(("llm_adapter", settings))

    class FakeNeo4jAdapter:
        def __init__(self, settings: object) -> None:
            calls.append(("neo4j_adapter", settings))

    class FakeUseCase:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            calls.append(("use_case", kwargs))

        async def execute(self, pdf_path: str) -> None:
            calls.append(("execute", pdf_path))

    calls: list[Any] = []
    monkeypatch.setattr("book_graph_rag.main.Settings", FakeSettings)
    monkeypatch.setattr("book_graph_rag.main.PDFAdapter", FakePDFAdapter)
    monkeypatch.setattr("book_graph_rag.main.LLMAdapter", FakeLLMAdapter)
    monkeypatch.setattr("book_graph_rag.main.Neo4jAdapter", FakeNeo4jAdapter)
    monkeypatch.setattr("book_graph_rag.main.IndexBookUseCase", FakeUseCase)

    pdf = tmp_path / "book.pdf"
    pdf.write_text("fake pdf")

    runner = CliRunner()
    result = runner.invoke(cli, ["index", str(pdf)])

    assert result.exit_code == 0, result.output

    settings_call = calls[0]
    assert settings_call == "settings"

    adapter_calls = calls[1:4]
    assert [name for name, _ in adapter_calls] == ["pdf_adapter", "llm_adapter", "neo4j_adapter"]

    for _, arg in adapter_calls:
        assert isinstance(arg, FakeSettings)

    use_case_call = calls[4]
    assert use_case_call[0] == "use_case"
    kwargs = use_case_call[1]
    assert isinstance(kwargs["pdf_port"], FakePDFAdapter)
    assert isinstance(kwargs["llm_port"], FakeLLMAdapter)
    assert isinstance(kwargs["graph_db_port"], FakeNeo4jAdapter)
    assert kwargs["max_concurrency"] == 7
    assert kwargs["batch_size"] == 11
    assert kwargs["dead_letter_path"] == Path("data/dead_letter.log")

    execute_call = calls[5]
    assert execute_call == ("execute", str(pdf))
