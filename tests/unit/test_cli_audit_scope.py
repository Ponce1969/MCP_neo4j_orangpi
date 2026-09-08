"""CLI tests for the audit --scope option."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from click.testing import CliRunner

from book_graph_rag.main import cli


def test_cli_audit_accepts_scope_and_threads_it_through(monkeypatch: Any) -> None:
    """A valid --scope is validated and passed to the audit use case."""

    class Settings:
        @classmethod
        def model_validate(cls, _: object) -> Settings:
            return cls()

        neo4j_uri = "bolt://db:7687"
        neo4j_database = "neo4j"
        catalog_path = Path("catalog.yaml")

    from book_graph_rag.domain.audit_models import AuditScope

    captured: dict[str, AuditScope | None] = {}

    class UseCase:
        def __init__(self, port: object) -> None:
            pass

        async def execute(
            self, target: object, sample_limit: int, scope: AuditScope | None = None
        ) -> Any:
            captured["scope"] = scope
            return type(
                "Report",
                (),
                {
                    "state": "passed",
                    "scope": scope.display if scope is not None else None,
                    "model_dump_json": lambda self, **_: json.dumps(
                        {"state": "passed", "scope": self.scope}
                    ),
                },
            )()

    monkeypatch.setattr("book_graph_rag.main.Settings", Settings)
    monkeypatch.setattr("book_graph_rag.main.Neo4jAuditAdapter", lambda settings: object())
    monkeypatch.setattr("book_graph_rag.main.AuditGraphUseCase", UseCase)

    result = CliRunner().invoke(
        cli,
        ["audit", "--target", "bookgraph-neo4j", "--scope", "knowledge"],
    )
    assert result.exit_code == 0
    assert captured["scope"] is not None
    assert captured["scope"].corpus == "knowledge"


def test_cli_audit_unknown_scope_fails_fast_exit_2(monkeypatch: Any) -> None:
    """An unknown corpus fails before any graph adapter is constructed."""
    adapter_calls: list[Any] = []

    class Settings:
        @classmethod
        def model_validate(cls, _: object) -> Settings:
            return cls()

        neo4j_uri = "bolt://db:7687"
        neo4j_database = "neo4j"
        catalog_path = Path("catalog.yaml")

    monkeypatch.setattr("book_graph_rag.main.Settings", Settings)
    def _fake_adapter(settings: object) -> object:
        adapter_calls.append(settings)
        return object()

    monkeypatch.setattr("book_graph_rag.main.Neo4jAuditAdapter", _fake_adapter)

    result = CliRunner().invoke(
        cli,
        ["audit", "--target", "bookgraph-neo4j", "--scope", "nope"],
    )
    assert result.exit_code == 2
    assert "Scope error" in result.output
    assert not adapter_calls


def test_cli_audit_malformed_scope_fails_fast_exit_2(monkeypatch: Any) -> None:
    """A malformed scope string fails fast before graph connection."""
    adapter_calls: list[Any] = []

    class Settings:
        @classmethod
        def model_validate(cls, _: object) -> Settings:
            return cls()

        neo4j_uri = "bolt://db:7687"
        neo4j_database = "neo4j"
        catalog_path = Path("catalog.yaml")

    monkeypatch.setattr("book_graph_rag.main.Settings", Settings)
    def _fake_adapter(settings: object) -> object:
        adapter_calls.append(settings)
        return object()

    monkeypatch.setattr("book_graph_rag.main.Neo4jAuditAdapter", _fake_adapter)

    result = CliRunner().invoke(
        cli,
        ["audit", "--target", "bookgraph-neo4j", "--scope", "knowledge/source"],
    )
    assert result.exit_code == 2
    assert "Scope error" in result.output
    assert not adapter_calls
