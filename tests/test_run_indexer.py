"""Tests for scripts/run_indexer.py."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import scripts.run_indexer as run_indexer
from click.testing import CliRunner


def _make_fake_settings_class(calls: list[Any]) -> type:
    class FakeSettings:
        def __init__(self) -> None:
            calls.append("settings")
            self.llm_max_concurrency = 2
            self.processing_batch_size = 3
            self.dead_letter_path = Path("data/dead_letter.log")
            self.relationship_orphan_policy = "log_orphan"

    return FakeSettings


def _make_fake_pdf_adapter(calls: list[Any]) -> type:
    class FakePDFAdapter:
        def __init__(self, settings: object) -> None:
            calls.append("pdf_adapter")

        def extract_chunks(self, pdf_path: str) -> Any:
            from book_graph_rag.domain.models import Book, KnowledgeGraphChunk, PageRef

            calls.append(("extract_chunks", pdf_path))
            for idx in range(2):
                yield KnowledgeGraphChunk(
                    text=f"chunk {idx}",
                    chunk_index=idx,
                    book=Book(
                        id="book",
                        title="Book",
                        pdf_path=pdf_path,
                        page_count=1,
                    ),
                    page_ref=PageRef(start=1, end=1),
                )

    return FakePDFAdapter


def _make_fake_llm_adapter(calls: list[Any]) -> type:
    class FakeLLMAdapter:
        def __init__(self, settings: object) -> None:
            calls.append("llm_adapter")

    return FakeLLMAdapter


def _make_fake_neo4j_adapter(calls: list[Any]) -> type:
    class FakeNeo4jCommandAdapter:
        def __init__(self, settings: object) -> None:
            calls.append("neo4j_adapter")

        async def count_chunks(self) -> int:
            calls.append("count_chunks")
            return 10

        async def count_entities(self) -> int:
            calls.append("count_entities")
            return 20

        async def count_mentions(self) -> int:
            calls.append("count_mentions")
            return 30

        async def clear_index(self) -> None:
            calls.append("clear_index")

        async def close(self) -> None:
            calls.append("close")

    return FakeNeo4jCommandAdapter


def _make_fake_use_case(calls: list[Any]) -> type:
    class FakeUseCase:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            calls.append("use_case")

        async def execute(self, pdf_path: str) -> None:
            calls.append(("execute", pdf_path))

    return FakeUseCase


@pytest.fixture
def fake_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[Any], Path]:
    calls: list[Any] = []
    monkeypatch.setattr(run_indexer, "Settings", _make_fake_settings_class(calls))
    monkeypatch.setattr(run_indexer, "PDFAdapter", _make_fake_pdf_adapter(calls))
    monkeypatch.setattr(run_indexer, "LLMAdapter", _make_fake_llm_adapter(calls))
    monkeypatch.setattr(
        run_indexer, "Neo4jCommandAdapter", _make_fake_neo4j_adapter(calls)
    )
    monkeypatch.setattr(run_indexer, "IndexBookUseCase", _make_fake_use_case(calls))

    pdf = tmp_path / "book.pdf"
    pdf.write_text("fake pdf")
    return calls, pdf


def test_run_indexer_help_shows_options() -> None:
    """AC-IP.1: help shows --pdf plus the diagnostic flags."""
    runner = CliRunner()
    result = runner.invoke(run_indexer.main, ["--help"])

    assert result.exit_code == 0
    assert "--pdf" in result.output
    assert "--clear" in result.output
    assert "--dry-run" in result.output
    assert "--fresh" in result.output
    assert "TODO(Fase 05)" not in result.output


def test_run_indexer_default_run_wires_use_case(fake_env: tuple[list[Any], Path]) -> None:
    """AC-IP.1: default run builds adapters, use case, awaits execute, prints counts."""
    calls, pdf = fake_env
    runner = CliRunner()
    result = runner.invoke(run_indexer.main, ["--pdf", str(pdf)])

    assert result.exit_code == 0, result.output
    assert "settings" in calls
    assert "pdf_adapter" in calls
    assert "llm_adapter" in calls
    assert "neo4j_adapter" in calls
    assert "use_case" in calls
    assert ("execute", str(pdf)) in calls
    assert calls.count("count_chunks") >= 1
    assert calls.count("count_entities") >= 1
    assert calls.count("count_mentions") >= 1


def test_run_indexer_dry_run_shows_counts_and_skips_execute(
    fake_env: tuple[list[Any], Path]
) -> None:
    """AC-IP.3: --dry-run reports counts without writing or indexing."""
    calls, pdf = fake_env
    runner = CliRunner()
    result = runner.invoke(run_indexer.main, ["--pdf", str(pdf), "--dry-run"])

    assert result.exit_code == 0, result.output
    assert ("execute", str(pdf)) not in calls
    assert "clear_index" not in calls
    assert calls.count("count_chunks") >= 1
    assert calls.count("count_entities") >= 1
    assert calls.count("count_mentions") >= 1
    assert "10" in result.output
    assert "20" in result.output
    assert "30" in result.output


def test_run_indexer_clear_calls_clear_index_and_skips_execute(
    fake_env: tuple[list[Any], Path]
) -> None:
    """AC-IP.4: --clear clears the index and does not index."""
    calls, pdf = fake_env
    runner = CliRunner()
    result = runner.invoke(run_indexer.main, ["--pdf", str(pdf), "--clear"])

    assert result.exit_code == 0, result.output
    assert "clear_index" in calls
    assert ("execute", str(pdf)) not in calls


def test_run_indexer_fresh_calls_clear_then_execute(
    fake_env: tuple[list[Any], Path]
) -> None:
    """AC-IP.4/5: --fresh clears before indexing in the correct order."""
    calls, pdf = fake_env
    runner = CliRunner()
    result = runner.invoke(run_indexer.main, ["--pdf", str(pdf), "--fresh"])

    assert result.exit_code == 0, result.output
    assert "clear_index" in calls
    assert ("execute", str(pdf)) in calls
    clear_idx = calls.index("clear_index")
    execute_idx = calls.index(("execute", str(pdf)))
    assert clear_idx < execute_idx


def test_run_indexer_failfast_on_missing_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing environment variables produce a clean configuration error."""
    monkeypatch.chdir(tmp_path)
    for var in ("NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD"):
        monkeypatch.delenv(var, raising=False)

    pdf = tmp_path / "book.pdf"
    pdf.write_text("fake pdf")

    runner = CliRunner()
    result = runner.invoke(run_indexer.main, ["--pdf", str(pdf)])

    assert result.exit_code == 1
    assert "Configuration error:" in result.output
    assert "Traceback" not in result.output
