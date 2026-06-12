"""Shared test fixtures for MCP OranPi."""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_oranpi.config import AppConfig, SSHConfig
from tests.helpers import MockSSHClient


@pytest.fixture
def project_root() -> Path:
    """Return the project root directory."""
    return Path(__file__).resolve().parents[2]


@pytest.fixture
def mock_env_vars(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Provide a minimal set of required environment variables for testing."""
    env = {
        "ORANPI_SSH_HOST": "test-oranpi.local",
        "ORANPI_SSH_USER": "testuser",
        "ORANPI_SSH_KEY_PATH": "/home/testuser/.ssh/id_test",
        "ORANPI_ROOT_WORKSPACE_DIR": "/home/testuser/codigo",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env


@pytest.fixture
def app_config(mock_env_vars: dict[str, str]) -> AppConfig:
    """Provide an AppConfig loaded from mock environment variables."""
    return AppConfig()


@pytest.fixture
def ssh_config(app_config: AppConfig) -> SSHConfig:
    """Provide an SSHConfig derived from the test AppConfig."""
    return app_config.ssh_config


# ── SSH Client Mocks ──────────────────────────────────────────────────────────


@pytest.fixture
def mock_ssh_client() -> MockSSHClient:
    """Provide a MockSSHClient for unit tests."""
    return MockSSHClient()


# ── Test Workspace Config ─────────────────────────────────────────────────────


@pytest.fixture
def workspace_yaml(tmp_path: Path) -> Path:
    """Provide a temporary workspaces.yaml file for testing."""
    yaml_content = """workspaces:
  guardian:
    path: /home/testuser/codigo/guardian
  crm:
    path: /home/testuser/codigo/crm
  lab:
    path: /home/testuser/codigo/lab
"""
    ws_file = tmp_path / "workspaces.yaml"
    ws_file.write_text(yaml_content, encoding="utf-8")
    return ws_file


@pytest.fixture
def app_config_with_workspaces(
    mock_env_vars: dict[str, str],
    workspace_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AppConfig:
    """Provide an AppConfig with workspace config pointing to test YAML."""
    monkeypatch.setenv("ORANPI_WORKSPACE_CONFIG", str(workspace_yaml))
    return AppConfig()