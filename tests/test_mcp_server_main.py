"""Tests for the MCP server CompositionRoot and entry point (REQ-07.4, REQ-07.5)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from click.testing import CliRunner

from book_graph_rag.config import Settings
from book_graph_rag.mcp_server_main import main, mcp_cli


class _FakeNeo4jQueryAdapter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FakeJsonFileQueryLoggerAdapter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FakeMcpServerAdapter:
    def __init__(self, query_port: Any, query_logger: Any) -> None:
        self.query_port = query_port
        self.query_logger = query_logger
        self.run_sse_mock = AsyncMock()

    async def run_sse(self, host: str = "0.0.0.0", port: int = 8003) -> None:
        await self.run_sse_mock(host=host, port=port)

    def create_server(self) -> MagicMock:
        return MagicMock()


@pytest.fixture
def fake_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    for key in ("NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    settings = Settings(
        neo4j_uri="bolt://localhost:7687",
        neo4j_user="neo4j",
        neo4j_password="fake-password",  # pragma: allowlist secret
    )
    monkeypatch.setattr("book_graph_rag.mcp_server_main.Settings", lambda: settings)
    return settings


@pytest.fixture
def fake_adapters(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    created: dict[str, Any] = {}

    class _FakeNeo4jQueryAdapterTracked(_FakeNeo4jQueryAdapter):
        def __init__(self, settings: Settings) -> None:
            super().__init__(settings)
            created["query_adapter"] = self

    class _FakeJsonLoggerTracked(_FakeJsonFileQueryLoggerAdapter):
        def __init__(self, settings: Settings) -> None:
            super().__init__(settings)
            created["query_logger"] = self

    class _FakeMcpServerAdapterTracked(_FakeMcpServerAdapter):
        def __init__(self, query_port: Any, query_logger: Any) -> None:
            super().__init__(query_port, query_logger)
            created["server_adapter"] = self

    monkeypatch.setattr(
        "book_graph_rag.mcp_server_main.Neo4jQueryAdapter", _FakeNeo4jQueryAdapterTracked
    )
    monkeypatch.setattr(
        "book_graph_rag.mcp_server_main.JsonFileQueryLoggerAdapter",
        _FakeJsonLoggerTracked,
    )
    monkeypatch.setattr(
        "book_graph_rag.mcp_server_main.McpServerAdapter", _FakeMcpServerAdapterTracked
    )
    return created


def test_main_entrypoint_is_callable() -> None:
    """main() is the script entrypoint for the MCP console command."""
    assert callable(main)


def test_mcp_cli_exposes_serve_command() -> None:
    """The CLI group exposes the 'serve' subcommand."""
    runner = CliRunner()
    result = runner.invoke(mcp_cli, ["--help"])

    assert result.exit_code == 0
    assert "serve" in result.output


def test_serve_command_starts_server(
    fake_settings: Settings, fake_adapters: dict[str, Any]
) -> None:
    """The serve command wires components and starts the SSE server."""
    runner = CliRunner()
    result = runner.invoke(mcp_cli, ["serve"])

    assert result.exit_code == 0, result.output
    assert "MCP server starting on port 8003" in result.output
    server_adapter = fake_adapters["server_adapter"]
    server_adapter.run_sse_mock.assert_awaited_once_with(host="0.0.0.0", port=8003)


def test_composition_root_creates_components_in_order(
    fake_settings: Settings, fake_adapters: dict[str, Any]
) -> None:
    """CompositionRoot instantiates adapters in the correct order and wires them."""
    runner = CliRunner()
    runner.invoke(mcp_cli, ["serve"])

    query_adapter = fake_adapters["query_adapter"]
    query_logger = fake_adapters["query_logger"]
    server_adapter = fake_adapters["server_adapter"]

    assert server_adapter.query_port is query_adapter
    assert server_adapter.query_logger is query_logger


def test_lifecycle_closes_driver_and_logger_on_shutdown(
    fake_settings: Settings, fake_adapters: dict[str, Any]
) -> None:
    """Driver and logger are closed when the server shuts down."""
    runner = CliRunner()
    result = runner.invoke(mcp_cli, ["serve"])

    assert result.exit_code == 0
    assert fake_adapters["query_adapter"].closed is True
    assert fake_adapters["query_logger"].closed is True


def test_serve_uses_custom_mcp_port(
    fake_settings: Settings,
    fake_adapters: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Custom MCP_PORT is passed to the SSE server."""
    fake_settings.mcp_port = 9000
    monkeypatch.setattr("book_graph_rag.mcp_server_main.Settings", lambda: fake_settings)

    runner = CliRunner()
    result = runner.invoke(mcp_cli, ["serve"])

    assert result.exit_code == 0, result.output
    assert "MCP server starting on port 9000" in result.output
    fake_adapters["server_adapter"].run_sse_mock.assert_awaited_once_with(
        host="0.0.0.0", port=9000
    )


def test_serve_fails_fast_on_missing_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing required env vars cause a clear, non-zero exit."""
    monkeypatch.chdir(tmp_path)
    for key in ("NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD"):
        monkeypatch.delenv(key, raising=False)

    runner = CliRunner()
    result = runner.invoke(mcp_cli, ["serve"])

    assert result.exit_code == 1
    assert "Configuration error:" in result.output
    assert "Traceback" not in result.output


async def test_neo4j_connection_failure_raises_clear_error(
    fake_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neo4j driver creation failure propagates with a clear error."""

    class _FailingNeo4jQueryAdapter:
        def __init__(self, settings: Settings) -> None:
            raise ConnectionError("Neo4j connection failed: wrong credentials")

    monkeypatch.setattr(
        "book_graph_rag.mcp_server_main.Neo4jQueryAdapter", _FailingNeo4jQueryAdapter
    )

    from book_graph_rag.mcp_server_main import _run_server

    with pytest.raises(ConnectionError, match="Neo4j connection failed"):
        await _run_server(fake_settings)


async def test_logger_creation_uses_settings(
    fake_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """JsonFileQueryLoggerAdapter is constructed from the loaded Settings."""
    captured: list[Settings] = []

    class _CapturingJsonLogger(_FakeJsonFileQueryLoggerAdapter):
        def __init__(self, settings: Settings) -> None:
            super().__init__(settings)
            captured.append(settings)

    monkeypatch.setattr(
        "book_graph_rag.mcp_server_main.JsonFileQueryLoggerAdapter", _CapturingJsonLogger
    )
    monkeypatch.setattr(
        "book_graph_rag.mcp_server_main.McpServerAdapter", _FakeMcpServerAdapter
    )

    from book_graph_rag.mcp_server_main import _run_server

    await _run_server(fake_settings)

    assert len(captured) == 1
    assert captured[0].mcp_log_path == fake_settings.mcp_log_path
    assert captured[0].mcp_log_retention_days == fake_settings.mcp_log_retention_days


def test_serve_catches_runtime_error_and_exits_two(
    fake_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runtime errors from the server produce a clean non-zero exit."""

    async def failing_run_server(_settings: Settings) -> None:
        raise RuntimeError("unexpected failure")

    monkeypatch.setattr("book_graph_rag.mcp_server_main._run_server", failing_run_server)

    runner = CliRunner()
    result = runner.invoke(mcp_cli, ["serve"])

    assert result.exit_code == 2
    assert "MCP server error: unexpected failure" in result.output
