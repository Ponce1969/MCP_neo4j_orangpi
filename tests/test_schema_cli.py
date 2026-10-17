from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import pytest
from click.testing import CliRunner

from book_graph_rag.config import Settings as AppSettings
from book_graph_rag.domain.schema_manifest import (
    GraphSchema,
    SchemaComparisonBasis,
    SchemaDriftReport,
)
from book_graph_rag.infrastructure.neo4j_schema_reader import (
    SchemaInferenceError,
    SchemaReadResult,
)
from book_graph_rag.main import cli


def _settings() -> AppSettings:
    return AppSettings.model_validate(
        {
            "neo4j_uri": "bolt://localhost:7687",
            "neo4j_user": "neo4j",
            "neo4j_password": "not-a-real-secret",
        }
    )


def _read_result(
    state: Literal["unknown", "match", "drift"], reason: str | None = None
) -> SchemaReadResult:
    return SchemaReadResult(
        schema=GraphSchema(labels={"Document": ("title",)}),
        source="native",
        graph_version="gv-0123456789ab",
        drift=SchemaDriftReport(
            state=state,
            graph_version="gv-0123456789ab",
            reason=reason,
            comparison_basis=SchemaComparisonBasis(source="native"),
        ),
        live_schema_available=True,
    )


def _patch_cli(
    monkeypatch: pytest.MonkeyPatch,
    outcome: SchemaReadResult | BaseException,
) -> dict[str, Any]:
    from book_graph_rag import main as main_module

    settings = _settings()
    created: dict[str, Any] = {"reads": 0}

    class _SettingsFactory:
        @classmethod
        def model_validate(cls, data: object) -> AppSettings:
            return settings

    class _QueryAdapter:
        def __init__(self, configured: object) -> None:
            created["adapter_settings"] = configured

        async def close(self) -> None:
            created["closed"] = True

    class _SchemaReader:
        def __init__(self, executor: object, configured: object) -> None:
            created["reader_executor"] = executor
            created["reader_settings"] = configured

        async def read(self) -> SchemaReadResult:
            created["reads"] += 1
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    monkeypatch.setattr(main_module, "Settings", _SettingsFactory)
    monkeypatch.setattr(main_module, "readonly_settings", lambda configured: configured)
    monkeypatch.setattr(main_module, "Neo4jQueryAdapter", _QueryAdapter)
    monkeypatch.setattr(main_module, "Neo4jSchemaReader", _SchemaReader, raising=False)
    monkeypatch.setattr(main_module, "current_code_commit", lambda: "abc1234", raising=False)
    return created


@pytest.mark.parametrize(
    ("outcome", "expected_exit"),
    [
        (_read_result("match"), 0),
        (_read_result("drift"), 10),
        (_read_result("unknown", "manifest_missing"), 11),
        (SchemaInferenceError("probe failure", unreachable=True), 12),
        (RuntimeError("failed"), 13),
    ],
)
def test_schema_verify_uses_fail_closed_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
    outcome: SchemaReadResult | BaseException,
    expected_exit: int,
) -> None:
    created = _patch_cli(monkeypatch, outcome)

    result = CliRunner().invoke(cli, ["schema", "verify"])

    assert result.exit_code == expected_exit, result.output
    if isinstance(outcome, SchemaReadResult):
        payload = json.loads(result.output)
        assert payload["drift"]["comparison_basis"] == {
            "source": "native",
            "fallback_reason": None,
        }
    assert created["reads"] == 1
    assert created["closed"] is True


def test_schema_snapshot_writes_manifest_and_uses_same_reader(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    created = _patch_cli(monkeypatch, _read_result("match"))
    output_path = tmp_path / "snapshot.json"
    result = CliRunner().invoke(cli, ["schema", "snapshot", "--output", str(output_path)])

    assert result.exit_code == 0, result.output
    assert created["reads"] == 1
    assert created["closed"] is True
    content = output_path.read_bytes()
    assert content.endswith(b"\n")
    assert b"\r" not in content
    manifest = json.loads(content)
    assert manifest["schema_version"] == "1.0.0"
    assert manifest["committed_at"]
    assert manifest["code_commit"] == "abc1234"
    assert manifest["graph_version"] == "gv-0123456789ab"
    assert manifest["source"] == "native"
    assert manifest["labels"] == {"Document": ["title"]}
    assert manifest["relationship_types"] == {}
