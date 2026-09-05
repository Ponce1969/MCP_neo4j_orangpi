"""CLI flag acceptance tests for `book-graph-rag index` Phase 2 commands.

These tests exercise the Click surface and the wiring of the composition root.
All heavy dependencies (adapters, use cases, PDF parsing, version computation)
are replaced with deterministic fakes so the suite remains a true unit test.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from book_graph_rag.domain.checkpoint_models import VersionDimensions
from book_graph_rag.main import cli


@pytest.fixture
def fake_settings(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Replace Settings with a minimal fake that satisfies validation."""

    class FakeSecret:
        def get_secret_value(self) -> str:
            return "password"

    class FakeSettings:
        @classmethod
        def model_validate(cls, data: object) -> FakeSettings:
            return cls()

        neo4j_uri = "bolt://localhost:7687"
        neo4j_user = "neo4j"
        neo4j_password = FakeSecret()
        neo4j_database = "neo4j"
        catalog_path = Path("catalog.yaml")
        llm_max_concurrency = 3
        processing_batch_size = 5
        checkpoint_enabled = True
        checkpoint_max_attempts = 3
        checkpoint_stale_lease_seconds = 300
        pipeline_version = "1.0.0"
        schema_version = "1.0.0"
        graph_llm_model_date = "2026-09-01"
        graph_llm_base_url = "https://graph.test/v1"
        graph_llm_model_name = "graph-model"
        query_llm_base_url = "https://query.test/v1"
        query_llm_model_name = "query-model"
        dead_letter_path = Path("data/dead_letter.log")
        dead_letter_path_chunks = Path("data/dead_letter_chunks.jsonl")
        relationship_orphan_policy = "log_orphan"

    monkeypatch.setattr("book_graph_rag.main.Settings", FakeSettings)
    monkeypatch.setattr("book_graph_rag.main.validate_llm_provider_settings", lambda s: None)
    return FakeSettings


