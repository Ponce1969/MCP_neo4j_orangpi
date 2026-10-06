"""Tests for LogsTools in application/logs_tools.py.

Uses MockCommandRunner to test log tools without SSH.
"""

from __future__ import annotations

import pytest

from mcp_oranpi.application.logs_tools import LogsTools
from mcp_oranpi.domain.errors import (
    CONN_TIMEOUT,
    DOCKER_NOT_FOUND,
    LOG_FILE_NOT_FOUND,
    LOG_FILE_NOT_READABLE,
    LOG_FILE_OVERSIZED,
    LOG_PATH_FORBIDDEN,
)
from mcp_oranpi.infrastructure.ssh_client import CommandResult
from tests.helpers import MockCommandRunner


@pytest.fixture
def mock_runner() -> MockCommandRunner:
    """Provide a MockCommandRunner for LogsTools tests."""
    return MockCommandRunner()


@pytest.fixture
def allowed_dirs() -> list[str]:
    """Provide allowed log directories for testing."""
    return ["/var/log", "/home", "/opt"]


@pytest.fixture
def logs_tools(mock_runner: MockCommandRunner, allowed_dirs: list[str]) -> LogsTools:
    """Provide a LogsTools instance with mock runner and config."""
    return LogsTools(mock_runner, allowed_log_dirs=allowed_dirs, max_log_file_mb=50)


class TestLogsDocker:
    """Tests for logs_docker."""

    async def test_success(self, logs_tools: LogsTools) -> None:
        stdout = "2024-01-15 10:30:00 App started\n2024-01-15 10:30:01 Request received"
        logs_tools._runner.set_response(  # type: ignore
            "docker_logs",
            CommandResult(exit_code=0, stdout=stdout, stderr=""),
        )

        result = await logs_tools.logs_docker("nginx", tail=100)

        assert result.error is None
        assert result.data is not None
        assert result.data["container"] == "nginx"
        assert result.data["log_count"] == 2

    async def test_container_not_found(self, logs_tools: LogsTools) -> None:
        logs_tools._runner.set_response(  # type: ignore
            "docker_logs",
            CommandResult(
                exit_code=1,
                stdout="",
                stderr="Error: No such container: nonexistent",
            ),
        )

        result = await logs_tools.logs_docker("nonexistent")

        assert result.error is not None
        assert result.error.code == DOCKER_NOT_FOUND

    async def test_invalid_container_name(self, logs_tools: LogsTools) -> None:
        result = await logs_tools.logs_docker("; rm -rf /")

        assert result.error is not None
        assert result.error.code == "VALID_PARAM_INVALID"


