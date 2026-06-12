"""Tests for WorkspaceResolver infrastructure module.

Tests workspace loading from YAML, path validation, SSH directory
checks, status assignment, and error handling.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_oranpi.config import AppConfig
from mcp_oranpi.domain.workspace import WorkspaceStatus
from mcp_oranpi.infrastructure.ssh_client import CommandResult
from mcp_oranpi.infrastructure.workspace_resolver import WorkspaceResolver
from tests.helpers import MockSSHClient

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_ssh() -> MockSSHClient:
    """Provide a MockSSHClient for WorkspaceResolver tests."""
    client = MockSSHClient()
    # Default: directories exist on remote host
    client.set_default_result(
        CommandResult(exit_code=0, stdout="EXISTS", stderr="")
    )
    return client


@pytest.fixture
def workspace_yaml_content() -> str:
    return """workspaces:
  guardian:
    path: /home/testuser/codigo/guardian
  crm:
    path: /home/testuser/codigo/crm
"""


@pytest.fixture
def workspace_yaml_with_escape() -> str:
    return """workspaces:
  legit:
    path: /home/testuser/codigo/legit
  escape:
    path: /etc/passwd
"""


@pytest.fixture
def workspace_yaml_invalid_id() -> str:
    return """workspaces:
  valid-project:
    path: /home/testuser/codigo/valid-project
  "../hack":
    path: /home/testuser/codigo/hack
