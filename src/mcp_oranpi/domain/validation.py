"""Parameter validation functions for MCP OranPi.

This module contains ONLY pure domain validation logic — no I/O,
no SSH, no MCP dependencies. Every tool's input parameters must
pass through these validators before reaching the application layer.

All validators raise ``ValidationError`` on failure rather than
returning booleans, so callers get a clear message for error responses.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from mcp_oranpi.domain.errors import (
    VALID_PARAM_INVALID,
    VALID_PARAM_OUT_OF_RANGE,
    VALID_PARAM_REQUIRED,
    WS_PATH_ESCAPE,
)

# ── Validation Patterns ──────────────────────────────────────────────────────
# These are the canonical regex patterns shared with workspace.py and
# any future modules that need to validate identifiers.

_WORKSPACE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_CONTAINER_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]+$")
_SERVICE_NAME_RE = re.compile(r"^[a-zA-Z0-9_.-]+$")
_JOURNAL_PRIORITY_RE = re.compile(
    r"^(emerg|alert|crit|err|warning|notice|info|debug)$",
    re.IGNORECASE,
)


class ValidationError(Exception):
    """Raised when a tool parameter fails validation.

    Carries the same namespace-prefixed error codes that the MCP
    handler returns to the caller, making it trivial to map
    validation failures to structured ``ToolError`` responses.
    """

    def __init__(self, code: str, message: str, field: str | None = None) -> None:
        self.code = code
        self.field = field
        super().__init__(message)


# ── Identifier Validators ────────────────────────────────────────────────────


def validate_workspace_id(workspace_id: str) -> str:
    """Validate a workspace identifier.

    Workspace IDs must match ``[a-zA-Z0-9_-]+``.
    Rejects path traversal, spaces, and special characters.

    Raises:
        ValidationError: If the ID is empty or contains disallowed chars.
    """
    if not workspace_id:
        raise ValidationError(
            VALID_PARAM_REQUIRED,
            "Workspace ID is required",
            field="workspace_id",
        )
    if not _WORKSPACE_ID_RE.match(workspace_id):
        raise ValidationError(
            VALID_PARAM_INVALID,
            f"Invalid workspace ID: {workspace_id!r}. "
            "Must match [a-zA-Z0-9_-]+",
            field="workspace_id",
        )
    return workspace_id


def validate_container_name(name: str) -> str:
    """Validate a Docker container name.

    Container names must start with a letter or digit and contain
    only letters, digits, underscores, dots, and hyphens.

    Raises:
        ValidationError: If the name is empty or contains disallowed chars.
    """
    if not name:
        raise ValidationError(
            VALID_PARAM_REQUIRED,
            "Container name is required",
            field="container_name",
        )
    if not _CONTAINER_NAME_RE.match(name):
        raise ValidationError(
            VALID_PARAM_INVALID,
            f"Invalid container name: {name!r}. "
            "Must match [a-zA-Z0-9][a-zA-Z0-9_.-]+",
            field="container_name",
        )
    return name


def validate_service_name(name: str) -> str:
    """Validate a systemd service name.

    Service names must match ``[a-zA-Z0-9_.-]+``.

    Raises:
        ValidationError: If the name is empty or contains disallowed chars.
    """
    if not name:
        raise ValidationError(
            VALID_PARAM_REQUIRED,
            "Service name is required",
            field="service_name",
        )
    if not _SERVICE_NAME_RE.match(name):
        raise ValidationError(
            VALID_PARAM_INVALID,
            f"Invalid service name: {name!r}. "
            "Must match [a-zA-Z0-9_.-]+",
            field="service_name",
        )
    return name


# ── Range / Literal Validators ───────────────────────────────────────────────


def validate_port(port: int) -> int:
    """Validate a network port number.

    Ports must be in the range 1–65535.

    Raises:
        ValidationError: If the port is out of range.
    """
    if port < 1 or port > 65535:
        raise ValidationError(
            VALID_PARAM_OUT_OF_RANGE,
            f"Port {port} is out of range. Must be 1–65535.",
            field="port",
        )
    return port


def validate_port_range(start: int, end: int) -> tuple[int, int]:
    """Validate a port scan range.

    Both ports must be valid (1–65535) and start <= end.
    The range is capped at 1024 ports maximum to prevent runaway scans.

    Raises:
        ValidationError: If either port is invalid or the range exceeds 1024.
    """
    validate_port(start)
    validate_port(end)
    if start > end:
        raise ValidationError(
            VALID_PARAM_INVALID,
            f"Start port {start} is greater than end port {end}.",
            field="port_range",
        )
    if (end - start) > 10000:
        raise ValidationError(
            VALID_PARAM_OUT_OF_RANGE,
            f"Port range {start}-{end} exceeds 10000 ports maximum.",
            field="port_range",
        )
    return start, end


def validate_duration(duration: str) -> str:
    """Validate a Duration literal string.

    Duration must be one of: ``5m``, ``15m``, ``30m``, ``1h``, ``6h``, ``24h``.

    Raises:
        ValidationError: If the duration is not a recognized literal.
    """
    allowed = {"5m", "15m", "30m", "1h", "6h", "24h"}
    if duration not in allowed:
        raise ValidationError(
            VALID_PARAM_INVALID,
            f"Invalid duration: {duration!r}. "
            f"Must be one of: {', '.join(sorted(allowed))}",
            field="duration",
        )
    return duration


def validate_line_limit(limit: int) -> int:
    """Validate a line limit for log-like outputs.

    Line limits must be in the range 1–500.

    Raises:
        ValidationError: If the limit is out of range.
    """
    if limit < 1 or limit > 500:
        raise ValidationError(
            VALID_PARAM_OUT_OF_RANGE,
            f"Line limit {limit} is out of range. Must be 1–500.",
            field="line_limit",
        )
    return limit


def validate_priority(priority: str) -> str:
    """Validate a journalctl priority level.

    Must be one of: emerg, alert, crit, err, warning, notice, info, debug.

    Raises:
        ValidationError: If the priority is not a recognized value.
    """
    allowed = {"emerg", "alert", "crit", "err", "warning", "notice", "info", "debug"}
    if priority.lower() not in allowed:
        raise ValidationError(
            VALID_PARAM_INVALID,
            f"Invalid priority: {priority!r}. "
            f"Must be one of: {', '.join(sorted(allowed))}",
            field="priority",
        )
    return priority.lower()


# ── Path Validators ───────────────────────────────────────────────────────────


def validate_workspace_path(path: str, root_dir: str) -> str:
    """Validate that a workspace path stays within the root directory.

    Resolves parent directory references (``..``) and rejects
    any path that would escape ``root_dir``. This is the domain-level
    pure validation; infrastructure-level symlink resolution over SSH
    lives in ``infrastructure/workspace_resolver.py``.

    Raises:
        ValidationError: If the path contains traversal or escapes root.
    """
    if ".." in PurePosixPath(path).parts:
        raise ValidationError(
            WS_PATH_ESCAPE,
            f"Workspace path contains '..': {path!r}",
            field="workspace_path",
        )

    normalized = PurePosixPath(path)
    root = PurePosixPath(root_dir)

    # String-based containment check (works for remote paths
    # that don't exist on the local filesystem)
    path_str = str(normalized)
    root_str = str(root)
    if not path_str.startswith(root_str):
        raise ValidationError(
            WS_PATH_ESCAPE,
            f"Workspace path escapes root directory: "
            f"{path!r} is not under {root_dir!r}",
            field="workspace_path",
        )

    return path


def validate_log_path(path: str, allowed_dirs: list[str]) -> str:
    """Validate that a log file path is under an allowed directory.

    The path must start with one of the directories in ``allowed_dirs``.
    Rejects traversal attempts and paths outside the allowlist.

    Args:
        path: The log file path to validate.
        allowed_dirs: List of allowed directory prefixes
            (e.g. ``["/var/log", "/home", "/opt"]``).

    Raises:
        ValidationError: If the path is not under any allowed directory.
    """
    if ".." in PurePosixPath(path).parts:
        raise ValidationError(
            WS_PATH_ESCAPE,
            f"Log path contains '..': {path!r}",
            field="log_path",
        )

    normalized = PurePosixPath(path)

    for allowed in allowed_dirs:
        allowed_prefix = PurePosixPath(allowed)
        if str(normalized).startswith(str(allowed_prefix)):
            return path

    raise ValidationError(
        VALID_PARAM_INVALID,
        f"Log path {path!r} is not under any allowed directory: "
        f"{', '.join(allowed_dirs)}",
        field="log_path",
    )