class TestLogsSystemd:
    """Tests for logs_systemd."""

    async def test_success_with_unit(self, logs_tools: LogsTools) -> None:
        stdout = (
            "Jan 15 10:30:00 nginx[1234]: Started\nJan 15 10:30:01 nginx[1234]: Request processed"
        )
        logs_tools._runner.set_response(  # type: ignore
            "journalctl",
            CommandResult(exit_code=0, stdout=stdout, stderr=""),
        )

        result = await logs_tools.logs_systemd(unit="nginx", tail=100)

        assert result.error is None
        assert result.data is not None
        assert result.data["unit"] == "nginx"
        assert result.data["log_count"] == 2

    async def test_success_without_unit(self, logs_tools: LogsTools) -> None:
        stdout = "Jan 15 10:30:00 systemd[1]: Started some service"
        logs_tools._runner.set_response(  # type: ignore
            "journalctl",
            CommandResult(exit_code=0, stdout=stdout, stderr=""),
        )

        result = await logs_tools.logs_systemd(tail=100)

        assert result.error is None
        assert result.data is not None
        assert result.data["unit"] is None

    async def test_with_priority(self, logs_tools: LogsTools) -> None:
        stdout = "Jan 15 10:30:00 nginx[1234]: Error occurred"
        logs_tools._runner.set_response(  # type: ignore
            "journalctl",
            CommandResult(exit_code=0, stdout=stdout, stderr=""),
        )

        result = await logs_tools.logs_systemd(unit="nginx", priority="err", tail=50)

        assert result.error is None
        last_call = logs_tools._runner.last_call()  # type: ignore
        assert last_call is not None
        assert "priority" in last_call[1]

    async def test_with_until(self, logs_tools: LogsTools) -> None:
        stdout = "Jan 15 10:30:00 nginx[1234]: Error occurred"
        logs_tools._runner.set_response(  # type: ignore
            "journalctl",
            CommandResult(exit_code=0, stdout=stdout, stderr=""),
        )

        result = await logs_tools.logs_systemd(
            unit="nginx", since="2 hours ago", until="1 hour ago"
        )

        assert result.error is None
        last_call = logs_tools._runner.last_call()  # type: ignore
        assert last_call is not None
        assert last_call[1]["since"] == "2 hours ago"
        assert last_call[1]["until"] == "1 hour ago"

    async def test_unit_not_found(self, logs_tools: LogsTools) -> None:
        logs_tools._runner.set_response(  # type: ignore
            "journalctl",
            CommandResult(
                exit_code=4,
                stdout="",
                stderr="Unit not-found.service could not be found",
            ),
        )

        result = await logs_tools.logs_systemd(unit="not-found")

        assert result.error is not None
        assert result.error.code == "LOG_UNIT_NOT_FOUND"

    async def test_query_failure_is_surfaced(self, logs_tools: LogsTools) -> None:
        # A rejected timestamp exits 1 with empty stdout and a stderr message;
        # it must not be reported as an empty success.
        logs_tools._runner.set_response(  # type: ignore
            "journalctl",
            CommandResult(
                exit_code=1,
                stdout="",
                stderr="Failed to parse timestamp: '2",
            ),
        )

        result = await logs_tools.logs_systemd(unit="mcp-server", since="2 hours ago")

        assert result.error is not None
        assert result.error.code == "LOG_QUERY_FAILED"
        assert "Failed to parse timestamp" in result.error.message

    async def test_no_entries_marker_is_still_a_success(self, logs_tools: LogsTools) -> None:
        logs_tools._runner.set_response(  # type: ignore
            "journalctl",
            CommandResult(exit_code=1, stdout="-- No entries --\n", stderr=""),
        )

        result = await logs_tools.logs_systemd(unit="mcp-server")

        assert result.error is None
        assert result.data is not None
        assert result.data["log_count"] == 0


