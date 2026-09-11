"""CLI tests for book-graph-rag gate --gate expose-mcp-readiness (Slice C, T-C.6)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from book_graph_rag.domain.audit_models import (
    AuditExecution,
    AuditFinding,
    AuditReport,
    AuditTarget,
    OverallState,
)
from book_graph_rag.domain.evaluation_models import (
    LayerMetricValue,
    LayerRunMetadata,
    LayerStatus,
    ReadinessGateResult,
    ReadinessLayerStatus,
)
from book_graph_rag.main import cli


def _fake_settings(tmp_path: Path) -> type:
    class Settings:
        @classmethod
        def model_validate(cls, _: object) -> Settings:
            return cls()

        neo4j_uri = "bolt://db:7687"
        neo4j_database = "neoj"
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
readiness_gates:
  - name: expose-mcp-readiness
    version: 1.0.0
    required_layers:
      - layer: structure
        blocking: true
      - layer: resolution
        blocking: true
      - layer: generation
        blocking: true
    optional_layers:
      - layer: extraction
        blocking: false
      - layer: retrieval
        blocking: false
    audit_gate_ref: expose-mcp
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
        execution=AuditExecution(
            state=state,
            exit_code={
                OverallState.PASSED: 0,
                OverallState.VIOLATIONS: 10,
                OverallState.INCOMPLETE: 11,
                OverallState.UNREACHABLE: 12,
                OverallState.FAILED: 13,
            }[state],
        ),
    )


def _readiness_result(*, exit_code: int, overall_state: str) -> ReadinessGateResult:
    return ReadinessGateResult(
        schema_version="1.0.0",
        gate_name="expose-mcp-readiness",
        gate_version="1.0.0",
        scope=None,
        passed=overall_state == "passed",
        overall_state=overall_state,  # type: ignore[arg-type]
        exit_code=exit_code,
        rationale=f"readiness gate {overall_state}",
        audit_gate_status="passed",
        evaluation_status="passed",
        layer_breakdown=(
            ReadinessLayerStatus(
                layer="structure",
                status=LayerStatus.PASSED,
                rationale="audit passed",
                measured=(LayerMetricValue(name="dimension", value=0.0),),
            ),
            ReadinessLayerStatus(
                layer="resolution",
                status=LayerStatus.PASSED,
                rationale="resolution passed",
                measured=(LayerMetricValue(name="f1", value=0.8, threshold=0.7),),
            ),
            ReadinessLayerStatus(
                layer="generation",
                status=LayerStatus.PASSED,
                rationale="generation passed",
                measured=(
                    LayerMetricValue(name="faithfulness", value=0.8, threshold=0.7),
                ),
            ),
        ),
        run_metadata=LayerRunMetadata(run_id="run-1", code_commit="abc123"),
    )


class FakeReadinessGateEvaluatorUseCase:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def execute(
        self,
        gate_name: str,
        audit_report: AuditReport,
        scope: str | None = None,
    ) -> ReadinessGateResult:
        self.calls.append({"gate_name": gate_name, "audit_report": audit_report, "scope": scope})
        return _readiness_result(exit_code=0, overall_state="passed")


@pytest.fixture
def readiness_use_case(monkeypatch: pytest.MonkeyPatch) -> FakeReadinessGateEvaluatorUseCase:
    fake = FakeReadinessGateEvaluatorUseCase()
    monkeypatch.setattr(
        "book_graph_rag.main._build_readiness_gate_evaluator_use_case",
        lambda _settings, _policy: fake,
    )
    return fake


def test_gate_readiness_per_layer_breakdown(
    monkeypatch: pytest.MonkeyPatch,
    readiness_use_case: FakeReadinessGateEvaluatorUseCase,
    tmp_path: Path,
) -> None:
    """gate expose-mcp-readiness prints JSON ReadinessGateResult with layer breakdown."""
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
        cli, ["gate", "expose-mcp-readiness", "--target", "bookgraph-neo4j"]
    )
    assert result.exit_code == 0
    parsed = json.loads(result.stdout)
    assert parsed["gate_name"] == "expose-mcp-readiness"
    assert parsed["overall_state"] == "passed"
    breakdown = parsed["layer_breakdown"]
    assert len(breakdown) == 3
    assert {layer["layer"] for layer in breakdown} == {"structure", "resolution", "generation"}
    assert readiness_use_case.calls
    assert readiness_use_case.calls[0]["gate_name"] == "expose-mcp-readiness"


def test_gate_readiness_exit_code_propagated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The readiness gate CLI exits with the use case's exit_code."""
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

    class FailingReadinessUseCase:
        async def execute(
            self,
            gate_name: str,
            audit_report: AuditReport,
            scope: str | None = None,
        ) -> ReadinessGateResult:
            return _readiness_result(exit_code=11, overall_state="incomplete")

    monkeypatch.setattr(
        "book_graph_rag.main._build_readiness_gate_evaluator_use_case",
        lambda _settings, _policy: FailingReadinessUseCase(),
    )

    result = CliRunner().invoke(
        cli, ["gate", "expose-mcp-readiness", "--target", "bookgraph-neo4j"]
    )
    assert result.exit_code == 11


def test_gate_unknown_readiness_name_fails_with_exit_10(
    monkeypatch: pytest.MonkeyPatch,
    readiness_use_case: FakeReadinessGateEvaluatorUseCase,
    tmp_path: Path,
) -> None:
    """An unknown gate name fails with exit 10 when a readiness gate exists in policy."""
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


def test_gate_audit_only_path_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    readiness_use_case: FakeReadinessGateEvaluatorUseCase,
    tmp_path: Path,
) -> None:
    """gate expose-mcp still uses the audit-only evaluator and returns a GateResult."""
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
    parsed = json.loads(result.stdout)
    assert "layer_breakdown" not in parsed
    assert parsed["passed"] is True
    assert parsed["overall_state"] == "passed"
    assert not readiness_use_case.calls