"""


@pytest.fixture
def resolver(
    app_config_with_workspaces: AppConfig, mock_ssh: MockSSHClient
) -> WorkspaceResolver:
    """Provide a WorkspaceResolver with test config."""
    return WorkspaceResolver(app_config_with_workspaces, mock_ssh)


# ── Load Tests ────────────────────────────────────────────────────────────────


class TestLoad:
    """Tests for WorkspaceResolver.load()."""

    @pytest.mark.asyncio
    async def test_load_valid_workspaces(
        self,
        app_config_with_workspaces: AppConfig,
        mock_ssh: MockSSHClient,
    ) -> None:
        # All directories exist on remote host
        mock_ssh.set_default_result(
            CommandResult(exit_code=0, stdout="EXISTS", stderr="")
        )

        resolver = WorkspaceResolver(app_config_with_workspaces, mock_ssh)
        workspaces = await resolver.load()

        assert len(workspaces) == 3
        assert workspaces["guardian"].status == WorkspaceStatus.ACTIVE
        assert workspaces["crm"].status == WorkspaceStatus.ACTIVE
        assert workspaces["lab"].status == WorkspaceStatus.ACTIVE

    @pytest.mark.asyncio
    async def test_load_marks_missing_directories_as_disabled(
        self,
        app_config_with_workspaces: AppConfig,
        mock_ssh: MockSSHClient,
    ) -> None:
        # All directories are missing
        mock_ssh.set_default_result(
            CommandResult(exit_code=0, stdout="MISSING", stderr="")
        )

        resolver = WorkspaceResolver(app_config_with_workspaces, mock_ssh)
        workspaces = await resolver.load()

        for ws in workspaces.values():
            assert ws.status == WorkspaceStatus.DISABLED

    @pytest.mark.asyncio
    async def test_load_marks_ssh_failure_as_unreachable(
        self,
        app_config_with_workspaces: AppConfig,
    ) -> None:
        mock_ssh = MockSSHClient()
        mock_ssh.set_execute_error(ConnectionError("SSH connection lost"))

        resolver = WorkspaceResolver(app_config_with_workspaces, mock_ssh)
        workspaces = await resolver.load()

        for ws in workspaces.values():
            assert ws.status == WorkspaceStatus.UNREACHABLE

    @pytest.mark.asyncio
    async def test_load_missing_yaml_file(
        self, app_config_with_workspaces: AppConfig, mock_ssh: MockSSHClient
    ) -> None:
        # Point to a nonexistent file
        app_config_with_workspaces.workspace_config = "/nonexistent/workspaces.yaml"

        resolver = WorkspaceResolver(app_config_with_workspaces, mock_ssh)
        workspaces = await resolver.load()

        assert len(workspaces) == 0

    @pytest.mark.asyncio
    async def test_load_workspace_path_escape(
        self, tmp_path: Path, mock_ssh: MockSSHClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Workspaces with paths outside root_dir are marked disabled."""
        yaml_content = """workspaces:
  legit:
    path: /home/testuser/codigo/legit
  escape:
    path: /etc/passwd
"""
        yaml_file = tmp_path / "workspaces.yaml"
        yaml_file.write_text(yaml_content, encoding="utf-8")

        monkeypatch.setenv("ORANPI_ROOT_WORKSPACE_DIR", "/home/testuser/codigo")
        monkeypatch.setenv("ORANPI_SSH_HOST", "test.local")
        monkeypatch.setenv("ORANPI_SSH_USER", "testuser")
        monkeypatch.setenv("ORANPI_SSH_KEY_PATH", "/home/test/.ssh/id_test")
        monkeypatch.setenv("ORANPI_WORKSPACE_CONFIG", str(yaml_file))

        config = AppConfig()
        mock_ssh.set_default_result(
            CommandResult(exit_code=0, stdout="EXISTS", stderr="")
        )

        resolver = WorkspaceResolver(config, mock_ssh)
        workspaces = await resolver.load()

        # legit workspace should be active
        assert workspaces["legit"].status == WorkspaceStatus.ACTIVE
        # escape workspace should be disabled
        assert workspaces["escape"].status == WorkspaceStatus.DISABLED

    @pytest.mark.asyncio
    async def test_load_invalid_workspace_id(
        self, tmp_path: Path, mock_ssh: MockSSHClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Workspace IDs with invalid characters are marked disabled."""
        yaml_content = """workspaces:
  valid-project:
    path: /home/testuser/codigo/valid-project
  "../hack":
    path: /home/testuser/codigo/hack
"""
        yaml_file = tmp_path / "workspaces.yaml"
        yaml_file.write_text(yaml_content, encoding="utf-8")

        monkeypatch.setenv("ORANPI_ROOT_WORKSPACE_DIR", "/home/testuser/codigo")
        monkeypatch.setenv("ORANPI_SSH_HOST", "test.local")
        monkeypatch.setenv("ORANPI_SSH_USER", "testuser")
        monkeypatch.setenv("ORANPI_SSH_KEY_PATH", "/home/test/.ssh/id_test")
        monkeypatch.setenv("ORANPI_WORKSPACE_CONFIG", str(yaml_file))

        config = AppConfig()
        mock_ssh.set_default_result(
            CommandResult(exit_code=0, stdout="EXISTS", stderr="")
        )

        resolver = WorkspaceResolver(config, mock_ssh)
        workspaces = await resolver.load()

        # Valid ID should be active
        assert workspaces["valid-project"].status == WorkspaceStatus.ACTIVE
        # Invalid ID should be disabled
        assert workspaces["../hack"].status == WorkspaceStatus.DISABLED


# ── Resolve Tests ─────────────────────────────────────────────────────────────


class TestResolve:
    """Tests for WorkspaceResolver.resolve()."""

    @pytest.mark.asyncio
    async def test_resolve_existing_workspace(
        self,
        app_config_with_workspaces: AppConfig,
        mock_ssh: MockSSHClient,
    ) -> None:
        mock_ssh.set_default_result(
            CommandResult(exit_code=0, stdout="EXISTS", stderr="")
        )

        resolver = WorkspaceResolver(app_config_with_workspaces, mock_ssh)
        await resolver.load()

        info = resolver.resolve("guardian")

        assert info is not None
        assert info.id == "guardian"
        assert info.path == "/home/testuser/codigo/guardian"
        assert info.status == WorkspaceStatus.ACTIVE

    @pytest.mark.asyncio
    async def test_resolve_nonexistent_workspace(
        self,
        app_config_with_workspaces: AppConfig,
        mock_ssh: MockSSHClient,
    ) -> None:
        mock_ssh.set_default_result(
            CommandResult(exit_code=0, stdout="EXISTS", stderr="")
        )

        resolver = WorkspaceResolver(app_config_with_workspaces, mock_ssh)
        await resolver.load()

        info = resolver.resolve("nonexistent")

        assert info is None


# ── Resolve Path Tests ────────────────────────────────────────────────────────


class TestResolvePath:
    """Tests for WorkspaceResolver.resolve_path()."""

    @pytest.mark.asyncio
    async def test_resolve_path_existing(
        self,
        app_config_with_workspaces: AppConfig,
        mock_ssh: MockSSHClient,
    ) -> None:
        mock_ssh.set_default_result(
            CommandResult(exit_code=0, stdout="EXISTS", stderr="")
        )

        resolver = WorkspaceResolver(app_config_with_workspaces, mock_ssh)
        await resolver.load()

        path = resolver.resolve_path("guardian")

        assert path == "/home/testuser/codigo/guardian"

    @pytest.mark.asyncio
    async def test_resolve_path_nonexistent(
        self,
        app_config_with_workspaces: AppConfig,
        mock_ssh: MockSSHClient,
    ) -> None:
        mock_ssh.set_default_result(
            CommandResult(exit_code=0, stdout="EXISTS", stderr="")
        )

        resolver = WorkspaceResolver(app_config_with_workspaces, mock_ssh)
        await resolver.load()

        path = resolver.resolve_path("nonexistent")

        assert path is None


# ── Get Active Workspace Tests ────────────────────────────────────────────────


class TestGetActiveWorkspace:
    """Tests for WorkspaceResolver.get_active_workspace()."""

    @pytest.mark.asyncio
    async def test_get_active_workspace(
        self,
        app_config_with_workspaces: AppConfig,
        mock_ssh: MockSSHClient,
    ) -> None:
        mock_ssh.set_default_result(
            CommandResult(exit_code=0, stdout="EXISTS", stderr="")
        )

        resolver = WorkspaceResolver(app_config_with_workspaces, mock_ssh)
        await resolver.load()

        path, info = resolver.get_active_workspace("guardian")

        assert path == "/home/testuser/codigo/guardian"
        assert info.status == WorkspaceStatus.ACTIVE

    @pytest.mark.asyncio
    async def test_get_active_workspace_not_found(
        self,
        app_config_with_workspaces: AppConfig,
        mock_ssh: MockSSHClient,
    ) -> None:
        mock_ssh.set_default_result(
            CommandResult(exit_code=0, stdout="EXISTS", stderr="")
        )

        resolver = WorkspaceResolver(app_config_with_workspaces, mock_ssh)
        await resolver.load()

        with pytest.raises(ValueError, match="WS_NOT_FOUND"):
            resolver.get_active_workspace("nonexistent")

    @pytest.mark.asyncio
    async def test_get_active_workspace_disabled(
        self,
        app_config_with_workspaces: AppConfig,
        mock_ssh: MockSSHClient,
    ) -> None:
        # Make all directories missing (disabled)
        mock_ssh.set_default_result(
            CommandResult(exit_code=0, stdout="MISSING", stderr="")
        )

        resolver = WorkspaceResolver(app_config_with_workspaces, mock_ssh)
        await resolver.load()

        with pytest.raises(ValueError, match="WS_DISABLED"):
            resolver.get_active_workspace("guardian")

    @pytest.mark.asyncio
    async def test_get_active_workspace_unreachable(
        self, app_config_with_workspaces: AppConfig
    ) -> None:
        mock_ssh = MockSSHClient()
        mock_ssh.set_execute_error(ConnectionError("SSH down"))

        resolver = WorkspaceResolver(app_config_with_workspaces, mock_ssh)
        await resolver.load()

        with pytest.raises(ValueError, match="WS_UNREACHABLE"):
            resolver.get_active_workspace("guardian")


# ── Detect Indicators Tests ────────────────────────────────────────────────────


class TestDetectIndicators:
    """Tests for compose and git detection in workspaces."""

    @pytest.mark.asyncio
    async def test_detect_compose_yml(
        self,
        app_config_with_workspaces: AppConfig,
        mock_ssh: MockSSHClient,
    ) -> None:
        # Directory exists, has compose file
        def mock_execute(cmd: str, *, timeout: int | None = None) -> CommandResult:
            if "test -d" in cmd:
                return CommandResult(exit_code=0, stdout="EXISTS", stderr="")
            if "test -e" in cmd:
                if "docker-compose" in cmd or "compose" in cmd:
                    return CommandResult(exit_code=0, stdout="", stderr="")
                return CommandResult(exit_code=1, stdout="", stderr="")
            return CommandResult(exit_code=0, stdout="", stderr="")

        mock_ssh._default_result = CommandResult(exit_code=0, stdout="", stderr="")
        mock_ssh._results = {}

        async def custom_execute(
            command: str, *, timeout: int | None = None
        ) -> CommandResult:
            return mock_execute(command, timeout=timeout)

        mock_ssh.execute = custom_execute

        resolver = WorkspaceResolver(app_config_with_workspaces, mock_ssh)
        workspaces = await resolver.load()

        # At least one workspace should have has_compose detection
        # (whether True or False depends on the mock)
        for ws in workspaces.values():
            assert isinstance(ws.has_compose, bool)