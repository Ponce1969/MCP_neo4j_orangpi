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


def test_cli_index_fails_before_adapter_construction_when_llm_settings_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The index composition root reports missing LLM settings before network setup."""

    class IncompleteSettings:
        @classmethod
        def model_validate(cls, data: object) -> IncompleteSettings:
            return cls()

        llm_max_concurrency = 3
        processing_batch_size = 5
        dead_letter_path = Path("data/dead_letter.log")
        graph_llm_base_url = ""
        graph_llm_model_name = ""
        query_llm_base_url = ""
        query_llm_model_name = ""

    monkeypatch.setattr("book_graph_rag.main.Settings", IncompleteSettings)
    pdf = tmp_path / "book.pdf"
    pdf.write_text("fake pdf")

    result = CliRunner().invoke(cli, ["index", str(pdf)])

    assert result.exit_code == 1
    assert "Configuration error:" in result.output
    assert "GRAPH_LLM_BASE_URL" in result.output
    assert "QUERY_LLM_BASE_URL" in result.output


def test_cli_index_composition_correct_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-05.3: Settings -> adapters -> use case -> asyncio.run, in that order."""

    class FakeSettings:
        @classmethod
        def model_validate(cls, data: object) -> FakeSettings:
            return cls()

        def __init__(self) -> None:
            calls.append("settings")
            self.llm_max_concurrency = 7
            self.processing_batch_size = 11
            self.dead_letter_path = Path("data/dead_letter.log")
            self.relationship_orphan_policy = "log_orphan"
            self.graph_llm_base_url = "https://graph-provider.test/v1"
            self.graph_llm_model_name = "graph-model"
            self.query_llm_base_url = "https://query-provider.test/v1"
            self.query_llm_model_name = "query-model"

    class FakePDFAdapter:
        def __init__(self, settings: object) -> None:
            calls.append(("pdf_adapter", settings))

    class FakeLLMAdapter:
        def __init__(self, settings: object) -> None:
            calls.append(("llm_adapter", settings))

    class FakeNeo4jCommandAdapter:
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
    monkeypatch.setattr("book_graph_rag.main.Neo4jCommandAdapter", FakeNeo4jCommandAdapter)
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
    assert isinstance(kwargs["graph_db_port"], FakeNeo4jCommandAdapter)
    assert kwargs["max_concurrency"] == 7
    assert kwargs["batch_size"] == 11
    assert kwargs["dead_letter_path"] == Path("data/dead_letter.log")

    execute_call = calls[5]
    assert execute_call == ("execute", str(pdf))


def test_cli_index_unknown_namespace_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-02 §4: index into an unregistered namespace exits 2 before adapters."""

    catalog_file = tmp_path / "catalog.yaml"
    catalog_file.write_text(
        "version: 1\n"
        "corpora:\n"
        "  knowledge:\n"
        "    label: \"Knowledge Library\"\n"
        "    sources:\n"
        "      known-source:\n"
        "        label: Known\n"
        "        file: known.pdf\n"
        "        status: active\n"
    )

    class FakeSettings:
        @classmethod
        def model_validate(cls, data: object) -> FakeSettings:
            return cls()

        def __init__(self) -> None:
            self.catalog_path = catalog_file

    constructed: list[str] = []

    def fake_validate(settings: object) -> None:
        return None

    monkeypatch.setattr("book_graph_rag.main.Settings", FakeSettings)
    monkeypatch.setattr("book_graph_rag.main.validate_llm_provider_settings", fake_validate)
    monkeypatch.setattr("book_graph_rag.main.PDFAdapter", lambda *a, **k: constructed.append("pdf"))
    monkeypatch.setattr("book_graph_rag.main.LLMAdapter", lambda *a, **k: constructed.append("llm"))
    monkeypatch.setattr(
        "book_graph_rag.main.Neo4jCommandAdapter", lambda *a, **k: constructed.append("neo4j")
    )

    pdf = tmp_path / "book.pdf"
    pdf.write_text("fake pdf")

    result = CliRunner().invoke(
        cli, ["index", str(pdf), "--corpus", "knowledge", "--source", "unknown-source"]
    )

    assert result.exit_code == 2
    assert "Namespace error:" in result.output
    assert "Unknown source" in result.output
    assert constructed == []
