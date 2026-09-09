"""CLI tests for the book-graph-rag gate subcommand."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from click.testing import CliRunner

from book_graph_rag.domain.audit_models import (
    AuditExecution,
    AuditFinding,
    AuditReport,
    AuditTarget,
    OverallState,
    Severity,
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
        gates_policy_path = tmp_path / "gates.yaml"

    return Settings


def _write_policy(path: Path) -> None:
    path.write_text(
        """
version: 1.0.0
gates:
  - name: expose-mcp
    version: 1.0.0
    required_dimensions:
      hierarchy: pass
      endpoints: pass
      uniqueness: pass
      coverage: pass
    max_severity: blocking
""",
        encoding="utf-8",
    )


def _target() -> AuditTarget:
    return AuditTarget(
        selector="bookgraph-neo4j",
        database="neo4j",
        scheme="bolt",
        host="db",
        port=7687,
        uri="bolt://db:7687",
    )


def _report(
    state: OverallState, findings: tuple[AuditFinding, ...] = ()
) -> AuditReport:
    return AuditReport(
        target=_target(),
        state=state,
        findings=findings,
        execution=AuditExecution(state=state, exit_code={
            OverallState.PASSED: 0,
            OverallState.VIOLATIONS: 10,
            OverallState.INCOMPLETE: 11,
            OverallState.UNREACHABLE: 12,
            OverallState.FAILED: 13,
        }[state]),
    )


def test_cli_gate_passes_clean_report(monkeypatch: Any, tmp_path: Path) -> None:
    """A clean audit produces a passing gate result and exit 0."""
    _write_policy(tmp_path / "gates.yaml")
    monkeypatch.setattr("book_graph_rag.main.Settings", _fake_settings(tmp_path))
    monkeypatch.setattr("book_graph_rag.main.Neo4jAuditAdapter", lambda settings: object())

    class UseCase:
        def __init__(self, port: object) -> None:
            pass

        async def execute(
            self, target: object, sample_limit: int, scope: object | None = None
        ) -> AuditReport:
            return _report(OverallState.PASSED)

    monkeypatch.setattr("book_graph_rag.main.AuditGraphUseCase", UseCase)

    result = CliRunner().invoke(
        cli, ["gate", "expose-mcp", "--target", "bookgraph-neo4j"]
    )
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["passed"] is True
    assert parsed["overall_state"] == "passed"


def test_cli_gate_fails_blocking_audit_exit_10(monkeypatch: Any, tmp_path: Path) -> None:
    """A blocking finding produces a failing gate result and exit 10."""
    _write_policy(tmp_path / "gates.yaml")
    monkeypatch.setattr("book_graph_rag.main.Settings", _fake_settings(tmp_path))
    monkeypatch.setattr("book_graph_rag.main.Neo4jAuditAdapter", lambda settings: object())

    class UseCase:
        def __init__(self, port: object) -> None:
            pass

        async def execute(
            self, target: object, sample_limit: int, scope: object | None = None
        ) -> AuditReport:
            finding = AuditFinding(
                rule_id="HIERARCHY_CHUNK_PARENT_REQUIRED",
                category="hierarchy",
                severity=Severity.BLOCKING,
                total=1,
            )
            return _report(OverallState.VIOLATIONS, findings=(finding,))

    monkeypatch.setattr("book_graph_rag.main.AuditGraphUseCase", UseCase)

    result = CliRunner().invoke(
        cli, ["gate", "expose-mcp", "--target", "bookgraph-neo4j"]
    )
    assert result.exit_code == 10
    parsed = json.loads(result.output)
    assert parsed["passed"] is False
    assert parsed["overall_state"] == "violations"


def test_cli_gate_unknown_gate_exit_10(monkeypatch: Any, tmp_path: Path) -> None:
    """An unknown gate name fails with exit 10 and no gate evaluation occurs."""
    _write_policy(tmp_path / "gates.yaml")
    monkeypatch.setattr("book_graph_rag.main.Settings", _fake_settings(tmp_path))
    monkeypatch.setattr("book_graph_rag.main.Neo4jAuditAdapter", lambda settings: object())

    class UseCase:
        def __init__(self, port: object) -> None:
            pass

        async def execute(
            self, target: object, sample_limit: int, scope: object | None = None
        ) -> AuditReport:
            return _report(OverallState.PASSED)

    monkeypatch.setattr("book_graph_rag.main.AuditGraphUseCase", UseCase)

    result = CliRunner().invoke(
        cli, ["gate", "not-a-gate", "--target", "bookgraph-neo4j"]
    )
    assert result.exit_code == 10
    assert "Unknown gate" in result.output


def test_cli_gate_policy_load_failure_exit_2(monkeypatch: Any, tmp_path: Path) -> None:
    """A missing gates.yaml fails fast before any graph connection."""
    adapter_calls: list[Any] = []
    monkeypatch.setattr("book_graph_rag.main.Settings", _fake_settings(tmp_path))

    def _fake_adapter(settings: object) -> object:
        adapter_calls.append(settings)
        return object()

    monkeypatch.setattr("book_graph_rag.main.Neo4jAuditAdapter", _fake_adapter)

    result = CliRunner().invoke(
        cli, ["gate", "expose-mcp", "--target", "bookgraph-neo4j"]
    )
    assert result.exit_code == 2
    assert "Gate policy error" in result.output
    assert not adapter_calls


def test_cli_gate_unknown_scope_fails_fast_exit_2(monkeypatch: Any, tmp_path: Path) -> None:
    """An invalid --scope fails before building the Neo4j adapter."""
    _write_policy(tmp_path / "gates.yaml")
    adapter_calls: list[Any] = []
    monkeypatch.setattr("book_graph_rag.main.Settings", _fake_settings(tmp_path))

    def _fake_adapter(settings: object) -> object:
        adapter_calls.append(settings)
        return object()

    monkeypatch.setattr("book_graph_rag.main.Neo4jAuditAdapter", _fake_adapter)

    result = CliRunner().invoke(
        cli,
        ["gate", "expose-mcp", "--target", "bookgraph-neo4j", "--scope", "nope"],
    )
    assert result.exit_code == 2
    assert "Scope error" in result.output
    assert not adapter_calls


def test_cli_gate_writes_output_file(monkeypatch: Any, tmp_path: Path) -> None:
    """The --output option persists the gate result JSON."""
    _write_policy(tmp_path / "gates.yaml")
    monkeypatch.setattr("book_graph_rag.main.Settings", _fake_settings(tmp_path))
    monkeypatch.setattr("book_graph_rag.main.Neo4jAuditAdapter", lambda settings: object())

    class UseCase:
        def __init__(self, port: object) -> None:
            pass

        async def execute(
            self, target: object, sample_limit: int, scope: object | None = None
        ) -> AuditReport:
            return _report(OverallState.PASSED)

    monkeypatch.setattr("book_graph_rag.main.AuditGraphUseCase", UseCase)

    output_path = tmp_path / "gate.json"
    result = CliRunner().invoke(
        cli,
        ["gate", "expose-mcp", "--target", "bookgraph-neo4j", "--output", str(output_path)],
    )
    assert result.exit_code == 0
    assert output_path.exists()
    parsed = json.loads(output_path.read_text(encoding="utf-8"))
    assert parsed["passed"] is True
