"""Workspace path resolution and validation for MCP OranPi.

Loads the workspace registry from workspaces.yaml (local file, version-controlled).
Validates workspace IDs and paths against domain rules. Uses SSHClient
to check directory existence on the remote OrangePi for activation status.

Design decisions:
- Domain owns validation rules (domain/validation.py, domain/workspace.py).
- Infrastructure owns SSH resolution (this module).
- The agent NEVER sees or provides raw filesystem paths.
- Path escape detection uses domain rules BEFORE SSH checks.
- Symlink resolution on the remote host is done via SSH.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import structlog
import yaml

from mcp_oranpi.config import AppConfig
from mcp_oranpi.domain.errors import (
    WS_DISABLED,
    WS_NOT_FOUND,
    WS_UNREACHABLE,
)
from mcp_oranpi.domain.validation import (
    ValidationError,
    validate_workspace_id,
    validate_workspace_path,
)
from mcp_oranpi.domain.workspace import WorkspaceInfo, WorkspaceStatus
from mcp_oranpi.infrastructure.ssh_client import SSHClient

log = structlog.get_logger()


class WorkspaceResolver:
    """Resolves logical workspace identifiers to remote paths.

    Loads workspace definitions from the local YAML registry,
    validates paths against the root directory, and checks
    directory existence on the remote host via SSH.

    Usage::

        resolver = WorkspaceResolver(app_config, ssh_client)
        await resolver.load()
        info = resolver.resolve("guardian")
        path = resolver.resolve_path("guardian")
    """

    def __init__(
        self,
        config: AppConfig,
        ssh_client: SSHClient,
    ) -> None:
        self._config = config
        self._ssh = ssh_client
        self._workspaces: dict[str, WorkspaceInfo] = {}
        self._paths: dict[str, str] = {}  # id -> remote path

    @property
    def workspaces(self) -> dict[str, WorkspaceInfo]:
        """Return all loaded workspace info objects."""
        return dict(self._workspaces)

    async def load(self) -> dict[str, WorkspaceInfo]:
        """Load workspace registry from YAML and validate paths.

        Reads the local workspaces.yaml file (NOT from the OrangePi),
        validates each workspace path against the root directory,
        then checks directory existence on the remote host via SSH.

        Invalid workspaces are marked as disabled but do NOT prevent
        the MCP from starting. Only tools referencing disabled workspaces
        return WS_DISABLED errors.

        Returns:
            Dictionary mapping workspace IDs to WorkspaceInfo.
        """
        config_path = Path(self._config.workspace_config)

        if not config_path.is_absolute():
            config_path = Path.cwd() / config_path

        log.info(
            "workspace_load_start",
            config_path=str(config_path),
            root_dir=self._config.root_workspace_dir,
        )

        try:
            raw = config_path.read_text(encoding="utf-8")
            data = yaml.safe_load(raw)
        except FileNotFoundError:
            log.error("workspace_config_not_found", path=str(config_path))
            return {}
        except yaml.YAMLError as exc:
            log.error("workspace_config_invalid", path=str(config_path), error=str(exc))
            return {}

        if not isinstance(data, dict) or "workspaces" not in data:
            log.error("workspace_config_format_error", path=str(config_path))
            return {}

        workspace_data = data["workspaces"]
        if not isinstance(workspace_data, dict):
            log.error("workspace_config_format_error", path=str(config_path))
            return {}

        root_dir = self._config.root_workspace_dir

        for ws_id, ws_config in workspace_data.items():
            try:
                validated_id = validate_workspace_id(ws_id)
            except ValidationError:
                log.warning(
                    "workspace_invalid_id",
                    workspace_id=ws_id,
                )
                self._workspaces[ws_id] = WorkspaceInfo(
                    id=ws_id,
                    path=str(ws_config.get("path", "")),
                    status=WorkspaceStatus.DISABLED,
                )
                continue

            if not isinstance(ws_config, dict) or "path" not in ws_config:
                log.warning(
                    "workspace_missing_path",
                    workspace_id=ws_id,
                )
                self._workspaces[validated_id] = WorkspaceInfo(
                    id=validated_id,
                    path="",
                    status=WorkspaceStatus.DISABLED,
                )
                continue

            ws_path = ws_config["path"]

            # Domain validation: check path doesn't escape root directory
            try:
                validate_workspace_path(ws_path, root_dir)
            except ValidationError:
                log.warning(
                    "workspace_path_escape",
                    workspace_id=validated_id,
                    path=ws_path,
                    root_dir=root_dir,
                )
                self._workspaces[validated_id] = WorkspaceInfo(
                    id=validated_id,
                    path=ws_path,
                    status=WorkspaceStatus.DISABLED,
                )
                self._paths[validated_id] = ws_path
                continue

            # SSH validation: check if directory exists on remote host
            info = await self._check_remote_directory(validated_id, ws_path)
            self._workspaces[validated_id] = info
            self._paths[validated_id] = ws_path

        active = sum(
            1 for ws in self._workspaces.values()
            if ws.status == WorkspaceStatus.ACTIVE
        )
        total = len(self._workspaces)
        log.info(
            "workspace_load_complete",
            active=active,
            total=total,
        )

        return dict(self._workspaces)

    def resolve(self, workspace_id: str) -> WorkspaceInfo | None:
        """Resolve a workspace ID to its WorkspaceInfo.

        Args:
            workspace_id: Logical workspace identifier (e.g., "guardian").

        Returns:
            WorkspaceInfo if found, None if not registered.
        """
        return self._workspaces.get(workspace_id)

    def resolve_path(self, workspace_id: str) -> str | None:
        """Resolve a workspace ID to its remote path.

        Args:
            workspace_id: Logical workspace identifier (e.g., "guardian").

        Returns:
            Remote path string if found, None if not registered.
        """
        return self._paths.get(workspace_id)

    def get_active_workspace(self, workspace_id: str) -> tuple[str, WorkspaceInfo]:
        """Get an active workspace path and info.

        Validates that the workspace exists and is active.
        Used by application-layer tools before executing commands.

        Args:
            workspace_id: Logical workspace identifier.

        Returns:
            Tuple of (remote_path, WorkspaceInfo).

        Raises:
            ValueError: With appropriate error code for:
                - WS_NOT_FOUND: workspace ID not registered
                - WS_DISABLED: workspace path escaped root or directory missing
                - WS_UNREACHABLE: SSH failed during directory check
        """
        info = self._workspaces.get(workspace_id)
        if info is None:
            raise ValueError(f"{WS_NOT_FOUND}: Workspace {workspace_id!r} not found")

        path = self._paths.get(workspace_id)
        if path is None:
            raise ValueError(f"{WS_NOT_FOUND}: Workspace {workspace_id!r} has no path")

        if info.status == WorkspaceStatus.DISABLED:
            raise ValueError(
                f"{WS_DISABLED}: Workspace {workspace_id!r} is disabled"
            )

        if info.status == WorkspaceStatus.UNREACHABLE:
            raise ValueError(
                f"{WS_UNREACHABLE}: Workspace {workspace_id!r} is unreachable"
            )

        return path, info

    # ── Private ────────────────────────────────────────────────────────────

    async def _check_remote_directory(
        self,
        workspace_id: str,
        ws_path: str,
    ) -> WorkspaceInfo:
        """Check if a workspace directory exists on the remote host.

        Uses SSHClient to execute 'test -d' on the remote path.
        On SSH failure, marks the workspace as unreachable.
        On directory not found, marks as disabled.

        Args:
            workspace_id: Logical workspace identifier.
            ws_path: Absolute path on the remote host.

        Returns:
            WorkspaceInfo with status set based on the check.
        """
        try:
            result = await self._ssh.execute(
                f"test -d {ws_path} && echo EXISTS || echo MISSING",
                timeout=10,
            )

            if result.exit_code == 0 and "EXISTS" in result.stdout:
                # Check for compose and git indicators
                compose = await self._check_indicator(
                    ws_path, "docker-compose.yml docker-compose.yaml compose.yml compose.yaml"
                )
                git = await self._check_indicator(ws_path, ".git")

                return WorkspaceInfo(
                    id=workspace_id,
                    path=ws_path,
                    status=WorkspaceStatus.ACTIVE,
                    has_compose=compose,
                    has_git=git,
                )

            # Directory doesn't exist on remote host
            log.warning(
                "workspace_directory_missing",
                workspace_id=workspace_id,
                path=ws_path,
            )
            return WorkspaceInfo(
                id=workspace_id,
                path=ws_path,
                status=WorkspaceStatus.DISABLED,
            )

        except (ConnectionError, OSError) as exc:
            # SSH connection not available or failed
            log.warning(
                "workspace_unreachable",
                workspace_id=workspace_id,
                path=ws_path,
                error=str(exc),
            )
            return WorkspaceInfo(
                id=workspace_id,
                path=ws_path,
                status=WorkspaceStatus.UNREACHABLE,
            )

        except (asyncio.CancelledError):
            # CancelledError must always be re-raised (spec 006, rule 1).
            raise
        except Exception as exc:
            # Unexpected error — mark unreachable for safety
            log.error(
                "workspace_check_error",
                workspace_id=workspace_id,
                path=ws_path,
                error=str(exc),
            )
            return WorkspaceInfo(
                id=workspace_id,
                path=ws_path,
                status=WorkspaceStatus.UNREACHABLE,
            )

    async def _check_indicator(self, ws_path: str, indicator: str) -> bool:
        """Check if an indicator file/directory exists in the workspace.

        For compose files (docker-compose.yml etc.), searches up to 2 levels
        deep to handle nested project directories. For other indicators
        like .git, only checks the workspace root.

        Args:
            ws_path: Remote workspace path.
            indicator: File or directory name to check (space-separated for OR).

        Returns:
            True if any indicator exists, False otherwise.
        """
        checks = indicator.split()
        if not checks:
            return False

        # Compose files may be nested one level deep (e.g. workspace/subdir/)
        # Use find with -maxdepth 2 for compose, -maxdepth 1 for others
        is_compose = any("compose" in c for c in checks)
        maxdepth = 2 if is_compose else 1

        # Build a find command: find /path -maxdepth N \( -name X -o -name Y \)
        name_args = " -o ".join(f'-name "{c}"' for c in checks)
        cmd = f'find "{ws_path}" -maxdepth {maxdepth} \\( {name_args} \\)'

        try:
            result = await self._ssh.execute(cmd, timeout=5)
            # find returns 0 even if nothing found; check if output is non-empty
            return result.exit_code == 0 and bool(result.stdout.strip())
        except (ConnectionError, OSError):
            return False