import json
from typing import Any

from click.testing import CliRunner

from book_graph_rag.main import cli


def test_audit_requires_allowlisted_target() -> None:
    result = CliRunner().invoke(cli, ["audit", "--target", "other"])
    assert result.exit_code != 0
    assert "Invalid value" in result.output


def test_audit_emits_json_before_nonzero_state(monkeypatch: Any) -> None:
    class Settings:
        @classmethod
        def model_validate(cls, _: object) -> "Settings":
            return cls()

        neo4j_uri = "bolt://db:7687"
        neo4j_user = "neo4j"
        neo4j_password = type("Secret", (), {"get_secret_value": lambda self: "secret"})()
        neo4j_database = "neo4j"

    class UseCase:
        def __init__(self, port: object) -> None:
            pass

        async def execute(self, target: object, sample_limit: int) -> Any:
            return type(
                "Report",
                (),
                {
                    "state": "violations",
                    "model_dump_json": lambda self, **_: json.dumps({"state": "violations"}),
                },
            )()

    monkeypatch.setattr("book_graph_rag.main.Settings", Settings)
    monkeypatch.setattr("book_graph_rag.main.Neo4jAuditAdapter", lambda settings: object())
    monkeypatch.setattr("book_graph_rag.main.AuditGraphUseCase", UseCase)
    result = CliRunner().invoke(cli, ["audit", "--target", "bookgraph-neo4j"])
    assert result.exit_code == 10
    assert json.loads(result.output)["state"] == "violations"
    assert "secret" not in result.output
