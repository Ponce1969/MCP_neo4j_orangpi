"""Tests for the entity-resolution CLI wrapper (dual-path).

Covers both ``scripts/resolve_entities.py`` and the ``resolve-entities``
subcommand in ``src/book_graph_rag/main.py``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from click.testing import CliRunner
from scripts import resolve_entities as resolve

from book_graph_rag.application.resolve_entities_use_case import (
    ResolveEntitiesResult,
)
from book_graph_rag.main import cli


class _FakeSettings:
    @classmethod
    def model_validate(cls, data: object) -> _FakeSettings:
        return cls()


class _FakeResolveUseCase:
    def __init__(self) -> None:
        self.dry_run: bool | None = None
        self.analyze_calls: int = 0

    async def analyze(self, *, dry_run: bool) -> ResolveEntitiesResult:
        self.dry_run = dry_run
        self.analyze_calls += 1
        return ResolveEntitiesResult(
            auto_merge_groups=[],
            quarantine_records=[],
            no_merge_candidates=[],
            total_pairs_evaluated=0,
        )


# ── scripts/resolve_entities.py ──────────────────────────────────────────────


def test_legacy_default_path_runs_existing_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without RESOLUTION_STRATEGY the CLI keeps the slug+token legacy path."""
    monkeypatch.delenv("RESOLUTION_STRATEGY", raising=False)
    calls: list[Any] = []

    async def _fake_run_main(threshold: float, dry_run: bool) -> None:
        calls.append(("legacy", threshold, dry_run))

    monkeypatch.setattr(resolve, "_run_main", _fake_run_main)

    runner = CliRunner()
    result = runner.invoke(resolve.main, ["--dry-run"])

    assert result.exit_code == 0, result.output
    assert ("legacy", 0.9, True) in calls


def test_hybrid_path_invokes_use_case_and_prints_report(monkeypatch: pytest.MonkeyPatch) -> None:
    """RESOLUTION_STRATEGY=hybrid delegates to ResolveEntitiesUseCase."""
    monkeypatch.setenv("RESOLUTION_STRATEGY", "hybrid")
    monkeypatch.setattr(resolve, "Settings", _FakeSettings)
    fake_use_case = _FakeResolveUseCase()

    async def _fake_factory(settings: Any) -> tuple[Any, list[Any]]:
        return fake_use_case, []

    monkeypatch.setattr(resolve, "build_resolve_entities_use_case", _fake_factory)

    runner = CliRunner()
    result = runner.invoke(resolve.main, ["--dry-run"])

    assert result.exit_code == 0, result.output
    assert fake_use_case.dry_run is True
    assert fake_use_case.analyze_calls == 1
    parsed = json.loads(result.output)
    assert parsed["strategy"] == "hybrid"
    assert parsed["dry_run"] is True


# ── main.py resolve-entities subcommand ──────────────────────────────────────


def test_main_resolve_entities_invokes_use_case(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``resolve-entities`` subcommand analyzes the active graph."""
    monkeypatch.setattr("book_graph_rag.main.Settings", _FakeSettings)
    fake_use_case = _FakeResolveUseCase()

    async def _fake_factory(settings: Any) -> tuple[Any, list[Any]]:
        return fake_use_case, []

    monkeypatch.setattr("book_graph_rag.main.build_resolve_entities_use_case", _fake_factory)

    runner = CliRunner()
    result = runner.invoke(cli, ["resolve-entities", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert fake_use_case.dry_run is True
    assert fake_use_case.analyze_calls == 1
    parsed = json.loads(result.output)
    assert parsed["strategy"] == "hybrid"


