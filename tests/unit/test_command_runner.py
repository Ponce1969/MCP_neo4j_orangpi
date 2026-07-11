"""Tests for CommandRunner infrastructure module.

Tests the ALLOWED_COMMANDS whitelist, command building, parameter
validation, workspace execution, and error handling.
"""

from __future__ import annotations

import asyncio

import pytest

from mcp_oranpi.domain.validation import ValidationError
from mcp_oranpi.infrastructure.command_runner import ALLOWED_COMMANDS, CommandRunner
from mcp_oranpi.infrastructure.ssh_client import CommandResult
from tests.helpers import MockSSHClient

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_ssh() -> MockSSHClient:
    """Provide a MockSSHClient for CommandRunner tests."""
    return MockSSHClient()


@pytest.fixture
def runner(mock_ssh: MockSSHClient) -> CommandRunner:
    """Provide a CommandRunner with a mock SSHClient."""
    return CommandRunner(mock_ssh)


# ── ALLOWED_COMMANDS Tests ────────────────────────────────────────────────────


class TestAllowedCommands:
    """Tests for the ALLOWED_COMMANDS whitelist."""

    def test_docker_commands_present(self) -> None:
        docker_keys = [
            "docker_ps",
            "docker_ps_all",
            "docker_inspect",
            "docker_logs",
            "docker_stats",
            "docker_port",
        ]
        for key in docker_keys:
            assert key in ALLOWED_COMMANDS

    def test_network_commands_present(self) -> None:
        network_keys = ["ss_tulnp", "tailscale_status"]
        for key in network_keys:
            assert key in ALLOWED_COMMANDS

    def test_system_commands_present(self) -> None:
        system_keys = [
            "top_bn1",
            "free_m",
            "df_h",
            "vcgencmd_measure_temp",
            "systemctl_status",
            "systemctl_list",
            "hostname",
            "uptime",
        ]
        for key in system_keys:
            assert key in ALLOWED_COMMANDS

    def test_log_commands_present(self) -> None:
        assert "journalctl" in ALLOWED_COMMANDS

    def test_compose_commands_present(self) -> None:
        compose_keys = [
            "compose_ps",
            "compose_config",
            "compose_logs",
            "compose_services",
        ]
        for key in compose_keys:
            assert key in ALLOWED_COMMANDS

    def test_all_commands_are_templates(self) -> None:
        """All command values must be non-empty strings (templates)."""
        for key, value in ALLOWED_COMMANDS.items():
            assert isinstance(value, str), f"{key} is not a string"
            assert len(value) > 0, f"{key} is empty"

    def test_no_raw_input_allowed(self) -> None:
        """Commands should NOT contain user-input placeholders like {0}.

        Note: stat -c %s uses %s as a stat(1) format specifier, NOT a Python
        format placeholder. The exclusion below accounts for this.
        """
        for key, value in ALLOWED_COMMANDS.items():
            assert "{0}" not in value, f"{key} uses positional formatting"
            # Allow stat -c format specifiers (%s, %Y, %n, etc.) which are
            # shell-level format strings, not Python user-input placeholders.
            if key != "stat_size":
                assert "%s" not in value, f"{key} uses % formatting"


# ── Build Command Tests ───────────────────────────────────────────────────────


class TestBuildCommand:
    """Tests for CommandRunner.build_command()."""

    def test_simple_command_no_params(self, runner: CommandRunner) -> None:
        cmd = runner.build_command("docker_ps")
        assert cmd == "docker ps --format json"

    def test_command_with_container(self, runner: CommandRunner) -> None:
        cmd = runner.build_command("docker_inspect", container="nginx")
        assert cmd == "docker inspect --format json nginx"

    def test_command_with_tail(self, runner: CommandRunner) -> None:
        cmd = runner.build_command("docker_logs", container="app", tail=50)
        assert "app" in cmd
        assert "--tail 50" in cmd

    def test_unknown_command_raises(self, runner: CommandRunner) -> None:
        with pytest.raises(ValueError, match="Unknown command key"):
            runner.build_command("rm_rf_slash")

    def test_unexpected_parameter_raises(self, runner: CommandRunner) -> None:
        with pytest.raises(ValueError, match="Unexpected parameter"):
            runner.build_command("docker_ps", bogus="value")

    def test_invalid_container_name_raises(self, runner: CommandRunner) -> None:
        """Container names with special characters are rejected."""
        with pytest.raises(ValidationError):
            runner.build_command("docker_inspect", container="; rm -rf /")

    def test_invalid_tail_value_raises(self, runner: CommandRunner) -> None:
        """Tail values outside 1-500 range are rejected."""
        with pytest.raises(ValidationError):
            runner.build_command("docker_logs", container="app", tail=999)

    def test_compose_command_no_params(self, runner: CommandRunner) -> None:
        cmd = runner.build_command("compose_ps")
        assert cmd == "docker compose ps --format json"

    def test_systemctl_status_with_service(self, runner: CommandRunner) -> None:
        cmd = runner.build_command("systemctl_status", service="nginx")
        assert cmd == "systemctl status nginx"

    def test_journalctl_with_unit(self, runner: CommandRunner) -> None:
        cmd = runner.build_command("journalctl", unit="docker", lines=100)
        assert "-u docker" in cmd
        assert "-n 100" in cmd


# ── Run Tests ──────────────────────────────────────────────────────────────────