class TestLogsFile:
    """Tests for logs_file - the 6-step security sequence."""

    async def test_success(self, logs_tools: LogsTools) -> None:
        # Step 1: path validation passes
        # Step 2: stat_size
        logs_tools._runner.set_response(  # type: ignore
            "stat_size",
            CommandResult(exit_code=0, stdout="12345", stderr=""),
        )
        # Step 3: readlink (no symlink)
        logs_tools._runner.set_response(  # type: ignore
            "readlink",
            CommandResult(exit_code=0, stdout="/var/log/app.log", stderr=""),
        )
        # Step 5: tail_file
        logs_tools._runner.set_response(  # type: ignore
            "tail_file",
            CommandResult(
                exit_code=0,
                stdout="2024-01-15 10:30:00 Server started\n2024-01-15 10:30:01 Request received",
                stderr="",
            ),
        )

        result = await logs_tools.logs_file("/var/log/app.log", tail=1000)

        assert result.error is None
        assert result.data is not None
        assert result.data["path"] == "/var/log/app.log"
        assert result.data["size_bytes"] == 12345

    async def test_forbidden_path(self, logs_tools: LogsTools) -> None:
        result = await logs_tools.logs_file("/etc/shadow", tail=100)

        assert result.error is not None
        assert result.error.code == LOG_PATH_FORBIDDEN

    async def test_path_with_traversal(self, logs_tools: LogsTools) -> None:
        result = await logs_tools.logs_file("/var/log/../../../etc/passwd", tail=100)

        assert result.error is not None
        assert result.error.code == LOG_PATH_FORBIDDEN

    async def test_file_oversized(self, logs_tools: LogsTools) -> None:
        # 100MB file (exceeds 50MB default)
        logs_tools._runner.set_response(  # type: ignore
            "stat_size",
            CommandResult(exit_code=0, stdout="104857600", stderr=""),
        )

        result = await logs_tools.logs_file("/var/log/huge.log", tail=1000)

        assert result.error is not None
        assert result.error.code == LOG_FILE_OVERSIZED

    async def test_symlink_escape(self, logs_tools: LogsTools) -> None:
        # Original path is valid
        # But symlink resolves outside allowed dirs
        logs_tools._runner.set_response(  # type: ignore
            "stat_size",
            CommandResult(exit_code=0, stdout="1000", stderr=""),
        )
        logs_tools._runner.set_response(  # type: ignore
            "readlink",
            CommandResult(exit_code=0, stdout="/etc/passwd", stderr=""),
        )

        result = await logs_tools.logs_file("/var/log/link", tail=1000)

        assert result.error is not None
        assert result.error.code == LOG_PATH_FORBIDDEN

    async def test_file_not_found(self, logs_tools: LogsTools) -> None:
        logs_tools._runner.set_response(  # type: ignore
            "stat_size",
            CommandResult(exit_code=0, stdout="1000", stderr=""),
        )
        logs_tools._runner.set_response(  # type: ignore
            "readlink",
            CommandResult(exit_code=0, stdout="/var/log/app.log", stderr=""),
        )
        logs_tools._runner.set_response(  # type: ignore
            "tail_file",
            CommandResult(
                exit_code=1,
                stdout="",
                stderr="cat: /var/log/app.log: No such file",
            ),
        )

        result = await logs_tools.logs_file("/var/log/app.log", tail=1000)

        assert result.error is not None
        assert result.error.code == LOG_FILE_NOT_FOUND

    async def test_binary_file(self, logs_tools: LogsTools) -> None:
        logs_tools._runner.set_response(  # type: ignore
            "stat_size",
            CommandResult(exit_code=0, stdout="1000", stderr=""),
        )
        logs_tools._runner.set_response(  # type: ignore
            "readlink",
            CommandResult(exit_code=0, stdout="/var/log/app.log", stderr=""),
        )
        # Content with null bytes (binary indicator)
        binary_content = "\x00\x01\x02" + "text content" * 100
        logs_tools._runner.set_response(  # type: ignore
            "tail_file",
            CommandResult(exit_code=0, stdout=binary_content, stderr=""),
        )

        result = await logs_tools.logs_file("/var/log/app.log", tail=1000)

        assert result.error is not None
        assert result.error.code == LOG_FILE_NOT_READABLE

    async def test_stat_size_timeout(
        self, logs_tools: LogsTools, mock_runner: MockCommandRunner
    ) -> None:
        async def timeout_run(*args: object, **kwargs: object) -> CommandResult:
            raise TimeoutError()

        mock_runner.run = timeout_run  # type: ignore

        result = await logs_tools.logs_file("/var/log/app.log", tail=1000)

        assert result.error is not None
        assert result.error.code == CONN_TIMEOUT

    async def test_tail_file_timeout(self, logs_tools: LogsTools) -> None:
        logs_tools._runner.set_response(  # type: ignore
            "stat_size",
            CommandResult(exit_code=0, stdout="1000", stderr=""),
        )
        logs_tools._runner.set_response(  # type: ignore
            "readlink",
            CommandResult(exit_code=0, stdout="/var/log/app.log", stderr=""),
        )

        async def timeout_run(*args: object, **kwargs: object) -> CommandResult:
            raise TimeoutError()

        logs_tools._runner.run = timeout_run  # type: ignore

        result = await logs_tools.logs_file("/var/log/app.log", tail=1000)

        assert result.error is not None
        assert result.error.code == CONN_TIMEOUT
