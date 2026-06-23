"""Tests for deployment artifacts (T-07.9)."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def deploy_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "deploy"


@pytest.fixture
def env_example() -> Path:
    return Path(__file__).resolve().parents[1] / ".env.example"


def test_systemd_service_file_exists(deploy_dir: Path) -> None:
    """deploy/mcp-server.service is created with the unit file."""
    service_path = deploy_dir / "mcp-server.service"

    assert service_path.exists()


def test_systemd_service_file_contains_required_directives(deploy_dir: Path) -> None:
    """The service file contains the expected systemd directives and paths."""
    service_path = deploy_dir / "mcp-server.service"
    content = service_path.read_text(encoding="utf-8")
    repo_path = "/home/bookgraph/Gonzalo_codigo/Mcp_libro/MCP_neo4j_orangpi"

    assert "[Unit]" in content
    assert "Description=Book Graph RAG MCP Server" in content
    assert "After=network.target docker.service" in content
    assert "[Service]" in content
    assert "ExecStart=/home/bookgraph/.local/bin/uv run book-graph-rag-mcp serve" in content
    assert f"EnvironmentFile={repo_path}/.env" in content
    assert f"WorkingDirectory={repo_path}" in content
    assert "Restart=on-failure" in content
    assert "[Install]" in content
    assert "WantedBy=multi-user.target" in content


def test_deploy_readme_exists(deploy_dir: Path) -> None:
    """deploy/README.md is created with deployment instructions."""
    readme_path = deploy_dir / "README.md"

    assert readme_path.exists()


def test_deploy_readme_contains_key_steps(deploy_dir: Path) -> None:
    """README.md documents copy, reload, enable, start and status steps."""
    readme_path = deploy_dir / "README.md"
    content = readme_path.read_text(encoding="utf-8")

    assert "mcp-server.service" in content
    assert "systemctl daemon-reload" in content
    assert "systemctl enable mcp-server" in content
    assert "systemctl start mcp-server" in content
    assert "systemctl status mcp-server" in content
    assert "journalctl -u mcp-server -f" in content


def test_env_example_includes_mcp_settings(env_example: Path) -> None:
    """.env.example documents the MCP settings added in Fase 07."""
    content = env_example.read_text(encoding="utf-8")

    assert "MCP_PORT=8003" in content
    assert "MCP_LOG_PATH=logs/mcp_queries.jsonl" in content
    assert "MCP_LOG_RETENTION_DAYS=7" in content
