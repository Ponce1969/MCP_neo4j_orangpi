"""Domain error types for MCP OranPi.

All error codes follow the ``CATEGORY_SPECIFIC_ERROR`` namespace pattern.
Every tool returns a ``ToolResult`` containing either data or a ``ToolError``.
"""

from __future__ import annotations

from dataclasses import dataclass

# ── Error Code Constants ──────────────────────────────────────────────────────
# These are string constants (not enums) for MCP SDK compatibility.

# Connection
CONN_FAILED = "CONN_FAILED"
CONN_TIMEOUT = "CONN_TIMEOUT"
CONN_RECONNECTING = "CONN_RECONNECTING"

# Docker
DOCKER_UNAVAILABLE = "DOCKER_UNAVAILABLE"
DOCKER_NOT_FOUND = "DOCKER_NOT_FOUND"
DOCKER_NOT_RUNNING = "DOCKER_NOT_RUNNING"

# Network
NET_SCAN_TIMEOUT = "NET_SCAN_TIMEOUT"
NET_SCAN_RANGE_INVALID = "NET_SCAN_RANGE_INVALID"
NET_TAILSCALE_NOT_INSTALLED = "NET_TAILSCALE_NOT_INSTALLED"

# System
SYS_SERVICE_NOT_FOUND = "SYS_SERVICE_NOT_FOUND"
SYS_SENSORS_UNAVAILABLE = "SYS_SENSORS_UNAVAILABLE"

# Log
LOG_PATH_FORBIDDEN = "LOG_PATH_FORBIDDEN"
LOG_FILE_NOT_FOUND = "LOG_FILE_NOT_FOUND"
LOG_FILE_NOT_READABLE = "LOG_FILE_NOT_READABLE"
LOG_FILE_OVERSIZED = "LOG_FILE_OVERSIZED"
LOG_UNIT_NOT_FOUND = "LOG_UNIT_NOT_FOUND"

# Workspace
WS_NOT_FOUND = "WS_NOT_FOUND"
WS_DISABLED = "WS_DISABLED"
WS_UNREACHABLE = "WS_UNREACHABLE"
WS_NO_COMPOSE = "WS_NO_COMPOSE"
WS_PATH_ESCAPE = "WS_PATH_ESCAPE"

# Validation
VALID_PARAM_INVALID = "VALID_PARAM_INVALID"
VALID_PARAM_OUT_OF_RANGE = "VALID_PARAM_OUT_OF_RANGE"
VALID_PARAM_REGEX_FAIL = "VALID_PARAM_REGEX_FAIL"
VALID_PARAM_REQUIRED = "VALID_PARAM_REQUIRED"
VALID_PAYLOAD_EXCEEDED = "VALID_PAYLOAD_EXCEEDED"

# Authorization
AUTH_FORBIDDEN = "AUTH_FORBIDDEN"
AUTH_HOST_KEY_REJECTED = "AUTH_HOST_KEY_REJECTED"


# ── Error Code Namespace Prefixes ────────────────────────────────────────────

ERROR_PREFIXES: dict[str, str] = {
    "CONN_": "Connection",
    "DOCKER_": "Docker",
    "NET_": "Network",
    "SYS_": "System",
    "LOG_": "Logs",
    "WS_": "Workspace",
    "VALID_": "Validation",
    "AUTH_": "Authorization",
}


@dataclass(slots=True)
class ToolError:
    """Structured error returned by any MCP tool.

    Attributes:
        code: Namespaced error code (e.g., ``CONN_FAILED``).
        message: Human-readable explanation.
        detail: Optional structured context for debugging.
        retryable: Whether the client should retry the call.
    """

    code: str
    message: str
    detail: dict[str, object] | None = None
    retryable: bool = False


@dataclass(slots=True)
class ToolResult:
    """Unified result type for all MCP tools.

    Exactly one of ``data`` or ``error`` should be present.
    ``truncated`` indicates whether output was bounded by the
    global truncation policy.

    Attributes:
        data: Present on success, ``None`` on failure.
        error: Present on failure, ``None`` on success.
        truncated: ``True`` if output exceeded the configured limit.
    """

    data: dict[str, object] | None = None
    error: ToolError | None = None
    truncated: bool = False
