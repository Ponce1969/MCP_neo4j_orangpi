"""Type aliases and contracts for MCP OranPi tool inputs/outputs.

Protocols define the interfaces that the application layer depends on.
Infrastructure provides the concrete implementations.

Dependency direction (hexagonal):
    domain ← application ← infrastructure
    application imports protocols from domain contracts,
    infrastructure implements those protocols.
"""

from __future__ import annotations

from typing import Literal, Protocol

from mcp_oranpi.domain.workspace import WorkspaceInfo

# ── Type Aliases ──────────────────────────────────────────────────────────────

# Duration for time-based queries (e.g., system_cpu_usage averaging window)
type Duration = Literal["5m", "15m", "30m", "1h", "6h", "24h"]

# Line limit for log-like outputs (1-500, default 100)
type LineLimit = int


# ── Protocol Interfaces ───────────────────────────────────────────────────────
# Application-layer tools depend on these protocols, NOT on infrastructure
# concretions. The composition root (server.py) wires concrete implementations.


class CommandRunnerProtocol(Protocol):
    """Protocol for safe command execution over SSH.

    The application layer uses this protocol to execute commands.
    Infrastructure provides CommandRunner as the concrete implementation.
    """

    async def run(
        self,
        command_key: str,
        *,
        timeout: int | None = None,
        **params: str | int,
    ) -> CommandResultProtocol: ...

    async def run_in_workspace(
        self,
        command_key: str,
        workspace_path: str,
        *,
        timeout: int | None = None,
        **params: str | int,
    ) -> CommandResultProtocol: ...


class CommandResultProtocol(Protocol):
    """Protocol for command execution results.

    Matches the infrastructure CommandResult interface without
    importing from infrastructure.
    """

    exit_code: int
    stdout: str
    stderr: str


class WorkspaceResolverProtocol(Protocol):
    """Protocol for workspace path resolution.

    The application layer uses this protocol to resolve workspace
    IDs to paths. Infrastructure provides WorkspaceResolver.
    """

    @property
    def workspaces(self) -> dict[str, WorkspaceInfo]: ...

    async def load(self) -> dict[str, WorkspaceInfo]: ...

    def resolve(self, workspace_id: str) -> WorkspaceInfo | None: ...

    def resolve_path(self, workspace_id: str) -> str | None: ...


class SSHClientProtocol(Protocol):
    """Protocol for SSH connection handling.

    Abstracts the infrastructure SSHClient for type-safe mocking
    and strict dependency inversion.
    """

    @property
    def connected(self) -> bool: ...

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def execute(
        self, command: str, *, timeout: int | None = None
    ) -> CommandResultProtocol: ...

    async def health_check(self) -> bool: ...
