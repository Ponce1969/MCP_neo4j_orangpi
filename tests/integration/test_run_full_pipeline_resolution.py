"""Integration test for run_full_pipeline --resolve-entities hybrid wiring.

Uses stub use cases to assert the pipeline calls the hybrid resolution path
when ``RESOLUTION_STRATEGY=hybrid`` without requiring a live Neo4j instance.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from book_graph_rag.application.resolve_entities_use_case import (
    MergeGroup,
    ResolveEntitiesResult,
)

from ..test_run_full_pipeline import (
    _FakeGraphDatabase,
    _make_fake_llm_adapter,
    _make_fake_neo4j_adapter,
    _make_fake_pdf_adapter,
    _make_fake_settings_class,
    _make_fake_use_case,
    _StatefulFakeSession,
)


class _FakeResolveUseCase:
    def __init__(self) -> None:
        self.analyze_calls: list[bool] = []

    async def analyze(self, *, dry_run: bool) -> ResolveEntitiesResult:
        self.analyze_calls.append(dry_run)
        return ResolveEntitiesResult(
            auto_merge_groups=[
                MergeGroup(
                    canonical_id="c1",
                    duplicate_ids=["d1"],
                    band="exact",  # type: ignore[arg-type]
                    evidence=[],
                ),
            ],
            quarantine_records=[],
            no_merge_candidates=[],
            total_pairs_evaluated=1,
        )


class _FakeApplyUseCase:
    def __init__(self) -> None:
        self.apply_calls: list[Any] = []

    async def apply(self, group: Any, *, approver: str) -> Any:
        self.apply_calls.append((group, approver))
        return None


@pytest.fixture
def hybrid_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, list[Any], Path]:
    # ``scripts`` is a namespace package; import here so pytest can collect the
    # module even when no other test has initialized the package.
    from scripts import run_full_pipeline as rfp

    calls: list[Any] = []
    monkeypatch.setattr(rfp, "Settings", _make_fake_settings_class(calls))
    monkeypatch.setattr(rfp, "PDFAdapter", _make_fake_pdf_adapter(calls))
    monkeypatch.setattr(rfp, "LLMAdapter", _make_fake_llm_adapter(calls))
    monkeypatch.setattr(rfp, "Neo4jCommandAdapter", _make_fake_neo4j_adapter(calls))
    monkeypatch.setattr(rfp, "IndexBookUseCase", _make_fake_use_case(calls))

    async def _fake_communities(fresh: bool = False) -> None:
        calls.append(("communities", fresh))

    monkeypatch.setattr(rfp, "_run_communities", _fake_communities)
    monkeypatch.setattr(
        rfp,
        "AsyncGraphDatabase",
        _FakeGraphDatabase(_StatefulFakeSession()),
    )
    monkeypatch.setattr(rfp, "_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setenv("RESOLUTION_STRATEGY", "hybrid")

    pdf = tmp_path / "book.pdf"
    pdf.write_text("fake pdf")
    return rfp, calls, pdf


def test_pipeline_runs_hybrid_resolution_when_flag_on(
    hybrid_env: tuple[Any, list[Any], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-IP.7: --resolve-entities with RESOLUTION_STRATEGY=hybrid calls analyze()."""
    rfp, calls, pdf = hybrid_env
    fake_resolve = _FakeResolveUseCase()
    fake_apply = _FakeApplyUseCase()

    async def _fake_build_resolve(settings: Any) -> tuple[Any, list[Any]]:
        calls.append("build_resolve")
        return fake_resolve, []

    async def _fake_build_apply(settings: Any) -> tuple[Any, list[Any]]:
        calls.append("build_apply")
        return fake_apply, []

    monkeypatch.setattr(rfp, "build_resolve_entities_use_case", _fake_build_resolve)
    monkeypatch.setattr(rfp, "build_apply_merge_use_case", _fake_build_apply)

    runner = CliRunner()
    result = runner.invoke(rfp.cli, [str(pdf), "--resolve-entities"])

    assert result.exit_code == 0, result.output
    assert "build_resolve" in calls
    assert len(fake_resolve.analyze_calls) == 1
    assert fake_resolve.analyze_calls[0] is False
    assert "build_apply" in calls
    assert len(fake_apply.apply_calls) == 1
    assert fake_apply.apply_calls[0][1] == "pipeline"
    assert "Entity resolution: merged 1 duplicates" in result.output


def test_pipeline_keeps_legacy_resolution_when_strategy_is_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without RESOLUTION_STRATEGY=hybrid the pipeline keeps the legacy slug+token path."""
    # Import inside the test because ``scripts`` is a namespace package.
    from scripts import run_full_pipeline as rfp

    monkeypatch.delenv("RESOLUTION_STRATEGY", raising=False)
    calls: list[Any] = []
    monkeypatch.setattr(rfp, "Settings", _make_fake_settings_class(calls))
    monkeypatch.setattr(rfp, "PDFAdapter", _make_fake_pdf_adapter(calls))
    monkeypatch.setattr(rfp, "LLMAdapter", _make_fake_llm_adapter(calls))
    monkeypatch.setattr(rfp, "Neo4jCommandAdapter", _make_fake_neo4j_adapter(calls))
    monkeypatch.setattr(rfp, "IndexBookUseCase", _make_fake_use_case(calls))

    async def _fake_communities(fresh: bool = False) -> None:
        calls.append(("communities", fresh))

    monkeypatch.setattr(rfp, "_run_communities", _fake_communities)
    monkeypatch.setattr(
        rfp,
        "AsyncGraphDatabase",
        _FakeGraphDatabase(_StatefulFakeSession()),
    )
    monkeypatch.setattr(rfp, "_BACKUP_DIR", tmp_path / "backups")

    class _FakeResolveModule:
        async def run_resolution(
            self, driver: Any, threshold: float = 0.9, dry_run: bool = False
        ) -> Any:
            calls.append(("legacy_resolve", threshold, dry_run))
            return type(
                "Report",
                (),
                {
                    "total_entities": 5,
                    "duplicate_count": 2,
                    "group_count": 1,
                    "dry_run": dry_run,
                },
            )()

    monkeypatch.setattr(rfp, "resolve_entities", _FakeResolveModule())

    pdf = tmp_path / "book.pdf"
    pdf.write_text("fake pdf")

    runner = CliRunner()
    result = runner.invoke(rfp.cli, [str(pdf), "--resolve-entities"])

    assert result.exit_code == 0, result.output
    assert ("legacy_resolve", 0.9, False) in calls
    assert "Entity resolution: merged 2 duplicates into 1 groups." in result.output