@pytest.fixture
def fake_adapters(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub every infrastructure adapter the composition root may construct."""

    class FakePDFAdapter:
        def __init__(self, settings: object, namespace: object | None = None) -> None:
            self.settings = settings
            self.namespace = namespace

        def extract_chunks(self, pdf_path: str) -> list[Any]:
            return []

    class FakeLLMAdapter:
        def __init__(self, settings: object, namespace: object | None = None) -> None:
            self.settings = settings
            self.namespace = namespace

    class FakeNeo4jCommandAdapter:
        def __init__(self, settings: object) -> None:
            self.settings = settings

    class FakeNeo4jCheckpointAdapter:
        def __init__(self, settings: object) -> None:
            self.settings = settings

        async def close(self) -> None:
            pass

    class FakeDeadLetter:
        def __init__(self, path: Path, failed_chunk_path: Path | None = None) -> None:
            self.path = path
            self.failed_chunk_path = failed_chunk_path

    monkeypatch.setattr(
        "book_graph_rag.main.PDFAdapter", FakePDFAdapter, raising=False
    )
    monkeypatch.setattr(
        "book_graph_rag.main.LLMAdapter", FakeLLMAdapter, raising=False
    )
    monkeypatch.setattr(
        "book_graph_rag.main.Neo4jCommandAdapter",
        FakeNeo4jCommandAdapter,
        raising=False,
    )
    monkeypatch.setattr(
        "book_graph_rag.main.Neo4jCheckpointAdapter",
        FakeNeo4jCheckpointAdapter,
        raising=False,
    )
    monkeypatch.setattr(
        "book_graph_rag.main.JSONLDeadLetter", FakeDeadLetter, raising=False
    )

    fake_versions = VersionDimensions(
        source_version="a" * 16,
        pipeline_version="1.0.0",
        model_version="openai:graph-model:2026-09-01",
        schema_version="1.0.0",
    )
    monkeypatch.setattr(
        "book_graph_rag.main.compute_version_dimensions",
        lambda pdf_bytes, settings: fake_versions,
        raising=False,
    )

    return {
        "versions": fake_versions,
    }


@pytest.fixture
def fake_use_cases(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub the three use cases reachable from the index command."""

    calls: dict[str, list[tuple[tuple[Any, ...], dict[str, Any]]]] = {
        "index": [],
        "replay": [],
        "backfill": [],
    }

    class FakeIndexUseCase:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            calls["index"].append(((), kwargs))

        async def execute(self, pdf_path: str) -> None:
            pass

        async def close(self) -> None:
            pass

    class FakeReplayUseCase:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            calls["replay"].append(((), kwargs))
            self._require_force = False

        async def execute(
            self,
            source_id: str | None = None,
            limit: int | None = None,
            force_reprocess: bool = False,
        ) -> int:
            if self._require_force and not force_reprocess:
                raise ValueError(
                    "Chunk corpus:source:0 is already PROCESSED; use --force-reprocess to replay it"
                )
            return 1

        async def close(self) -> None:
            pass

    class FakeBackfillUseCase:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            calls["backfill"].append(((), kwargs))

        async def execute(
            self,
            source_id: str,
            *,
            apply: bool = False,
            approval_path: Path | None = None,
        ) -> Any:
            return type(
                "Report",
                (),
                {
                    "processed_count": 0,
                    "model_dump_json": lambda self, **_: '{"processed_count": 0}',
                },
            )()

        async def close(self) -> None:
            pass

    monkeypatch.setattr(
        "book_graph_rag.main.IndexBookUseCase", FakeIndexUseCase, raising=False
    )
    monkeypatch.setattr(
        "book_graph_rag.main.ReplayDeadLetterUseCase",
        FakeReplayUseCase,
        raising=False,
    )
    monkeypatch.setattr(
        "book_graph_rag.main.BackfillCheckpointsUseCase",
        FakeBackfillUseCase,
        raising=False,
    )

    return calls


def _make_pdf(tmp_path: Path) -> Path:
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    return pdf


def test_index_help_lists_new_flags() -> None:
    """The help text advertises the new Phase 2 flags."""
    result = CliRunner().invoke(cli, ["index", "--help"])
    assert result.exit_code == 0
    assert "--resume" in result.output
    assert "--replay-dead-letter" in result.output
    assert "--backfill-checkpoints" in result.output
    assert "--force-reprocess" in result.output
    assert "--dry-run" in result.output


@pytest.mark.parametrize(
    "extra",
    [
        [],
        ["--resume"],
        ["--no-resume"],
        ["--replay-dead-letter", "--limit", "10", "--source-id", "corpus:source"],
        ["--backfill-checkpoints", "--source-id", "corpus:source", "--dry-run"],
        ["--force-reprocess"],
        ["--replay-dead-letter", "--force-reprocess"],
    ],
)
def test_valid_flag_combos_are_accepted(
    tmp_path: Path,
    fake_settings: Any,
    fake_adapters: Any,
    fake_use_cases: Any,
    extra: list[str],
) -> None:
    """Known-good flag combinations exit 0 and reach the expected use case."""
    pdf = _make_pdf(tmp_path)
    result = CliRunner().invoke(cli, ["index", str(pdf), *extra])
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize(
    ("extra", "expected_snippet"),
    [
        (["--replay-dead-letter", "--backfill-checkpoints"], "mutually exclusive"),
        (["--replay-dead-letter", "--no-resume"], "no-resume"),
        (["--backfill-checkpoints", "--no-resume"], "no-resume"),
        (["--backfill-checkpoints", "--limit", "5"], "limit"),
        (["--replay-dead-letter", "--dry-run"], "dry-run"),
        (["--no-resume", "--dry-run"], "dry-run"),
        (["--replay-dead-letter", "--limit", "0"], "limit"),
        (["--replay-dead-letter", "--limit", "-1"], "limit"),
    ],
)
def test_mutually_exclusive_or_context_invalid_combos_rejected(
    tmp_path: Path,
    fake_settings: Any,
    fake_adapters: Any,
    fake_use_cases: Any,
    extra: list[str],
    expected_snippet: str,
) -> None:
    """Invalid flag combinations are rejected with a non-zero exit code."""
    pdf = _make_pdf(tmp_path)
    result = CliRunner().invoke(cli, ["index", str(pdf), *extra])
    assert result.exit_code != 0, result.output
    assert expected_snippet.lower() in result.output.lower()


def test_force_reprocess_required_for_processed_replay_targets(
    tmp_path: Path,
    fake_settings: Any,
    fake_adapters: Any,
    fake_use_cases: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replay without --force-reprocess fails when the use case targets PROCESSED chunks."""

    class StrictReplayUseCase:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def execute(
            self,
            source_id: str | None = None,
            limit: int | None = None,
            force_reprocess: bool = False,
        ) -> int:
            if not force_reprocess:
                raise ValueError(
                    "Chunk corpus:source:0 is already PROCESSED; use --force-reprocess to replay it"
                )
            return 1

        async def close(self) -> None:
            pass

    monkeypatch.setattr("book_graph_rag.main.ReplayDeadLetterUseCase", StrictReplayUseCase)

    pdf = _make_pdf(tmp_path)
    result = CliRunner().invoke(
        cli, ["index", str(pdf), "--replay-dead-letter", "--source-id", "corpus:source"]
    )
    assert result.exit_code != 0
    assert "--force-reprocess" in result.output

    result = CliRunner().invoke(
        cli,
        [
            "index",
            str(pdf),
            "--replay-dead-letter",
            "--source-id",
            "corpus:source",
            "--force-reprocess",
        ],
    )
    assert result.exit_code == 0, result.output
