"""Unit tests for domain error codes and ToolResult."""

from __future__ import annotations

from mcp_oranpi.domain.errors import (
    AUTH_FORBIDDEN,
    AUTH_HOST_KEY_REJECTED,
    CONN_FAILED,
    CONN_RECONNECTING,
    CONN_TIMEOUT,
    DOCKER_NOT_FOUND,
    DOCKER_NOT_RUNNING,
    DOCKER_UNAVAILABLE,
    ERROR_PREFIXES,
    LOG_FILE_NOT_FOUND,
    LOG_FILE_NOT_READABLE,
    LOG_FILE_OVERSIZED,
    LOG_PATH_FORBIDDEN,
    LOG_UNIT_NOT_FOUND,
    NET_SCAN_RANGE_INVALID,
    NET_SCAN_TIMEOUT,
    NET_TAILSCALE_NOT_INSTALLED,
    SYS_SENSORS_UNAVAILABLE,
    SYS_SERVICE_NOT_FOUND,
    VALID_PARAM_INVALID,
    VALID_PARAM_OUT_OF_RANGE,
    VALID_PARAM_REGEX_FAIL,
    VALID_PARAM_REQUIRED,
    VALID_PAYLOAD_EXCEEDED,
    WS_DISABLED,
    WS_NO_COMPOSE,
    WS_NOT_FOUND,
    WS_PATH_ESCAPE,
    WS_UNREACHABLE,
    ToolError,
    ToolResult,
)


class TestErrorCodeConstants:
    """Verify all error code constants exist and follow the namespace pattern."""

    def test_connection_codes(self) -> None:
        """Connection error codes use CONN_ prefix."""
        assert CONN_FAILED == "CONN_FAILED"
        assert CONN_TIMEOUT == "CONN_TIMEOUT"
        assert CONN_RECONNECTING == "CONN_RECONNECTING"

    def test_docker_codes(self) -> None:
        """Docker error codes use DOCKER_ prefix."""
        assert DOCKER_UNAVAILABLE == "DOCKER_UNAVAILABLE"
        assert DOCKER_NOT_FOUND == "DOCKER_NOT_FOUND"
        assert DOCKER_NOT_RUNNING == "DOCKER_NOT_RUNNING"

    def test_network_codes(self) -> None:
        """Network error codes use NET_ prefix."""
        assert NET_SCAN_TIMEOUT == "NET_SCAN_TIMEOUT"
        assert NET_SCAN_RANGE_INVALID == "NET_SCAN_RANGE_INVALID"
        assert NET_TAILSCALE_NOT_INSTALLED == "NET_TAILSCALE_NOT_INSTALLED"

    def test_system_codes(self) -> None:
        """System error codes use SYS_ prefix."""
        assert SYS_SERVICE_NOT_FOUND == "SYS_SERVICE_NOT_FOUND"
        assert SYS_SENSORS_UNAVAILABLE == "SYS_SENSORS_UNAVAILABLE"

    def test_log_codes(self) -> None:
        """Log error codes use LOG_ prefix."""
        assert LOG_PATH_FORBIDDEN == "LOG_PATH_FORBIDDEN"
        assert LOG_FILE_NOT_FOUND == "LOG_FILE_NOT_FOUND"
        assert LOG_FILE_NOT_READABLE == "LOG_FILE_NOT_READABLE"
        assert LOG_FILE_OVERSIZED == "LOG_FILE_OVERSIZED"
        assert LOG_UNIT_NOT_FOUND == "LOG_UNIT_NOT_FOUND"

    def test_workspace_codes(self) -> None:
        """Workspace error codes use WS_ prefix."""
        assert WS_NOT_FOUND == "WS_NOT_FOUND"
        assert WS_DISABLED == "WS_DISABLED"
        assert WS_UNREACHABLE == "WS_UNREACHABLE"
        assert WS_NO_COMPOSE == "WS_NO_COMPOSE"
        assert WS_PATH_ESCAPE == "WS_PATH_ESCAPE"

    def test_validation_codes(self) -> None:
        """Validation error codes use VALID_ prefix."""
        assert VALID_PARAM_INVALID == "VALID_PARAM_INVALID"
        assert VALID_PARAM_OUT_OF_RANGE == "VALID_PARAM_OUT_OF_RANGE"
        assert VALID_PARAM_REGEX_FAIL == "VALID_PARAM_REGEX_FAIL"
        assert VALID_PARAM_REQUIRED == "VALID_PARAM_REQUIRED"
        assert VALID_PAYLOAD_EXCEEDED == "VALID_PAYLOAD_EXCEEDED"

    def test_auth_codes(self) -> None:
        """Auth error codes use AUTH_ prefix."""
        assert AUTH_FORBIDDEN == "AUTH_FORBIDDEN"
        assert AUTH_HOST_KEY_REJECTED == "AUTH_HOST_KEY_REJECTED"

    def test_error_prefixes(self) -> None:
        """ERROR_PREFIXES covers all expected namespace prefixes."""
        expected_prefixes = {
            "CONN_",
            "DOCKER_",
            "NET_",
            "SYS_",
            "LOG_",
            "WS_",
            "VALID_",
            "AUTH_",
        }
        assert set(ERROR_PREFIXES.keys()) == expected_prefixes


class TestToolError:
    """Tests for ToolError construction."""

    def test_error_with_all_fields(self) -> None:
        """ToolError can be created with all fields."""
        error = ToolError(
            code=CONN_FAILED,
            message="SSH connection failed",
            detail={"host": "oranpi.local"},
            retryable=False,
        )
        assert error.code == "CONN_FAILED"
        assert error.message == "SSH connection failed"
        assert error.detail == {"host": "oranpi.local"}
        assert error.retryable is False

    def test_error_with_defaults(self) -> None:
        """ToolError defaults detail to None and retryable to False."""
        error = ToolError(
            code=DOCKER_NOT_FOUND,
            message="Container not found",
        )
        assert error.detail is None
        assert error.retryable is False

    def test_retryable_error(self) -> None:
        """CONN_RECONNECTING is retryable."""
        error = ToolError(
            code=CONN_RECONNECTING,
            message="Reconnecting in background",
            retryable=True,
        )
        assert error.retryable is True


class TestToolResult:
    """Tests for ToolResult construction."""

    def test_success_result(self) -> None:
        """ToolResult can represent success with data."""
        result = ToolResult(
            data={"containers": []},
            error=None,
            truncated=False,
        )
        assert result.data == {"containers": []}
        assert result.error is None
        assert result.truncated is False

    def test_error_result(self) -> None:
        """ToolResult can represent failure with an error."""
        error = ToolError(
            code=CONN_FAILED,
            message="Connection failed",
        )
        result = ToolResult(
            data=None,
            error=error,
            truncated=False,
        )
        assert result.data is None
        assert result.error.code == "CONN_FAILED"
        assert result.truncated is False

    def test_truncated_result(self) -> None:
        """ToolResult can represent truncated output."""
        result = ToolResult(
            data={"logs": "truncated content"},
            error=None,
            truncated=True,
        )
        assert result.truncated is True