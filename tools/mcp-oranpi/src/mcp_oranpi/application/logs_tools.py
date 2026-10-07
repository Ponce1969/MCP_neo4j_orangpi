"""Logs audit tools for MCP OranPi.

Provides read-only access to Docker container logs, systemd journal,
and arbitrary log files on the remote OrangePi host.

All tools follow the audit-first, human-in-the-loop philosophy:
- Observe and report only
- No log rotation, deletion, or modification
- Constructor DI for all dependencies
"""

from __future__ import annotations

from typing import Any

import structlog

from mcp_oranpi.application.parsers import (
    parse_docker_logs,
    parse_journalctl,
    parse_stat_size,
)
from mcp_oranpi.domain.contracts import CommandRunnerProtocol
from mcp_oranpi.domain.errors import (
    CONN_FAILED,
    CONN_TIMEOUT,
    LOG_FILE_NOT_FOUND,
    LOG_FILE_NOT_READABLE,
    LOG_FILE_OVERSIZED,
    LOG_PATH_FORBIDDEN,
    LOG_QUERY_FAILED,
    ToolError,
    ToolResult,
)
from mcp_oranpi.domain.redaction import is_log_path_blocked
from mcp_oranpi.domain.truncation import truncate_text
from mcp_oranpi.domain.validation import (
    ValidationError,
    validate_line_limit,
    validate_log_path,
    validate_service_name,
)

log = structlog.get_logger()


JOURNAL_LOG_DEFAULT_LINES = 100


def resolve_journal_aliases(
    *,
    service: str | None,
    lines: int | None,
    unit: str | None,
    tail: int | None,
) -> tuple[str | None, int, str | None]:
    """Resolve the ``service``/``unit`` and ``lines``/``tail`` aliases.

    ``service`` and ``lines`` are the documented parameter names; ``unit`` and
    ``tail`` are accepted so older clients keep working. Sending both names with
    different values is a conflict, never a silent preference.

    Returns:
        ``(unit, lines, error)``, where ``error`` is set when the caller sent
        conflicting aliases.
    """
    if service is not None and unit is not None and service != unit:
        return (
            None,
            0,
            (
                f"Conflicting parameters: service={service!r} and unit={unit!r} "
                "select different units; send only one"
            ),
        )
    if lines is not None and tail is not None and lines != tail:
        return (
            None,
            0,
            (
                f"Conflicting parameters: lines={lines} and tail={tail} "
                "request different line counts; send only one"
            ),
        )

    resolved_unit = service if service is not None else unit
    resolved_lines = lines if lines is not None else tail
    if resolved_lines is None:
        resolved_lines = JOURNAL_LOG_DEFAULT_LINES
    return resolved_unit, resolved_lines, None