class TestRun:
    """Tests for CommandRunner.run()."""

    @pytest.mark.asyncio
    async def test_run_simple_command(self, runner: CommandRunner, mock_ssh: MockSSHClient) -> None:
        mock_ssh.set_default_result(
            CommandResult(exit_code=0, stdout='{"name":"container1"}', stderr="")
        )

        result = await runner.run("docker_ps")

        assert result.exit_code == 0
        assert "container1" in result.stdout

    @pytest.mark.asyncio
    async def test_run_unknown_command_raises(
        self, runner: CommandRunner, mock_ssh: MockSSHClient
    ) -> None:
        with pytest.raises(ValueError, match="Unknown command key"):
            await runner.run("arbitrary_command")

    @pytest.mark.asyncio
    async def test_run_with_params(self, runner: CommandRunner, mock_ssh: MockSSHClient) -> None:
        mock_ssh.set_default_result(
            CommandResult(exit_code=0, stdout="logs output", stderr="")
        )

        result = await runner.run("docker_logs", container="nginx", tail=10)

        assert result.exit_code == 0
        # Verify the command was sent with correct parameters
        assert "docker logs" in mock_ssh.last_command
        assert "nginx" in mock_ssh.last_command
        assert "--tail 10" in mock_ssh.last_command

    @pytest.mark.asyncio
    async def test_run_propagates_timeout_error(
        self, runner: CommandRunner, mock_ssh: MockSSHClient
    ) -> None:
        """Correction 2: timeouts propagate as asyncio.TimeoutError."""
        mock_ssh.set_execute_error(TimeoutError())

        with pytest.raises(asyncio.TimeoutError):
            await runner.run("docker_ps")

    @pytest.mark.asyncio
    async def test_run_propagates_connection_error(
        self, runner: CommandRunner, mock_ssh: MockSSHClient
    ) -> None:
        mock_ssh.set_execute_error(ConnectionError("not connected"))

        with pytest.raises(ConnectionError):
            await runner.run("docker_ps")


# ── Run In Workspace Tests ───────────────────────────────────────────────────


class TestRunInWorkspace:
    """Tests for CommandRunner.run_in_workspace().

    Correction 1: cwd wrapping belongs to CommandRunner, NOT SSHClient.
    """

    @pytest.mark.asyncio
    async def test_run_in_workspace_wraps_with_cd(
        self, runner: CommandRunner, mock_ssh: MockSSHClient
    ) -> None:
        mock_ssh.set_default_result(
            CommandResult(exit_code=0, stdout="compose output", stderr="")
        )

        result = await runner.run_in_workspace(
            "compose_ps", "/home/testuser/codigo/guardian"
        )

        assert result.exit_code == 0
        # Verify cd wrapper
        assert mock_ssh.last_command.startswith("cd /home/testuser/codigo/guardian && ")

    @pytest.mark.asyncio
    async def test_run_in_workspace_preserves_command(
        self, runner: CommandRunner, mock_ssh: MockSSHClient
    ) -> None:
        mock_ssh.set_default_result(
            CommandResult(exit_code=0, stdout="", stderr="")
        )

        await runner.run_in_workspace(
            "compose_ps", "/home/testuser/codigo/guardian"
        )

        assert "docker compose ps --format json" in mock_ssh.last_command

    @pytest.mark.asyncio
    async def test_run_in_workspace_with_params(
        self, runner: CommandRunner, mock_ssh: MockSSHClient
    ) -> None:
        mock_ssh.set_default_result(
            CommandResult(exit_code=0, stdout="logs output", stderr="")
        )

        await runner.run_in_workspace(
            "compose_logs",
            "/home/testuser/codigo/guardian",
            service="web",
            tail=20,
        )

        cmd = mock_ssh.last_command
        assert "cd /home/testuser/codigo/guardian &&" in cmd
        assert "docker compose logs" in cmd
        assert "web" in cmd
        assert "--tail 20" in cmd

    @pytest.mark.asyncio
    async def test_run_in_workspace_unknown_command_raises(
        self, runner: CommandRunner, mock_ssh: MockSSHClient
    ) -> None:
        with pytest.raises(ValueError, match="Unknown command key"):
            await runner.run_in_workspace("rm_rf", "/home/testuser/codigo")

    @pytest.mark.asyncio
    async def test_run_in_workspace_propagates_timeout(
        self, runner: CommandRunner, mock_ssh: MockSSHClient
    ) -> None:
        mock_ssh.set_execute_error(TimeoutError())

        with pytest.raises(asyncio.TimeoutError):
            await runner.run_in_workspace(
                "compose_ps", "/home/testuser/codigo/guardian"
            )


# ── Exit Code Interpretation Tests ────────────────────────────────────────────


class TestExitCodeInterpretation:
    """Correction 4: infrastructure returns raw data, application interprets."""

    @pytest.mark.asyncio
    async def test_nonzero_exit_code_returned_as_is(
        self, runner: CommandRunner, mock_ssh: MockSSHClient
    ) -> None:
        """Infrastructure does NOT interpret exit codes."""
        mock_ssh.set_default_result(
            CommandResult(exit_code=126, stdout="", stderr="permission denied")
        )

        result = await runner.run("docker_inspect", container="nonexistent")

        # Infrastructure returns raw exit code — NO interpretation
        assert result.exit_code == 126
        assert result.stderr == "permission denied"

    @pytest.mark.asyncio
    async def test_command_not_found_exit_code(
        self, runner: CommandRunner, mock_ssh: MockSSHClient
    ) -> None:
        """Command not found (127) is raw data, not DOCKER_UNAVAILABLE."""
        mock_ssh.set_default_result(
            CommandResult(exit_code=127, stdout="", stderr="not found")
        )

        result = await runner.run("docker_ps")

        # Infrastructure does NOT map this to DOCKER_UNAVAILABLE
        assert result.exit_code == 127