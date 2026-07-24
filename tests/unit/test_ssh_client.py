"""Tests for SSHClient infrastructure module.

Tests the SSH connection lifecycle, command execution, timeout handling,
CancelledError propagation, reconnection logic, and health check.
All tests use mocks — no real SSH connections.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_oranpi.config import SSHConfig
from mcp_oranpi.infrastructure.ssh_client import (
    CommandResult,
    SSHClient,
    _ConnectionState,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def ssh_config() -> SSHConfig:
    """Provide a test SSH configuration."""
    return SSHConfig(
        host="test-oranpi.local",
        port=22,
        username="testuser",
        key_path="/home/testuser/.ssh/id_test",  # type: ignore
        known_hosts=None,
        security_mode="development",
        host_key_policy="accept_new",
        connect_timeout=5,
        keepalive_interval=15,
        command_timeout=10,
    )


@pytest.fixture
def ssh_client(ssh_config: SSHConfig) -> SSHClient:
    """Provide an SSHClient instance for testing."""
    return SSHClient(ssh_config)


# ── CommandResult Tests ──────────────────────────────────────────────────────


class TestCommandResult:
    """Tests for the CommandResult dataclass."""

    def test_command_result_creation(self) -> None:
        result = CommandResult(exit_code=0, stdout="OK", stderr="")
        assert result.exit_code == 0
        assert result.stdout == "OK"
        assert result.stderr == ""

    def test_command_result_nonzero_exit(self) -> None:
        result = CommandResult(exit_code=127, stdout="", stderr="command not found")
        assert result.exit_code == 127
        assert result.stderr == "command not found"

    def test_command_result_slots(self) -> None:
        result = CommandResult(exit_code=0, stdout="", stderr="")
        assert hasattr(result, "__slots__")
        assert not hasattr(result, "__dict__")

    def test_command_result_no_timed_out_field(self) -> None:
        """Correction 2: CommandResult has NO timed_out field."""
        result = CommandResult(exit_code=0, stdout="", stderr="")
        assert not hasattr(result, "timed_out")


# ── ConnectionState Tests ────────────────────────────────────────────────────


class TestConnectionState:
    """Tests for _ConnectionState enum."""

    def test_state_values(self) -> None:
        assert _ConnectionState.DISCONNECTED == "disconnected"  # type: ignore
        assert _ConnectionState.CONNECTING == "connecting"  # type: ignore
        assert _ConnectionState.CONNECTED == "connected"  # type: ignore
        assert _ConnectionState.RECONNECTING == "reconnecting"  # type: ignore

    def test_initial_state_is_disconnected(self, ssh_client: SSHClient) -> None:
        assert ssh_client.state == _ConnectionState.DISCONNECTED

    def test_initial_connected_property_is_false(self, ssh_client: SSHClient) -> None:
        assert not ssh_client.connected


# ── Connect Tests ────────────────────────────────────────────────────────────


class TestConnect:
    """Tests for SSHClient.connect()."""

    @pytest.mark.asyncio
    async def test_connect_success(self, ssh_client: SSHClient) -> None:
        mock_conn = AsyncMock()
        mock_conn.close = MagicMock()

        with patch.object(SSHClient, "_create_connection", return_value=mock_conn):
            await ssh_client.connect()

        assert ssh_client.connected
        assert ssh_client.state == _ConnectionState.CONNECTED

    @pytest.mark.asyncio
    async def test_connect_failure_sets_disconnected(
        self, ssh_client: SSHClient
    ) -> None:
        with patch.object(
            SSHClient, "_create_connection", side_effect=ConnectionError("refused")
        ), pytest.raises(ConnectionError):
            await ssh_client.connect()

        assert not ssh_client.connected
        assert ssh_client.state == _ConnectionState.DISCONNECTED

    @pytest.mark.asyncio
    async def test_connect_when_already_connected(self, ssh_client: SSHClient) -> None:
        mock_conn = AsyncMock()
        mock_conn.close = MagicMock()

        with patch.object(SSHClient, "_create_connection", return_value=mock_conn):
            await ssh_client.connect()
            # Second connect should be a no-op
            await ssh_client.connect()

        assert ssh_client.connected


# ── Disconnect Tests ─────────────────────────────────────────────────────────


class TestDisconnect:
    """Tests for SSHClient.disconnect()."""

    @pytest.mark.asyncio
    async def test_disconnect_after_connect(self, ssh_client: SSHClient) -> None:
        mock_conn = AsyncMock()
        mock_conn.close = MagicMock()

        with patch.object(SSHClient, "_create_connection", return_value=mock_conn):
            await ssh_client.connect()
        await ssh_client.disconnect()

        assert not ssh_client.connected
        assert ssh_client.state == _ConnectionState.DISCONNECTED

    @pytest.mark.asyncio
    async def test_disconnect_without_connect(self, ssh_client: SSHClient) -> None:
        # Disconnect should be safe even without a connection
        await ssh_client.disconnect()
        assert not ssh_client.connected

    @pytest.mark.asyncio
    async def test_disconnect_cancels_reconnect_task(
        self, ssh_client: SSHClient
    ) -> None:
        mock_conn = AsyncMock()
        mock_conn.close = MagicMock()

        with patch.object(SSHClient, "_create_connection", return_value=mock_conn):
            await ssh_client.connect()

        # Start a reconnection task
        await ssh_client.start_reconnection()

        # Disconnect should cancel the reconnection task
        await ssh_client.disconnect()
        assert not ssh_client.connected


# ── Execute Tests ─────────────────────────────────────────────────────────────


class TestExecute:
    """Tests for SSHClient.execute()."""

    @pytest.mark.asyncio
    async def test_execute_success(self, ssh_client: SSHClient) -> None:
        mock_conn = AsyncMock()
        mock_process = MagicMock()
        mock_process.exit_status = 0
        mock_process.stdout = "container1\ncontainer2"
        mock_process.stderr = ""
        mock_conn.run = AsyncMock(return_value=mock_process)
        mock_conn.close = MagicMock()

        with patch.object(SSHClient, "_create_connection", return_value=mock_conn):
            await ssh_client.connect()

        result = await ssh_client.execute("docker ps --format json")

        assert result.exit_code == 0
        assert "container1" in result.stdout

    @pytest.mark.asyncio
    async def test_execute_nonzero_exit_code(self, ssh_client: SSHClient) -> None:
        """Correction 4: infrastructure returns raw exit code, no interpretation."""
        mock_conn = AsyncMock()
        mock_process = MagicMock()
        mock_process.exit_status = 1
        mock_process.stdout = ""
        mock_process.stderr = "error output"
        mock_conn.run = AsyncMock(return_value=mock_process)
        mock_conn.close = MagicMock()

        with patch.object(SSHClient, "_create_connection", return_value=mock_conn):
            await ssh_client.connect()

        result = await ssh_client.execute("docker ps --format json")

        # Infrastructure returns raw result — NO interpretation
        assert result.exit_code == 1
        assert result.stderr == "error output"

    @pytest.mark.asyncio
    async def test_execute_not_connected_raises(self, ssh_client: SSHClient) -> None:
        with pytest.raises(ConnectionError, match="not connected"):
            await ssh_client.execute("echo test")

    @pytest.mark.asyncio
    async def test_execute_not_connected_reconnecting(
        self, ssh_client: SSHClient
    ) -> None:
        # Simulate reconnecting state
        ssh_client._state = _ConnectionState.RECONNECTING
        ssh_client._conn = None

        with pytest.raises(ConnectionError, match="reconnecting"):
            await ssh_client.execute("echo test")

    @pytest.mark.asyncio
    async def test_execute_timeout_propagates(self, ssh_client: SSHClient) -> None:
        """Correction 2: timeouts propagate as asyncio.TimeoutError."""

        async def slow_run(cmd: str) -> None:
            await asyncio.sleep(100)

        mock_conn = AsyncMock()
        mock_conn.run = slow_run
        mock_conn.close = MagicMock()

        with patch.object(SSHClient, "_create_connection", return_value=mock_conn):
            await ssh_client.connect()

        with pytest.raises(asyncio.TimeoutError):
            await ssh_client.execute("slow_command", timeout=1)

    @pytest.mark.asyncio
    async def test_execute_uses_config_timeout(self, ssh_client: SSHClient) -> None:
        """Default timeout comes from SSHConfig.command_timeout."""

        async def slow_run(cmd: str) -> None:
            await asyncio.sleep(100)

        mock_conn = AsyncMock()
        mock_conn.run = slow_run
        mock_conn.close = MagicMock()

        with patch.object(SSHClient, "_create_connection", return_value=mock_conn):
            await ssh_client.connect()

        # ssh_config has command_timeout=10
        with pytest.raises(asyncio.TimeoutError):
            await ssh_client.execute("slow_command")

    @pytest.mark.asyncio
    async def test_execute_cancelled_error_reraised(
        self, ssh_client: SSHClient
    ) -> None:
        """CancelledError is always re-raised (spec 006, rule 1)."""

        async def cancel_run(cmd: str) -> None:
            raise asyncio.CancelledError()

        mock_conn = AsyncMock()
        mock_conn.run = cancel_run
        mock_conn.close = MagicMock()

        with patch.object(SSHClient, "_create_connection", return_value=mock_conn):
            await ssh_client.connect()

        with pytest.raises(asyncio.CancelledError):
            await ssh_client.execute("echo test")

    @pytest.mark.asyncio
    async def test_execute_none_exit_status_treated_as_zero(
        self, ssh_client: SSHClient
    ) -> None:
        """asyncssh may return None exit_status for signal-killed processes."""
        mock_conn = AsyncMock()
        mock_process = MagicMock()
        mock_process.exit_status = None
        mock_process.stdout = "partial output"
        mock_process.stderr = ""
        mock_conn.run = AsyncMock(return_value=mock_process)
        mock_conn.close = MagicMock()

        with patch.object(SSHClient, "_create_connection", return_value=mock_conn):
            await ssh_client.connect()

        result = await ssh_client.execute("echo test")

        assert result.exit_code == 0

    @pytest.mark.asyncio
    async def test_execute_custom_timeout(self, ssh_client: SSHClient) -> None:
        """Custom timeout overrides config default."""
        mock_conn = AsyncMock()
        mock_process = MagicMock()
        mock_process.exit_status = 0
        mock_process.stdout = "OK"
        mock_process.stderr = ""
        mock_conn.run = AsyncMock(return_value=mock_process)
        mock_conn.close = MagicMock()

        with patch.object(SSHClient, "_create_connection", return_value=mock_conn):
            await ssh_client.connect()

        result = await ssh_client.execute("echo test", timeout=60)
        assert result.exit_code == 0


# ── Health Check Tests ────────────────────────────────────────────────────────


class TestHealthCheck:
    """Tests for SSHClient.health_check()."""

    @pytest.mark.asyncio
    async def test_health_check_returns_true_when_connected(
        self, ssh_client: SSHClient
    ) -> None:
        mock_conn = AsyncMock()
        mock_process = MagicMock()
        mock_process.exit_status = 0
        mock_process.stdout = "OK"
        mock_process.stderr = ""
        mock_conn.run = AsyncMock(return_value=mock_process)
        mock_conn.close = MagicMock()

        with patch.object(SSHClient, "_create_connection", return_value=mock_conn):
            await ssh_client.connect()

        assert await ssh_client.health_check() is True

    @pytest.mark.asyncio
    async def test_health_check_returns_false_when_disconnected(
        self, ssh_client: SSHClient
    ) -> None:
        assert await ssh_client.health_check() is False

    @pytest.mark.asyncio
    async def test_health_check_returns_false_on_error(
        self, ssh_client: SSHClient
    ) -> None:
        mock_conn = AsyncMock()
        mock_conn.close = MagicMock()

        with patch.object(SSHClient, "_create_connection", return_value=mock_conn):
            await ssh_client.connect()

        # Make execute raise an error
        mock_conn.run = AsyncMock(side_effect=ConnectionError("lost"))

        result = await ssh_client.health_check()
        assert result is False


# ── Reconnection Tests ────────────────────────────────────────────────────────


class TestReconnection:
    """Tests for SSHClient reconnection logic."""

    @pytest.mark.asyncio
    async def test_start_reconnection_sets_state(
        self, ssh_client: SSHClient
    ) -> None:
        mock_conn = AsyncMock()
        mock_conn.close = MagicMock()

        with patch.object(SSHClient, "_create_connection", return_value=mock_conn):
            await ssh_client.connect()

        # Disconnect first to allow reconnection
        await ssh_client.disconnect()
        assert ssh_client.state == _ConnectionState.DISCONNECTED

        # Start reconnection — but mock _create_connection to keep failing
        with patch.object(
            SSHClient,
            "_create_connection",
            side_effect=ConnectionError("refused"),
        ):
            await ssh_client.start_reconnection()

        assert ssh_client.state == _ConnectionState.RECONNECTING  # type: ignore

        # Clean up the background task
        await ssh_client.disconnect()

    @pytest.mark.asyncio
    async def test_execute_raises_during_reconnection(
        self, ssh_client: SSHClient
    ) -> None:
        ssh_client._state = _ConnectionState.RECONNECTING
        ssh_client._conn = None

        with pytest.raises(ConnectionError, match="reconnecting"):
            await ssh_client.execute("echo test")


# ── Security Mode Tests ───────────────────────────────────────────────────────


class TestSecurityMode:
    """Tests for security mode handling in _create_connection."""

    @pytest.mark.asyncio
    async def test_production_mode_uses_known_hosts(
        self, ssh_config: SSHConfig
    ) -> None:
        ssh_config.security_mode = "production"
        ssh_config.known_hosts = "/home/user/.ssh/known_hosts"  # type: ignore

        client = SSHClient(ssh_config)

        async def mock_connect(**kwargs: object) -> AsyncMock:
            return AsyncMock()

        with patch(
            "mcp_oranpi.infrastructure.ssh_client.asyncssh.connect",
            side_effect=mock_connect,
        ) as mock_connect_fn:
            await client._create_connection()

            call_kwargs = mock_connect_fn.call_args[1]
            assert call_kwargs["known_hosts"] == "/home/user/.ssh/known_hosts"

    @pytest.mark.asyncio
    async def test_development_mode_ignores_known_hosts(
        self, ssh_config: SSHConfig
    ) -> None:
        ssh_config.security_mode = "development"

        client = SSHClient(ssh_config)

        async def mock_connect(**kwargs: object) -> AsyncMock:
            return AsyncMock()

        with patch(
            "mcp_oranpi.infrastructure.ssh_client.asyncssh.connect",
            side_effect=mock_connect,
        ) as mock_connect_fn:
            await client._create_connection()

            call_kwargs = mock_connect_fn.call_args[1]
            assert call_kwargs["known_hosts"] is None