class LogsTools:
    """Logs inspection tools for OrangePi host.

    Provides read-only access to Docker container logs, systemd journal,
    and files under allowed directories. All operations are audit-only.

    Args:
        runner: CommandRunner instance for executing remote commands.
        allowed_log_dirs: List of allowed directory prefixes for file logs.
        max_log_file_mb: Maximum log file size in MB to read (default 50).
    """

    def __init__(
        self,
        runner: CommandRunnerProtocol,
        allowed_log_dirs: list[str],
        max_log_file_mb: int = 50,
    ) -> None:
        self._runner = runner
        self._allowed_log_dirs = allowed_log_dirs
        self._max_log_file_bytes = max_log_file_mb * 1024 * 1024

    # ── Docker Logs ───────────────────────────────────────────────────────────

    async def logs_docker(self, container: str, tail: int = 100) -> ToolResult:
        """Fetch logs from a Docker container (alias for docker_container_logs).

        Args:
            container: Container name or ID.
            tail: Number of lines to fetch from the end.

        Returns:
            ToolResult with DockerLogResult.
        """
        log.info("logs_docker", container=container, tail=tail)

        try:
            from mcp_oranpi.domain.validation import validate_container_name

            validate_container_name(container)
        except ValidationError as e:
            return ToolResult(
                error=ToolError(
                    code="VALID_PARAM_INVALID",
                    message=str(e),
                    retryable=False,
                )
            )

        try:
            validate_line_limit(tail)
        except ValidationError as e:
            return ToolResult(
                error=ToolError(
                    code="VALID_PARAM_INVALID",
                    message=str(e),
                    retryable=False,
                )
            )

        try:
            result = await self._runner.run(
                "docker_logs",
                container=container,
                tail=tail,
            )
        except TimeoutError:
            return ToolResult(
                error=ToolError(
                    code=CONN_TIMEOUT,
                    message="Command timed out while fetching container logs",
                    retryable=True,
                )
            )
        except ConnectionError:
            return ToolResult(
                error=ToolError(
                    code=CONN_FAILED,
                    message="SSH connection failed",
                    retryable=True,
                )
            )

        # Exit code non-zero may mean container not running or logs unavailable
        if result.exit_code != 0:
            stderr_lower = result.stderr.lower()
            if "no such container" in stderr_lower:
                return ToolResult(
                    error=ToolError(
                        code="DOCKER_NOT_FOUND",
                        message=f"Container not found: {container}",
                        detail={"container": container},
                        retryable=False,
                    )
                )

        log_result = parse_docker_logs(result.stdout, container)

        # Apply truncation
        truncated_text, meta = truncate_text(log_result.logs)

        return ToolResult(
            data={
                "container": log_result.container,
                "log_count": log_result.log_count,
                "logs": truncated_text,
                "truncated": meta.truncated,
            },
            truncated=meta.truncated,
        )

    # ── Systemd Journal ───────────────────────────────────────────────────────

    async def logs_systemd(
        self,
        service: str | None = None,
        priority: str = "info",
        lines: int | None = None,
        since: str | None = None,
        until: str | None = None,
        *,
        unit: str | None = None,
        tail: int | None = None,
    ) -> ToolResult:
        """Fetch logs from the systemd journal.

        Args:
            service: Optional systemd unit (service) name to filter by.
            priority: Priority level (emerg, alert, crit, err, warning,
                     notice, info, debug). Default info.
            lines: Number of lines to fetch from the end. Default 100.
            since: ISO timestamp or relative time (e.g., "1 hour ago").
            until: ISO timestamp or relative time upper bound (e.g., "1 hour ago").
            unit: Deprecated alias of ``service``, kept for older clients.
            tail: Deprecated alias of ``lines``, kept for older clients.

        Returns:
            ToolResult with SystemdLogResult.
        """
        unit, lines, alias_error = resolve_journal_aliases(
            service=service, lines=lines, unit=unit, tail=tail
        )
        if alias_error is not None:
            return ToolResult(
                error=ToolError(
                    code="VALID_PARAM_INVALID",
                    message=alias_error,
                    retryable=False,
                )
            )

        log.info("logs_systemd", unit=unit, priority=priority, tail=lines)

        if unit:
            try:
                validate_service_name(unit)
            except Exception as e:
                return ToolResult(
                    error=ToolError(
                        code="VALID_PARAM_INVALID",
                        message=str(e),
                        retryable=False,
                    )
                )

        try:
            validate_line_limit(lines)
        except ValidationError as e:
            return ToolResult(
                error=ToolError(
                    code="VALID_PARAM_INVALID",
                    message=str(e),
                    retryable=False,
                )
            )

        try:
            journal_params: dict[str, Any] = {"lines": lines, "priority": priority}
            if unit is not None:
                journal_params["unit"] = unit
            if since is not None:
                journal_params["since"] = since
            if until is not None:
                journal_params["until"] = until

            result = await self._runner.run("journalctl", **journal_params)
        except TimeoutError:
            return ToolResult(
                error=ToolError(
                    code=CONN_TIMEOUT,
                    message="Command timed out while fetching journal logs",
                    retryable=True,
                )
            )
        except ConnectionError:
            return ToolResult(
                error=ToolError(
                    code=CONN_FAILED,
                    message="SSH connection failed",
                    retryable=True,
                )
            )

        # journalctl returns exit code 1 if no entries found (not an error)
        # but exit code 4 if there's a serious problem
        if result.exit_code == 4:
            return ToolResult(
                error=ToolError(
                    code="LOG_UNIT_NOT_FOUND",
                    message=f"Journal unit not found or unavailable: {unit}",
                    detail={"unit": unit},
                    retryable=False,
                )
            )

        # A non-zero exit with empty stdout and a non-empty stderr is a real
        # journal query failure (for example a rejected timestamp argument).
        # Reporting it as an empty success would silently mislead callers.
        if result.exit_code != 0 and not result.stdout.strip() and result.stderr.strip():
            return ToolResult(
                error=ToolError(
                    code=LOG_QUERY_FAILED,
                    message=f"journalctl failed: {result.stderr.strip()[:200]}",
                    detail={"unit": unit},
                    retryable=False,
                )
            )

        log_result = parse_journalctl(result.stdout)

        # Apply truncation
        truncated_text, meta = truncate_text(log_result.logs)

        return ToolResult(
            data={
                "service": unit,
                "unit": unit,
                "log_count": log_result.log_count,
                "logs": truncated_text,
                "truncated": meta.truncated,
            },
            truncated=meta.truncated,
        )

    # ── File Logs ─────────────────────────────────────────────────────────────

    async def logs_file(
        self,
        path: str,
        tail: int = 1000,
        since: str | None = None,
    ) -> ToolResult:
        """Read a log file from the remote host.

        Implements a 7-step security sequence:
        1. Validate path against allowed_log_dirs
        1.5. Block paths to sensitive files (credentials, keys, tokens)
        2. Check file size (reject if > max_log_file_bytes)
        3. Resolve symlinks and validate resolved path
        4. Check file exists
        5. Read file content with tail
        6. Apply truncation and binary detection

        Args:
            path: Absolute path to the log file on remote host.
            tail: Number of lines to read from end of file.
            since: Optional ISO timestamp to read from (not supported in v0.1).

        Returns:
            ToolResult with FileLogResult.
        """
        log.info("logs_file", path=path, tail=tail)

        # Step 1: Validate path against allowed directories
        try:
            validate_log_path(path, self._allowed_log_dirs)
        except ValidationError:
            return ToolResult(
                error=ToolError(
                    code=LOG_PATH_FORBIDDEN,
                    message=f"Log path not allowed: {path}",
                    detail={"path": path, "allowed_dirs": self._allowed_log_dirs},
                    retryable=False,
                )
            )

        # Step 1.5: Block paths to sensitive files (credentials, keys, tokens)
        blocked, reason = is_log_path_blocked(path)
        if blocked:
            return ToolResult(
                error=ToolError(
                    code=LOG_PATH_FORBIDDEN,
                    message=f"Log path blocked for security: {reason}",
                    detail={"path": path, "reason": reason},
                    retryable=False,
                )
            )

        # Step 2: Check file size
        try:
            size_result = await self._runner.run("stat_size", path=path)
        except TimeoutError:
            return ToolResult(
                error=ToolError(
                    code=CONN_TIMEOUT,
                    message="Command timed out while checking file size",
                    retryable=True,
                )
            )
        except ConnectionError:
            return ToolResult(
                error=ToolError(
                    code=CONN_FAILED,
                    message="SSH connection failed",
                    retryable=True,
                )
            )

        file_size = parse_stat_size(size_result.stdout)

        if file_size > self._max_log_file_bytes:
            return ToolResult(
                error=ToolError(
                    code=LOG_FILE_OVERSIZED,
                    message=f"Log file size ({file_size} bytes) exceeds maximum "
                    f"({self._max_log_file_bytes} bytes)",
                    detail={
                        "path": path,
                        "size_bytes": file_size,
                        "max_bytes": self._max_log_file_bytes,
                    },
                    retryable=False,
                )
            )

        # Step 3: Resolve symlinks and validate resolved path
        try:
            link_result = await self._runner.run("readlink", path=path)
        except TimeoutError:
            return ToolResult(
                error=ToolError(
                    code=CONN_TIMEOUT,
                    message="Command timed out while resolving symlink",
                    retryable=True,
                )
            )
        except ConnectionError:
            return ToolResult(
                error=ToolError(
                    code=CONN_FAILED,
                    message="SSH connection failed",
                    retryable=True,
                )
            )

        resolved_path = link_result.stdout.strip()
        if resolved_path and resolved_path != path:
            # Validate the resolved path is still in allowed dirs
            try:
                validate_log_path(resolved_path, self._allowed_log_dirs)
            except ValidationError:
                return ToolResult(
                    error=ToolError(
                        code=LOG_PATH_FORBIDDEN,
                        message="Symlink resolves outside allowed directories",
                        detail={
                            "original_path": path,
                            "resolved_path": resolved_path,
                        },
                        retryable=False,
                    )
                )
            path = resolved_path

        # Step 4: Check file exists via tail command (will fail if not found)
        # We do this implicitly in step 5, but verify by checking stat_size succeeded
        # and that the file is not empty (empty files are valid)
        # If stat_size returned 0 and tail returned empty, that's still valid

        # Step 5: Read file content
        try:
            result = await self._runner.run("tail_file", path=path, lines=tail)
        except TimeoutError:
            return ToolResult(
                error=ToolError(
                    code=CONN_TIMEOUT,
                    message="Command timed out while reading file",
                    retryable=True,
                )
            )
        except ConnectionError:
            return ToolResult(
                error=ToolError(
                    code=CONN_FAILED,
                    message="SSH connection failed",
                    retryable=True,
                )
            )
        except UnicodeDecodeError:
            return ToolResult(
                error=ToolError(
                    code="LOG_FILE_BINARY",
                    message="Cannot read file: Appears to be binary data (Unicode decode failed).",
                    detail={"path": path},
                    retryable=False,
                )
            )

        # File might not exist or be unreadable
        if result.exit_code != 0:
            stderr_lower = result.stderr.lower()
            if "no such file" in stderr_lower or "cannot open" in stderr_lower:
                return ToolResult(
                    error=ToolError(
                        code=LOG_FILE_NOT_FOUND,
                        message=f"Log file not found: {path}",
                        detail={"path": path},
                        retryable=False,
                    )
                )
            return ToolResult(
                error=ToolError(
                    code=LOG_FILE_NOT_FOUND,
                    message=f"Could not read log file: {result.stderr[:200]}",
                    detail={"path": path},
                    retryable=False,
                )
            )

        content = result.stdout

        # Step 6: Binary detection
        if content:
            # Check first 512 bytes for null bytes (binary file indicator)
            check_len = min(512, len(content))
            if "\x00" in content[:check_len]:
                return ToolResult(
                    error=ToolError(
                        code=LOG_FILE_NOT_READABLE,
                        message="Log file appears to be binary, not readable as text",
                        detail={"path": path},
                        retryable=False,
                    )
                )

        # Note: last_modified requires stat -c %y which isn't in ALLOWED_COMMANDS yet.
        # Would need to extend command_runner.py to support it.
        last_modified = ""

        # Count lines read
        lines_read = len(content.splitlines()) if content.strip() else 0

        # Apply truncation
        truncated_content, meta = truncate_text(content)

        return ToolResult(
            data={
                "path": path,
                "size_bytes": file_size,
                "last_modified": last_modified,
                "lines_read": lines_read,
                "content": truncated_content,
                "truncated": meta.truncated,
            },
            truncated=meta.truncated,
        )
