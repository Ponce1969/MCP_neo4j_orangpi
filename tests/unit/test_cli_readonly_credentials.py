from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner
from pydantic import SecretStr

from book_graph_rag.config import Settings
from book_graph_rag.main import cli


def _install_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    settings = Settings.model_validate(
        {
            "neo4j_uri": "bolt://localhost:7687",
            "neo4j_user": "writer",
            "neo4j_password": SecretStr("writer-password"),
            "neo4j_readonly_user": "  reader  ",
            "neo4j_readonly_password": SecretStr("  short-read-password  "),
            # The validate command calls validate_llm_provider_settings explicitly, so the fake
            # settings must satisfy it; the other commands never look at these.
            "query_llm_base_url": "https://api.deepseek.com/v1",
            "query_llm_model_name": "deepseek-v4-flash",
            "query_llm_api_key": SecretStr("x" * 32),
        }
    )

    class SettingsProvider:
        @classmethod
        def model_validate(cls, _data: object) -> Settings:
            return settings

    monkeypatch.setattr("book_graph_rag.main.Settings", SettingsProvider)
    return settings


class _FakeAdapter:
    def __init__(self, settings: Settings, captured: list[Settings]) -> None:
        captured.append(settings)

    async def close(self) -> None:
        return None


class _FakeAuditUseCase:
    def __init__(self, _adapter: object) -> None:
        pass

    async def execute(self, *_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(
            state="passed",
            execution=SimpleNamespace(exit_code=0),
            model_dump_json=lambda **_kwargs: "{}",
        )


def test_audit_uses_readonly_credential_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_settings(monkeypatch)
    captured: list[Settings] = []
    monkeypatch.setattr(
        "book_graph_rag.main.Neo4jAuditAdapter",
        lambda settings: _FakeAdapter(settings, captured),
    )
    monkeypatch.setattr("book_graph_rag.main.AuditGraphUseCase", _FakeAuditUseCase)

    result = CliRunner().invoke(cli, ["audit", "--target", "bookgraph-neo4j"])

    assert result.exit_code == 0, result.output
    assert captured[0].neo4j_user == "reader"
    assert captured[0].neo4j_password.get_secret_value() == "short-read-password"


def test_gate_uses_readonly_credential_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_settings(monkeypatch)
    captured: list[Settings] = []
    monkeypatch.setattr(
        "book_graph_rag.main.Neo4jAuditAdapter",
        lambda settings: _FakeAdapter(settings, captured),
    )
    monkeypatch.setattr("book_graph_rag.main.AuditGraphUseCase", _FakeAuditUseCase)

    class GatePolicyLoader:
        def __init__(self, _path: Path) -> None:
            pass

        def load(self) -> Any:
            return SimpleNamespace(readiness_gates=())

    class GateEvaluator:
        def __init__(self, _policy: Any) -> None:
            pass

        def evaluate(self, _name: str, _report: Any) -> Any:
            return SimpleNamespace(exit_code=0, model_dump_json=lambda **_kwargs: "{}")

    monkeypatch.setattr("book_graph_rag.main.GatePolicyLoader", GatePolicyLoader)
    monkeypatch.setattr("book_graph_rag.main.GateEvaluatorUseCase", GateEvaluator)

    result = CliRunner().invoke(cli, ["gate", "expose-mcp", "--target", "bookgraph-neo4j"])

    assert result.exit_code == 0, result.output
    assert captured[0].neo4j_user == "reader"
    assert captured[0].neo4j_password.get_secret_value() == "short-read-password"


def test_evaluate_uses_readonly_credential_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_settings(monkeypatch)
    captured: list[Settings] = []

    class EvaluateUseCase:
        async def execute(self, *, layer: str) -> tuple[Any, int]:
            return SimpleNamespace(model_dump_json=lambda **_kwargs: "{}"), 0

    def build_evaluate_use_case(settings: Settings) -> EvaluateUseCase:
        captured.append(settings)
        return EvaluateUseCase()

    monkeypatch.setattr(
        "book_graph_rag.main._build_evaluate_command_use_case", build_evaluate_use_case
    )

    result = CliRunner().invoke(cli, ["evaluate", "--layer", "retrieval", "--json-only"])

    assert result.exit_code == 0, result.output
    assert captured[0].neo4j_user == "reader"
    assert captured[0].neo4j_password.get_secret_value() == "short-read-password"


def test_query_uses_readonly_credential_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The query command reads the graph; it must not hold the writer credential."""
    _install_settings(monkeypatch)
    captured: list[Settings] = []
    monkeypatch.setattr(
        "book_graph_rag.main.Neo4jQueryAdapter",
        lambda settings: _FakeAdapter(settings, captured),
    )

    class QueryUseCase:
        def __init__(self, port: object) -> None:
            pass

        async def execute(self, _query: object) -> Any:
            # The command echoes ``result.model_dump_json()``, so the fake must look like the model.
            return SimpleNamespace(model_dump_json=lambda **_kwargs: "{}")

    monkeypatch.setattr("book_graph_rag.main.QueryKnowledgeGraphUseCase", QueryUseCase)

    result = CliRunner().invoke(cli, ["query", "--type", "entity", "--query", '{"name": "ReAct"}'])

    assert result.exit_code == 0, result.output
    assert captured[0].neo4j_user == "reader"
    assert captured[0].neo4j_password.get_secret_value() == "short-read-password"


def test_validate_uses_readonly_credential_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-reindex validation is documented read-only; it must read with the read credential."""
    _install_settings(monkeypatch)
    captured: list[Settings] = []

    class ValidateUseCase:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def execute(self, **_kwargs: Any) -> tuple[Any, Any]:
            # The command unpacks ``(bundle, policy)`` and exits with ``policy.exit_code``.
            return (
                SimpleNamespace(model_dump_json=lambda **_kwargs: "{}"),
                SimpleNamespace(exit_code=0),
            )

    monkeypatch.setattr(
        "book_graph_rag.main.Neo4jValidationAdapter",
        lambda settings: _FakeAdapter(settings, captured),
    )
    monkeypatch.setattr(
        "book_graph_rag.main.Neo4jQueryAdapter",
        lambda settings: _FakeAdapter(settings, captured),
    )
    monkeypatch.setattr("book_graph_rag.main.ValidateGraphUseCase", ValidateUseCase)

    result = CliRunner().invoke(
        cli, ["validate", "--book-id", "knowledge:agentic-architectural-patterns"]
    )

    assert result.exit_code == 0, result.output
    assert captured, "both adapters are constructed with the settings"
    assert {settings.neo4j_user for settings in captured} == {"reader"}
