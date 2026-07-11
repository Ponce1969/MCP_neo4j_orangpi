"""Test helpers shared across test modules.

Contains mock objects for SSH client, command runner, workspace resolver,
and other infrastructure that are used both as fixtures and as type hints.
"""

from __future__ import annotations

from mcp_oranpi.domain.workspace import WorkspaceInfo
from mcp_oranpi.infrastructure.ssh_client import CommandResult


class MockSSHClient:
    """Synchronous mock for SSHClient in unit tests.

    Provides controllable responses and tracks call history
    without requiring a real SSH connection.
    """

    def __init__(self) -> None:
        self.connected: bool = True
        self._commands: list[str] = []
        self._results: dict[str, CommandResult] = {}
        self._default_result: CommandResult = CommandResult(
            exit_code=0, stdout="", stderr=""
        )
        self._execute_side_effect: Exception | None = None

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def execute(
        self, command: str, *, timeout: int | None = None
    ) -> CommandResult:
        self._commands.append(command)

        if self._execute_side_effect is not None:
            raise self._execute_side_effect

        if not self.connected:
            raise ConnectionError("SSH client is not connected")

        # Return a specific result if configured for this command
        for pattern, result in self._results.items():
            if pattern in command:
                return result

        return self._default_result

    async def health_check(self) -> bool:
        return self.connected

    @property
    def state(self) -> str:
        return "connected" if self.connected else "disconnected"

    # ── Test Helpers ───────────────────────────────────────────────────────

    def set_result(self, pattern: str, result: CommandResult) -> None:
        """Configure a result for commands matching a pattern."""
        self._results[pattern] = result

    def set_default_result(self, result: CommandResult) -> None:
        """Configure the default result for any unmatched command."""
        self._default_result = result

    def set_execute_error(self, error: Exception) -> None:
        """Configure execute() to raise an error."""
        self._execute_side_effect = error

    @property
    def commands(self) -> list[str]:
        """Return all commands that were executed."""
        return list(self._commands)

    @property
    def last_command(self) -> str | None:
        """Return the last executed command, or None."""
        return self._commands[-1] if self._commands else None


class MockCommandRunner:
    """Mock CommandRunner that returns predefined results.

    Provides controllable responses and tracks call history
    without requiring a real SSH connection or command validation.
    """

    def __init__(self) -> None:
        self._responses: dict[str, CommandResult] = {}
        self._calls: list[tuple[str, dict[str, object]]] = []
        self._default_result = CommandResult(exit_code=0, stdout="", stderr="")

    def set_response(self, command_key: str, result: CommandResult) -> None:
        """Configure a result for a specific command key."""
        self._responses[command_key] = result

    def set_default_result(self, result: CommandResult) -> None:
        """Configure the default result for any unmatched command."""
        self._default_result = result

    async def run(
        self,
        command_key: str,
        *,
        timeout: int | None = None,
        **params: str | int,
    ) -> CommandResult:
        """Execute a command and return the configured response."""
        self._calls.append((command_key, {"timeout": timeout, **params}))
        if command_key in self._responses:
            return self._responses[command_key]
        return self._default_result

    async def run_in_workspace(
        self,
        command_key: str,
        workspace_path: str,
        *,
        timeout: int | None = None,
        **params: str | int,
    ) -> CommandResult:
        """Execute a command in workspace and return the configured response."""
        self._calls.append(
            (command_key, {"workspace_path": workspace_path, "timeout": timeout, **params})
        )
        if command_key in self._responses:
            return self._responses[command_key]
        return self._default_result

    @property
    def calls(self) -> list[tuple[str, dict[str, object]]]:
        """Return all calls made to the runner."""
        return list(self._calls)

    def last_call(self) -> tuple[str, dict[str, object]] | None:
        """Return the last call made, or None."""
        return self._calls[-1] if self._calls else None


class MockWorkspaceResolver:
    """Mock WorkspaceResolver that returns predefined workspaces.

    Provides controllable workspace metadata without requiring
    a real workspace YAML file or SSH connection.
    """

    def __init__(self) -> None:
        self._workspaces: dict[str, WorkspaceInfo] = {}

    def set_workspaces(self, workspaces: dict[str, WorkspaceInfo]) -> None:
        """Configure workspaces to return."""
        self._workspaces = workspaces

    @property
    def workspaces(self) -> dict[str, WorkspaceInfo]:
        """Return all configured workspaces."""
        return self._workspaces

    def resolve(self, workspace_id: str) -> WorkspaceInfo | None:
        """Resolve a workspace by ID."""
        return self._workspaces.get(workspace_id)

    def resolve_path(self, workspace_id: str) -> str | None:
        """Resolve a workspace path by ID."""
        ws = self._workspaces.get(workspace_id)
        return ws.path if ws else None